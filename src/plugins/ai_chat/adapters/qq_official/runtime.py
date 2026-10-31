"""Default-off official ingress, shared turns and sender, independent of OneBot."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import inspect
import hashlib
import logging
import re
import traceback
from types import SimpleNamespace

from src.bot_storage import DatabaseCalls, DatabaseError
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.lifecycle import BackgroundTaskSupervisor
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from src.plugins.ai_chat.turn_journal import TurnJournal
from src.plugins.ai_chat.workers.durable_jobs import DurableJobWorker
from src.qq_official.gateway import GatewayConnection
from src.qq_official.http import RestClient
from src.qq_official.errors import QQAPIError
from src.qq_official.token import TokenManager

from .binding import BindingReducer
from .config import OfficialConfig
from .inbox import OfficialInboxStore
from .keys import account_job_kind
from .lease import DedicatedAccountLease, LeaseLost, LeaseUnavailable, advisory_key_for
from .identity_store import observe_ready, observe_self_api
from .send_store import OfficialSendStore
from .official_metrics import observe


class _FencedToken:
    def __init__(self, manager, fence):
        self._manager, self._fence = manager, fence
        self.credentials, self.base_url = manager.credentials, manager.base_url

    async def get(self):
        await self._fence()
        value = await self._manager.get()
        await self._fence()
        return value

    async def prepare(self):
        await self._fence()
        value = await self._manager.prepare()
        await self._fence()
        return value

    def invalidate(self, token=None):
        self._manager.invalidate(token)


class _FencedSocket:
    def __init__(self, socket, fence):
        self._socket, self._fence = socket, fence

    async def send(self, payload):
        await self._fence()
        await self._socket.send(payload)

    async def receive(self):
        return await self._socket.receive()

    async def close(self):
        await self._socket.close()


@asynccontextmanager
async def open_official_protocol(loaded, *, checkpoint, on_checkpoint, ensure_current):
    """Construct transport only after acquisition; fence Identify/Resume and I/O."""
    tokens = TokenManager(loaded.credentials, loaded.api_base, allow_test_server=loaded.allow_test_server)
    rest = RestClient(_FencedToken(tokens, ensure_current), allow_test_server=loaded.allow_test_server)
    gateway = GatewayConnection(rest, checkpoint=checkpoint, on_checkpoint=on_checkpoint)
    native_connector = gateway.connector

    @asynccontextmanager
    async def connector(url):
        await ensure_current()
        async with native_connector(url) as socket:
            yield _FencedSocket(socket, ensure_current)

    gateway.connector = connector
    try:
        await ensure_current()
        yield gateway
    finally:
        try:
            await gateway.stop()
        finally:
            try:
                await rest.aclose()
            finally:
                await tokens.aclose()


class OfficialRuntime:
    def __init__(self, config: OfficialConfig, database, *, logger, ledger=None, journal=None,
                 protocol_factory=open_official_protocol, binding_sender=None,
                 lease_factory=DedicatedAccountLease, probe_seconds: float = 1,
                 retry_seconds: float = 2, allow_test_server: bool = False,
                 application=None, turn_consumer_factory=None, file_services_factory=None, approval_factory=None, migration_factory=None, media_factory=None, background_factory=None):
        self.config, self.database, self.logger = config, database, logger
        self.observer = None
        if config.enabled:
            from ...observability import telemetry
            from .official_metrics import official_metrics
            self.observer = official_metrics(telemetry.registry)
        self.protocol_factory, self.binding_sender = protocol_factory, binding_sender
        self.lease_factory = lease_factory
        self.probe_seconds = min(max(probe_seconds, 0.01), 5)
        self.retry_seconds = min(max(retry_seconds, 0.01), 60)
        self.allow_test_server = allow_test_server
        self.application, self.turn_consumer_factory = application, turn_consumer_factory
        self.migration_factory, self.migration_service = migration_factory, None
        self.background_factory, self.background_service = background_factory, None
        self.admin_identity_service = None
        self.approval_factory = approval_factory
        self.media_factory = media_factory
        self.media_service = None
        self.approval_service = None
        self.file_services_factory = file_services_factory
        self.file_service = None
        self.turn_consumer = None
        self.agent_service = None
        self.ledger = ledger
        self._owns_ledger = ledger is None
        self.journal = journal
        self._owns_journal = journal is None
        self.deliveries = None
        self.outbox = None
        self.sender = None
        self.loaded = None
        self.store = None
        self.jobs = None
        self._calls = DatabaseCalls()
        self._lifecycle_lock = asyncio.Lock()
        self._supervisor = BackgroundTaskSupervisor(logger)
        self._resume = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._stopping = False
        self._started = False
        self._token = None
        self._gateway = None
        self.state = "disabled"
        self.reason = "not_started"

    async def start(self) -> bool:
        async with self._lifecycle_lock:
            # Constructor calls may acquire database resources in a thread.
            # Finish assigning their results before cancellation can close them.
            initialization = asyncio.create_task(self._initialize())
            try:
                return await asyncio.shield(initialization)
            except asyncio.CancelledError:
                self._stopping = True
                while not initialization.done():
                    try:
                        await asyncio.shield(initialization)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not initialization.cancelled():
                    initialization.exception()
                await self._stop_locked()
                raise

    async def _initialize(self) -> bool:
        if self._started or self._stopping or not self.config.enabled:
            return False
        try:
            self.loaded = await self._calls(self.config.load, allow_test_server=self.allow_test_server)
            # HTTP dependency INFO/DEBUG messages include opaque endpoint URLs.
            # Do not allow application log settings to expose official IDs.
            for namespace in ("httpx", "httpcore"):
                dependency_logger = logging.getLogger(namespace)
                dependency_logger.setLevel(max(logging.WARNING, dependency_logger.level))
            self.jobs = await self._calls(DurableJobStore, self.database)
            if self.ledger is None:
                self.ledger = await self._calls(MessageLedger, self.database)
            self.deliveries = await self._calls(DeliveryStore, self.database, recover_interrupted=False)
            if self.journal is None:
                self.journal = await self._calls(TurnJournal, self.database)
            from .lease import assert_epoch
            binding = BindingReducer(self.loaded.binding_pepper, epoch_guard=assert_epoch)
            self.store = await self._calls(OfficialInboxStore, self.database, jobs=self.jobs, binding=binding,
                private_policy=self.loaded.private_policy, private_allowed_openids=self.loaded.private_allowed_openids,
                payload_retention_hours=self.loaded.payload_retention_hours)
            await self._calls(self.store.ensure_account, self.loaded.account)
            self.outbox = OfficialSendStore(self.deliveries, admission=self.store.admitted,
                pepper=self.loaded.binding_pepper, certification=self.loaded.delivery.certification,
                max_wait_seconds=self.loaded.delivery.max_wait_seconds,
                allow_newer_windows=self.loaded.delivery.allow_newer_windows)
        except Exception:
            await self._close_stores()
            self.state, self.reason = "failed", "configuration_or_storage_unavailable"
            raise RuntimeError("Official ingress could not initialize") from None
        if self._stopping:
            return False
        self._started = True
        self.state, self.reason = "starting", "waiting_for_lease"
        self._supervisor.start(account_job_kind("ingest", self.loaded.account.key), self._run)
        return True

    def status(self) -> dict:
        # No raw account, session, URL, credential, binding effect or DB exception.
        return {"state": self.state, "reason": self.reason, "started": self._started,
                "turn_execution_connected": self.turn_consumer is not None,
                "background_agent_connected": self.agent_service is not None,
                "binding_transport_connected": self.binding_sender is not None or self.sender is not None,
                "durable_sender_connected": self.sender is not None,
                "approval_transport_connected": self.approval_service is not None}

    async def _ensure_current(self, token):
        if self._stopping or self._token is not token:
            raise LeaseLost("Official runtime is not the current account owner")

        def check():
            with self.store.transaction() as cursor:
                self.store.epoch_guard(cursor, token)

        await self._calls(check)

    async def _run(self):
        try:
            while not self._stopping:
                self._resume.clear()
                self._idle.clear()
                lease = self.lease_factory(self.database.dsn, self.loaded.account, schema=self.database.schema)
                token = None
                try:
                    try:
                        token = await self._calls(lease.acquire)
                    except LeaseUnavailable:
                        self.state, self.reason = "paused", "lease_or_account_unavailable"
                    else:
                        if token is None:
                            self.state, self.reason = "waiting", "account_owned_by_another_instance"
                            await asyncio.sleep(self.retry_seconds)
                            continue
                        self._token = token
                        await self._calls(self.store.apply_configuration, token, self.loaded.owner_openids)
                        await self._calls(self._recover_binding, token)
                        checkpoint = await self._calls(self.store.checkpoint, token)
                        await self._serve(lease, token, checkpoint)
                except asyncio.CancelledError:
                    raise
                except LeaseLost:
                    self.state, self.reason = "paused", "account_lease_lost"
                except Exception as error:
                    frames = ' > '.join(f'{x.name}:{x.lineno}' for x in traceback.extract_tb(error.__traceback__)[-6:])
                    self.logger.warning(f'Official ingress paused: {type(error).__name__} at {frames}')
                    self.state, self.reason = "paused", "ingress_unavailable"
                    if token is not None:
                        try:
                            await self._calls(self.store.pause, token, "ingress_unavailable")
                        except Exception:
                            pass
                finally:
                    self._token = None
                    self._gateway = None
                    self.sender = None
                    self.turn_consumer = None
                    self.agent_service = None
                    # DatabaseCalls drains non-cancellable transaction threads
                    # before this close or any later account acquisition.
                    try:
                        await self._calls(lease.close)
                    finally:
                        self._idle.set()
                if not self._stopping:
                    await self._resume.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Supervisor logs its exception; only a fixed safe diagnostic leaves.
            raise RuntimeError("Official ingress lifecycle failed") from None

    def _recover_binding(self, token):
        with self.store.transaction() as cursor:
            self.store.binding.recover_sending(cursor, account=self.loaded.account, lease_token=token)

    async def _serve(self, lease, token, checkpoint):
        session_id = checkpoint.session_id if checkpoint is not None else None
        active_turns = set()
        turn_locks = {}
        self_identity_attempted = False

        async def on_checkpoint(value):
            await self._calls(self.store.checkpoint_callback, token, value)

        async def on_dispatch(frame):
            nonlocal session_id, self_identity_attempted
            if frame.get("t") == "READY":
                data = frame.get("d")
                session_id = data.get("session_id") if isinstance(data, dict) else None
            await self._ensure_current(token)
            accepted = await self._calls(self.store.accept, frame, token=token, session_id=session_id, ledger=self.ledger)
            observe(self.observer, 'inbox', accepted.disposition)
            if accepted.inbox_id:
                data=frame.get('d') if isinstance(frame.get('d'),dict) else {}
                group=data.get('group_openid')
                author=data.get('author') if isinstance(data.get('author'),dict) else {}
                native=group or author.get('user_openid','')
                self.logger.info(f"QQ receive kind={'group' if group else 'private'} "
                    f"endpoint={hashlib.sha256(str(native).encode()).hexdigest()[:12]} "
                    f"type={frame.get('t')} at={frame.get('t')=='GROUP_AT_MESSAGE_CREATE'} result={accepted.disposition}")
            if frame.get("t") == "READY":
                def record_ready():
                    with self.store.transaction() as cursor:
                        return observe_ready(cursor, token, frame, int(self.store.clock()))
                identity_result = await self._calls(record_ready)
                if identity_result.status == "unknown" and not self_identity_attempted and self.sender is not None:
                    self_identity_attempted = True
                    # This authenticated account self-read is a fallback for a
                    # READY without complete identity evidence. A rejection or
                    # unsupported response leaves sending disabled, never guessed.
                    try:
                        async with asyncio.timeout(5):
                            own = await self.sender.rest.request("GET", "/users/@me")
                    except (QQAPIError, TimeoutError):
                        pass
                    else:
                        def record_self():
                            with self.store.transaction() as cursor:
                                return observe_self_api(cursor, token, own, int(self.store.clock()))
                        await self._calls(record_self)
            if accepted.ingested is not None:
                await run_inbox(accepted.inbox_id, accepted.ingested)
            if self.sender is not None:
                self.sender.wake()
            if accepted.approval is not None and self.approval_service is not None:
                await self.approval_service.consume(accepted.approval)
            if accepted.binding is not None and accepted.binding.effect is None and self.sender is not None:
                from ...message_ir import MessageBody, TextNode
                from ...platform_types import EndpointKey, ActorKey
                from .renderer import render_plan
                def reply_binding():
                    with self.store.transaction() as cursor:
                        source = cursor.execute('SELECT * FROM qq_official_inbound_events WHERE inbox_id=?', (accepted.inbox_id,)).fetchone()
                        endpoint = EndpointKey.parse(source['endpoint_key'])
                        self.outbox.enqueue(token, rendered=render_plan(MessageBody((TextNode(0, accepted.binding.message),)), endpoint, markdown=False),
                            requester=ActorKey.parse(source['control_actor_key']), logical_key='binding-reply:'+accepted.inbox_id,
                            source_scope_key=endpoint.scope.key, expires_at=int(self.store.clock())+300,
                            source_native_message_id=source['native_message_id'], control_reply_id=accepted.inbox_id, transaction_cursor=cursor)
                await self._calls(reply_binding)
                self.sender.wake()
            if accepted.binding is not None and accepted.binding.effect is not None:
                await self._deliver_binding(token, accepted.binding.effect)
            if accepted.binding is not None and accepted.binding.status == 'confirmed' and self.sender is not None:
                await self.sender.refresh_bound_groups()

        async def run_inbox(inbox_id, result):
            observe(self.observer, 'ingest', result.status)
            if result.status != 'processed' or result.echo_state != 'not_echo':
                return
            def input_row():
                with self.store.transaction() as cursor:
                    return dict(cursor.execute('''SELECT i.endpoint_key,i.route_epoch,i.turn_text,
                        i.canonical_message_id,m.sender_actor_key FROM qq_official_inbound_events i
                        JOIN messages m USING(canonical_message_id) WHERE inbox_id=?''', (inbox_id,)).fetchone())
            row = await self._calls(input_row)
            if result.reply_requested and self.background_service is not None:
                self.background_service.direct_message(row['endpoint_key'])
            source = SimpleNamespace(job_id=None, payload={'inbox_id': inbox_id}, lease_owner=token.holder_id,
                lease_generation=1, attempts=1, kind=account_job_kind('turn', token.account_key), endpoint=row['endpoint_key'])
            key = row['endpoint_key'], row['sender_actor_key'], row['route_epoch']
            if batcher.add(key, source, row['turn_text'], requested=result.reply_requested,
                           canonical_id=row['canonical_message_id']):
                return
            await dispatch_turn(source, result.reply_requested)

        async def dispatch_turn(source, reply_requested=True):
            async def run():
                try:
                    if reply_requested:
                        if self.turn_consumer is not None:
                            async with turn_locks.setdefault(source.endpoint, asyncio.Lock()):
                                await self.turn_consumer(source)
                    elif self.background_service is not None:
                        await self.background_service.on_ingested(source)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    frames = [(frame.name, frame.lineno) for frame in traceback.extract_tb(error.__traceback__)[-4:]]
                    self.logger.warning(f'QQ turn error={type(error).__name__} frames={frames}')
            task = asyncio.create_task(run())
            active_turns.add(task)
            task.add_done_callback(active_turns.discard)

        from .message_batch import MessageBatcher
        batcher = MessageBatcher(dispatch_turn)

        async def monitor():
            loop = asyncio.get_running_loop()
            next_purge = 0.0
            next_result_scan = 0.0
            next_file_scan = 0.0
            while not self._stopping:
                if not await self._calls(lease.probe):
                    raise LeaseLost("Official account lease was lost")
                if loop.time() >= next_purge:
                    try:
                        deleted = await self._calls(self.store.purge_terminal_payloads, token)
                    except Exception:
                        observe(self.observer, 'cleanup', 'failed')
                        raise
                    observe(self.observer, 'cleanup', 'completed', count=deleted)
                    next_purge = loop.time() + 60
                if self.agent_service is not None and loop.time() >= next_result_scan:
                    await self.agent_service.reconcile()
                    next_result_scan = loop.time() + 5
                if self.migration_service is not None:
                    await self.migration_service.maintain()
                if self.file_service is not None and loop.time() >= next_file_scan:
                    await self.file_service.maintain()
                    next_file_scan = loop.time() + 30
                await asyncio.sleep(self.probe_seconds)

        worker = DurableJobWorker(self.jobs, logger=self.logger, poll_seconds=0.1,
                                  concurrency=2, per_scope_limit=1, database_calls=self._calls)
        async with self.protocol_factory(self.loaded, checkpoint=checkpoint, on_checkpoint=on_checkpoint,
                                         ensure_current=lambda: self._ensure_current(token)) as gateway, AsyncExitStack() as resources:
            self._gateway = gateway
            gateway.observer = self.observer
            from .admin_identity import AdminIdentityBinding
            self.admin_identity_service = AdminIdentityBinding(self, token)
            self.store.admin_identity_service = self.admin_identity_service
            async def close_identity():
                self.store.admin_identity_service = None
                self.admin_identity_service = None
            resources.push_async_callback(close_identity)
            if self.migration_factory is not None:
                from .host_services import OfficialHostSession
                migration = await self._optional_feature('projects', self.migration_factory, self, token)
                if migration is not None:
                    if not isinstance(migration, OfficialHostSession):
                        raise RuntimeError('official_migration_service_invalid')
                    async def close_migration():
                        try:
                            await migration.close()
                        finally:
                            self.migration_service = None
                    resources.push_async_callback(close_migration)
                    await self._calls(migration.attach)
                    self.migration_service = migration
            rest = getattr(gateway, "rest_client", None)
            if isinstance(rest, RestClient):
                from .sender import OfficialSender
                self.sender = OfficialSender(self.loaded.account, token, self.outbox, rest, self.ledger,
                    journal=self.journal, database_calls=self._calls, observer=self.observer,
                    ensure_current=lambda: self._ensure_current(token))
                await self.sender.recover()
                await self.sender.refresh_bound_groups()
            if self.approval_factory is not None and self.sender is not None:
                from .approval import OfficialApprovalService
                async def reset_approval_bindings():
                    self.approval_service = None
                    self.store.approval_store = None
                    self.outbox.approval_authority = None
                # A host factory must clean up construction failures of its own
                # resources; partially installed authority never reaches next epoch.
                resources.push_async_callback(reset_approval_bindings)
                service = await self._optional_feature('approval', self.approval_factory, self, token)
                if service is not None:
                    if not isinstance(service, OfficialApprovalService):
                        raise RuntimeError('official_approval_service_invalid')
                    # Capture this service, not the later file-factory variable.
                    async def cleanup_approval(current=service):
                        try:
                            await current.close()
                        finally:
                            self.approval_service = None
                            self.store.approval_store = None
                            self.outbox.approval_authority = None
                    resources.push_async_callback(cleanup_approval)
                    await self._calls(service.attach, self, token)
                    self.approval_service = service
            file_worker = None
            if self.file_services_factory is not None and isinstance(rest, RestClient):
                service = await self._optional_feature('files', self.file_services_factory, self, token, rest)
                if service is not None:
                    self.file_service = service
                    self.outbox.protected_payloads = service.payloads

                    async def close_files():
                        try:
                            await service.close()
                        finally:
                            self.file_service = None
                            self.outbox.protected_payloads = None

                    resources.push_async_callback(close_files)
                    file_worker = DurableJobWorker(self.jobs, logger=self.logger, poll_seconds=0.1,
                        concurrency=1, per_scope_limit=1, database_calls=self._calls)
                    file_worker.register(account_job_kind('file', self.loaded.account.key), service.dispatcher)
                    imports=self.migration_service.domains.get('file') if self.migration_service is not None else None
                    if imports is not None:
                        file_worker.register(account_job_kind('file_import',token.account_key), imports.execute)
            if self.media_factory is not None:
                media = await self._optional_feature('media', self.media_factory, self, token)
                self.media_service = media
                if media is not None:
                    async def close_media():
                        try:
                            await media.close()
                        finally:
                            self.media_service = None
                    resources.push_async_callback(close_media)
            background = (await self._optional_feature('background', self.background_factory, self, token)
                if self.background_factory is not None else None)
            if background is None:
                from .native_background import NativeBackgroundService
                background = NativeBackgroundService(runtime=self, token=token)
                background.attach()
            self.background_service = background
            async def close_background():
                try:
                    await background.close()
                finally:
                    self.background_service = None
            resources.push_async_callback(close_background)
            factory = self.turn_consumer_factory
            agent_worker = None
            if self.application is not None:
                from .background_agents import OfficialBackgroundAgents
                self.agent_service = OfficialBackgroundAgents(self, token, self.application)
                await self._calls(self.agent_service.store.recover, token.account_key)
                resources.push_async_callback(self.agent_service.close)
                agent_worker = DurableJobWorker(self.jobs, logger=self.logger, poll_seconds=0.1,
                    concurrency=3, per_scope_limit=3, database_calls=self._calls)
                agent_worker.register(account_job_kind("agent", self.loaded.account.key), self.agent_service.execute)
            if factory is None and self.application is not None:
                from .turns import build_turn_consumer
                factory = lambda current, lease_token: build_turn_consumer(current, lease_token, self.application)
            if factory is not None:
                self.turn_consumer = factory(self, token)
            self.state, self.reason = "running", "receiving_events"
            await self._calls(self._recover_interrupted_turns, token)
            tasks = [asyncio.create_task(gateway.run(on_dispatch)), asyncio.create_task(worker.run_forever()),
                     asyncio.create_task(monitor())]
            if self.sender is not None:
                tasks.append(asyncio.create_task(self.sender.run()))
            if self.approval_service is not None:
                tasks.append(asyncio.create_task(self.approval_service.run()))
            if agent_worker is not None:
                tasks.append(asyncio.create_task(agent_worker.run_forever()))
            if file_worker is not None:
                tasks.append(asyncio.create_task(file_worker.run_forever()))
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for finished in done:
                    await finished
                if not self._stopping:
                    candidate = getattr(gateway, "reason", "gateway_stopped")
                    reason = candidate if isinstance(candidate, str) and re.fullmatch(r"[a-z0-9_]{1,80}", candidate) else "gateway_stopped"
                    await self._calls(self.store.pause, token, reason)
                    self.state, self.reason = "paused", reason
            finally:
                await batcher.close()
                for turn in tuple(active_turns):
                    turn.cancel()
                await asyncio.gather(*tuple(active_turns), return_exceptions=True)
                self._gateway = None
                self._token = None
                try:
                    await gateway.stop()
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    self.sender = None
                    self.turn_consumer = None
                    self.agent_service = None

    def _recover_interrupted_turns(self, token):
        from src.plugins.ai_chat.message_ir import MessageBody, TextNode, body_from_json
        from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
        from .renderer import render_plan

        now = int(self.store.clock())
        notice = '刚才重启了，这条没处理完，麻烦再说一次'
        with self.store.transaction() as cursor:
            self.store.epoch_guard(cursor, token)
            cursor.execute("UPDATE durable_jobs SET status='cancelled',lease_owner='',lease_until=NULL,finished_at=?,updated_at=? WHERE kind IN (?,?) AND status IN ('pending','running')",
                (now, now, account_job_kind('ingest', token.account_key), account_job_kind('turn', token.account_key)))
            cursor.execute("UPDATE qq_official_inbound_events SET status='skipped',reason_code='restart_interrupted',processed_at=?,updated_at=? WHERE account_key=? AND status='pending'",
                (now, now, token.account_key))
            rows = cursor.execute('''SELECT x.* FROM qq_official_turn_executions x
                LEFT JOIN agent_turns t ON t.turn_id=x.journal_turn_id
                WHERE x.account_key=? AND x.account_fencing_epoch<?
                AND (t.status='running' OR x.state='executing'
                     OR x.state IN ('prepared','needs_attention','result_ready'))
                ORDER BY x.created_at DESC FOR UPDATE OF x''', (token.account_key, token.epoch)).fetchall()
            notified = set()
            for row in rows:
                endpoint = EndpointKey.parse(row['endpoint_key'])
                actor = ActorKey.parse(row['requester_actor_key'])
                if row['state'] == 'result_ready' and row['result_body_json'] and row['plan_id'] is None:
                    plan = self.outbox.enqueue(token,
                        rendered=render_plan(body_from_json(row['result_body_json']), endpoint), requester=actor,
                        logical_key='qq-turn-result:' + row['execution_id'], source_scope_key=row['scope_key'],
                        expires_at=max(now + 300, row['deadline_at']),
                        source_native_message_id=row['source_native_message_id'],
                        source_canonical_message_id=row['canonical_message_id'], transaction_cursor=cursor)
                    cursor.execute("UPDATE qq_official_turn_executions SET state='handed_off',plan_id=?,handed_off_at=?,updated_at=? WHERE execution_id=?",
                        (plan.plan_id, now, now, row['execution_id']))
                    continue
                if endpoint.key not in notified and self.store.admitted(cursor, endpoint, actor):
                    self.outbox.enqueue(token, rendered=render_plan(MessageBody((TextNode(0, notice),)), endpoint),
                        requester=actor, logical_key='restart:' + row['execution_id'],
                        source_scope_key=row['scope_key'], expires_at=now + 300,
                        source_native_message_id=row['source_native_message_id'],
                        source_canonical_message_id=row['canonical_message_id'], transaction_cursor=cursor)
                    notified.add(endpoint.key)
                if row['journal_turn_id'] is not None:
                    self.journal.finish_turn(row['journal_turn_id'], status='crashed',
                        final_text=notice, finished_at=now, transaction_cursor=cursor)
                cursor.execute('''UPDATE qq_official_turn_executions SET state=CASE WHEN plan_id IS NULL THEN 'cancelled' ELSE 'handed_off' END,
                    reason_code='restart_notified',updated_at=? WHERE execution_id=?''', (now, row['execution_id']))
            if rows:
                self.logger.info(f'QQ restart recovery: closed={len(rows)} conversations={len(notified)}')

    async def _optional_feature(self, name, factory, *args):
        try:
            result = factory(*args)
            return await result if inspect.isawaitable(result) else result
        except (LeaseLost, DatabaseError):
            raise
        except Exception as error:
            frames = [(f.name,f.lineno) for f in traceback.extract_tb(error.__traceback__)[-4:]]
            self.logger.warning(f'QQ feature disabled feature={name} error={type(error).__name__} frames={frames}')
            return None

    async def _deliver_binding(self, token, effect):
        outcome = "failed"
        try:
            await self._ensure_current(token)
            if effect.expires_at and effect.expires_at <= int(self.store.clock()):
                outcome = "failed"
            elif self.binding_sender is not None or self.sender is not None:
                # Only an explicit caller-supplied transport receives the secret
                # effect. The runtime never logs, serializes or retries this call.
                timeout = min(30, max(0.01, effect.expires_at - self.store.clock())) if effect.expires_at else 30
                async with asyncio.timeout(timeout):
                    target = self.binding_sender if self.binding_sender is not None else self.sender.emit_binding
                    outcome = await target(effect)
                if outcome not in {"success", "failed", "unknown"}:
                    outcome = "unknown"
        except asyncio.CancelledError:
            raise
        except LeaseLost:
            raise
        except Exception as error:
            frames = [(f.name,f.lineno) for f in traceback.extract_tb(error.__traceback__)[-4:]]
            self.logger.warning(f'QQ binding send error={type(error).__name__} frames={frames}')
            outcome = "unknown"
        if effect.kind == "issue_code":
            def mark():
                with self.store.transaction() as cursor:
                    self.store.binding.mark_issued(cursor, account=self.loaded.account,
                        challenge_id=effect.challenge_id, outcome=outcome, lease_token=token)
            await self._calls(mark)

    async def resume(self):
        """Explicit trusted local control action; never exposed as a chat tool."""
        if self._stopping or not self._started or self.state != "paused":
            return False
        await self._idle.wait()
        if self._stopping:
            return False

        def unpause():
            with self.store.transaction() as cursor:
                # A lost connection can leave diagnostic holder columns behind.
                # The actual advisory lock, across schemas, proves nobody owns
                # the account before this trusted action clears that stale row.
                locked = cursor.execute("SELECT pg_try_advisory_xact_lock(?) AS acquired",
                    (advisory_key_for(self.loaded.account),)).fetchone()
                if not locked["acquired"]:
                    return False
                cursor.execute("""UPDATE qq_official_accounts SET gateway_state='idle',pause_reason=NULL,
                    holder_id=NULL,lock_backend_pid=NULL,config_revision=config_revision+1,updated_at=?
                    WHERE account_key=? AND enabled=TRUE""",
                    (int(self.store.clock()), self.loaded.account.key))
                return cursor.rowcount == 1

        if not await self._calls(unpause):
            return False
        self._resume.set()
        return True

    async def stop(self):
        self._stopping = True
        async with self._lifecycle_lock:
            await self._stop_locked()

    async def _stop_locked(self):
        self._resume.set()
        if self._gateway is not None:
            try:
                await self._gateway.stop()
            except Exception:
                # Closing a broken socket must not bypass worker cancellation,
                # database-thread draining, lease release or store cleanup.
                self.logger.warning("Official gateway close failed; draining account runtime.")
        await self._supervisor.stop_all()
        await self._calls.close()
        await self._close_stores()
        self.state, self.reason = "stopped", "stopped"

    async def _close_stores(self):
        stores = [self.store, self.jobs, self.deliveries, self.ledger if self._owns_ledger else None,
                  self.journal if self._owns_journal else None]
        for store in stores:
            if store is not None:
                await asyncio.to_thread(store.close)
        self.store = self.jobs = None
        self.deliveries = self.outbox = self.sender = None
        if self._owns_ledger:
            self.ledger = None
        if self._owns_journal:
            self.journal = None
