"""Default-off official ingress lifecycle, independent of OneBot connections.

M2 persists turn intents; no model consumer or production binding sender is
registered here. M3/M4 will connect those explicit ports after their checks.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import re

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.lifecycle import BackgroundTaskSupervisor
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from src.plugins.ai_chat.workers.durable_jobs import DurableJobWorker, JobDeferred
from src.qq_official.gateway import GatewayConnection
from src.qq_official.http import RestClient
from src.qq_official.token import TokenManager

from .binding import BindingReducer
from .config import OfficialConfig
from .inbox import OfficialInboxStore
from .ingest import process_inbox
from .keys import account_job_kind
from .lease import DedicatedAccountLease, LeaseLost, LeaseUnavailable, advisory_key_for


class _FencedToken:
    def __init__(self, manager, fence):
        self._manager, self._fence = manager, fence
        self.credentials, self.base_url = manager.credentials, manager.base_url

    async def get(self):
        await self._fence()
        value = await self._manager.get()
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
    def __init__(self, config: OfficialConfig, database, *, logger, ledger=None,
                 protocol_factory=open_official_protocol, binding_sender=None,
                 lease_factory=DedicatedAccountLease, probe_seconds: float = 1,
                 retry_seconds: float = 2, allow_test_server: bool = False):
        self.config, self.database, self.logger = config, database, logger
        self.protocol_factory, self.binding_sender = protocol_factory, binding_sender
        self.lease_factory = lease_factory
        self.probe_seconds = min(max(probe_seconds, 0.01), 5)
        self.retry_seconds = min(max(retry_seconds, 0.01), 60)
        self.allow_test_server = allow_test_server
        self.ledger = ledger
        self._owns_ledger = ledger is None
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
            self.jobs = await self._calls(DurableJobStore, self.database)
            if self.ledger is None:
                self.ledger = await self._calls(MessageLedger, self.database)
            from .lease import assert_epoch
            binding = BindingReducer(self.loaded.binding_pepper, epoch_guard=assert_epoch)
            self.store = await self._calls(OfficialInboxStore, self.database, jobs=self.jobs, binding=binding,
                private_policy=self.loaded.private_policy, private_allowed_openids=self.loaded.private_allowed_openids,
                payload_retention_hours=self.loaded.payload_retention_hours)
            await self._calls(self.store.ensure_account, self.loaded.account)
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
                "turn_execution_connected": False, "binding_transport_connected": self.binding_sender is not None}

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
                        await self._calls(self.store.check_pending_ingest, token)
                        checkpoint = await self._calls(self.store.checkpoint, token)
                        await self._serve(lease, token, checkpoint)
                except asyncio.CancelledError:
                    raise
                except LeaseLost:
                    self.state, self.reason = "paused", "account_lease_lost"
                except Exception:
                    self.state, self.reason = "paused", "ingress_unavailable"
                    if token is not None:
                        try:
                            await self._calls(self.store.pause, token, "ingress_unavailable")
                        except Exception:
                            pass
                finally:
                    self._token = None
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
        ingest_failed = asyncio.Event()

        async def on_checkpoint(value):
            await self._calls(self.store.checkpoint_callback, token, value)

        async def on_dispatch(frame):
            nonlocal session_id
            if frame.get("t") == "READY":
                data = frame.get("d")
                session_id = data.get("session_id") if isinstance(data, dict) else None
            await self._ensure_current(token)
            accepted = await self._calls(self.store.accept, frame, token=token, session_id=session_id)
            if accepted.binding is not None and accepted.binding.effect is not None:
                await self._deliver_binding(token, accepted.binding.effect)

        async def ingest(job):
            inbox_id = job.payload.get("inbox_id")

            def execute():
                with self.store.transaction() as cursor:
                    return process_inbox(cursor, inbox_id=inbox_id, lease_token=token, ledger=self.ledger,
                        jobs=self.jobs, epoch_guard=self.store.epoch_guard, admission=self.store.admitted)

            try:
                await self._ensure_current(token)
                result = await self._calls(execute)
            except LeaseLost:
                raise JobDeferred("official_account_lease_lost", 3) from None
            except Exception:
                # Queue diagnostics are persisted and logged. Database errors
                # can include opaque IDs or payloads, so only a fixed code leaves.
                ingest_failed.set()
                # Preserve the durable continuation. A terminal failed job with
                # a pending inbox would block every later message in its lane.
                raise JobDeferred("official_ingest_unavailable", 0) from None
            if result.status == "deferred":
                raise JobDeferred("official_endpoint_order", 1)
            return {"status": result.status, "inbox_id": inbox_id}

        async def monitor():
            loop = asyncio.get_running_loop()
            next_purge = 0.0
            while not self._stopping:
                if not await self._calls(lease.probe):
                    raise LeaseLost("Official account lease was lost")
                if loop.time() >= next_purge:
                    await self._calls(self.store.check_pending_ingest, token)
                    await self._calls(self.store.purge_terminal_payloads, token)
                    next_purge = loop.time() + 60
                await asyncio.sleep(self.probe_seconds)

        async def failure():
            await ingest_failed.wait()
            raise RuntimeError("official_ingest_unavailable")

        worker = DurableJobWorker(self.jobs, logger=self.logger, poll_seconds=0.1,
                                  concurrency=2, per_scope_limit=1, database_calls=self._calls)
        worker.register(account_job_kind("ingest", self.loaded.account.key), ingest)
        async with self.protocol_factory(self.loaded, checkpoint=checkpoint, on_checkpoint=on_checkpoint,
                                         ensure_current=lambda: self._ensure_current(token)) as gateway:
            self._gateway = gateway
            self.state, self.reason = "running", "receiving_durable_events"
            tasks = [asyncio.create_task(gateway.run(on_dispatch)), asyncio.create_task(worker.run_forever()),
                     asyncio.create_task(monitor()), asyncio.create_task(failure())]
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
                self._gateway = None
                self._token = None
                try:
                    await gateway.stop()
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)

    async def _deliver_binding(self, token, effect):
        outcome = "failed"
        try:
            await self._ensure_current(token)
            if effect.expires_at and effect.expires_at <= int(self.store.clock()):
                outcome = "failed"
            elif self.binding_sender is not None:
                # Only an explicit caller-supplied transport receives the secret
                # effect. The runtime never logs, serializes or retries this call.
                timeout = min(30, max(0.01, effect.expires_at - self.store.clock())) if effect.expires_at else 30
                async with asyncio.timeout(timeout):
                    outcome = await self.binding_sender(effect)
                if outcome not in {"success", "failed", "unknown"}:
                    outcome = "unknown"
        except asyncio.CancelledError:
            raise
        except LeaseLost:
            raise
        except Exception:
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
        stores = [self.store, self.jobs, self.ledger if self._owns_ledger else None]
        for store in stores:
            if store is not None:
                await asyncio.to_thread(store.close)
        self.store = self.jobs = None
        if self._owns_ledger:
            self.ledger = None
