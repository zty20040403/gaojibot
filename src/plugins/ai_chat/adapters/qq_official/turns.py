"""Run ordinary official turns directly and record the shared journal."""
from __future__ import annotations

import time
from contextlib import contextmanager
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
import asyncio
import logging
import traceback
from src.plugins.ai_chat.agent_loop import FinalStreamState
from src.plugins.ai_chat.agent.control import LeaseLost as ToolLeaseLost
from src.plugins.ai_chat.adapters.qq_official.lease import LeaseLost
from src.plugins.ai_chat.platform_types import SendRequest
from .commands import route_command
from .turn_source import load_turn_input
from .turn_platform import ExecutionBinding, OfficialTurnPlatform
from .sections import ReplyStream
from contextvars import ContextVar
from src.plugins.ai_chat.application.commands import CommandPolicy, CommandServices, parse_command
from src.plugins.ai_chat.application.platform_conversation import build_platform_chat_orchestrator
from src.plugins.ai_chat.application.platform_chat import PlatformChatService
from src.plugins.ai_chat.conversation_keys import conversation_key
from src.plugins.ai_chat.model_catalog import SUPPORTED_REASONING_EFFORTS
from src.plugins.ai_chat.platform_types import EndpointKey
from . import turn_source as execution
from .command_fence import guarded_services
from .commands import usage_callback
from .lease import assert_epoch
from .turn_platform import TransportState


class LifecycleError(RuntimeError):
    """Fixed, operator-safe diagnostic."""


class OfficialTurnLifecycle:
    def __init__(self, *, api, token, handle, job, admission, transactions, journal,
                 database_calls, usage=None, clock=time.time):
        self.api, self.token, self.handle, self.job = api, token, handle, job
        self.admission, self.transactions, self.journal = admission, transactions, journal
        self._calls, self.usage, self.clock = database_calls, usage, clock

    def args(self):
        return dict(token=self.token, handle=self.handle, job=self.job,
                    admission=self.admission, now=int(self.clock()))

    def _current(self, cursor):
        return self.api.assert_execution_current(cursor, **self.args())

    async def assert_current(self):
        def check():
            with self.transactions.transaction() as cursor:
                row = self._current(cursor)
                if int(self.clock()) >= row["deadline_at"]:
                    raise LifecycleError("turn_deadline_expired")
        await self._calls(check)

    @contextmanager
    def effect_scope(self, inbound):
        """Hold both execution fences across one synchronous command effect.

        The command was marked executing before this boundary. A separate
        settings-store commit is not claimed to be atomic with its reply;
        interruption retains needs_attention and cannot replay the effect.
        """
        if (inbound.endpoint != self.handle.endpoint or inbound.actor != self.handle.actor
                or inbound.route_epoch != self.handle.route_epoch or inbound.scope.key != self.handle.scope_key
                or inbound.native_message_id != self.handle.source_native_message_id):
            raise LifecycleError("execution_input_mismatch")
        with self.transactions.transaction() as cursor:
            row = self._current(cursor)
            if row["state"] != "executing" or int(self.clock()) >= row["deadline_at"]:
                raise LifecycleError("execution_effect_not_current")
            yield cursor

    async def begin(self, *, inbound, selected_profile, objective, prompt_version):
        def begin():
            with self.transactions.transaction() as cursor:
                row = self._current(cursor)
                if row["state"] != "prepared" or row["execution_kind"] != "chat":
                    raise LifecycleError("execution_not_prepared")
                turn = self.journal.start_turn(inbound.scope,
                    trigger_canonical_message_id=self.handle.canonical_message_id, objective=objective,
                    provider=selected_profile.provider_identity, model=selected_profile.model,
                    profile=selected_profile.name, prompt_version=prompt_version,
                    started_at=int(self.clock()), transaction_cursor=cursor)
                self.api.begin_execution(cursor, **self.args(), journal_turn_id=turn.turn_id)
                return turn.turn_id
        return await self._calls(begin)

    async def finish(self, *, turn_id, status, trace, final_text, error_code, scope_key):
        if scope_key != self.handle.scope_key or not isinstance(final_text, str):
            raise LifecycleError("execution_result_mismatch")
        body = MessageBody() if status in {"silence", "aborted", "cancelled"} else MessageBody((TextNode(0, final_text),))

        def finish():
            with self.transactions.transaction() as cursor:
                self._current(cursor)
                changed = self.api.capture_result(cursor, **self.args(), body=body, turn_id=turn_id, status=status)
                if not changed:
                    return
                if turn_id is not None:
                    journal_row = cursor.execute("SELECT status FROM agent_turns WHERE turn_id=? FOR UPDATE", (turn_id,)).fetchone()
                    if journal_row is None or journal_row["status"] != "running":
                        raise LifecycleError("execution_journal_not_running")
                    payload = trace.to_payload() if trace is not None else {}
                    if error_code:
                        payload["failure"] = {"code": error_code}
                    if trace is not None and trace.model:
                        cursor.execute("UPDATE agent_turns SET provider=?,model=?,profile=? WHERE turn_id=?",
                            (trace.provider, trace.model, trace.profile, turn_id))
                    recorded = self.journal.finish_turn(turn_id, status=status, final_text=final_text,
                        trace_payload=payload or None, input_tokens=trace.input_tokens if trace else 0,
                        output_tokens=trace.output_tokens if trace else 0,
                        total_tokens=trace.total_tokens if trace else 0,
                        finished_at=int(self.clock()), transaction_cursor=cursor)
                    if recorded is None:
                        raise LifecycleError("execution_journal_missing")
                if self.usage is not None and trace is not None and (trace.input_tokens > 0 or trace.output_tokens > 0):
                    self.usage.record(scope_key=scope_key, source="turn", provider=trace.provider,
                        model=trace.model, input_tokens=trace.input_tokens, output_tokens=trace.output_tokens,
                        cached_tokens=trace.cached_tokens, turn_id=turn_id,
                        occurred_at=int(self.clock()), transaction_cursor=cursor)
        await self._calls(finish)

    async def interrupted(self, *, turn_id, reason):
        def interrupt():
            with self.transactions.transaction() as cursor:
                row = self._current(cursor)
                if row["journal_turn_id"] != turn_id:
                    raise LifecycleError("execution_journal_mismatch")
                return self.api.interrupt_execution(cursor, **self.args(), reason=reason)
        return await self._calls(interrupt)


class OfficialTurnConsumer:
    """One account owner; existing queue leases remain the sole scheduler.

    Only a prepared execution may enter the shared orchestrator. Every retry
    first reads durable execution state; result_ready retries only its handoff.
    Runtime callbacks and stores are trusted local objects, never model input.
    """

    def __init__(self, *, api, token, store, outbox, journal, database_calls,
                 orchestrator, command_services, transport_state, usage=None,
                 sender_wake=None, policy=None, clock=time.time,
                 model_timeout_seconds=600, business_services=None):
        if type(model_timeout_seconds) is not int or not 1 <= model_timeout_seconds <= 3600:
            raise ValueError("invalid_model_timeout")
        self.api, self.token, self.store, self.outbox = api, token, store, outbox
        self.journal, self._calls, self.orchestrator = journal, database_calls, orchestrator
        self.command_services, self.transport_state, self.usage = command_services, transport_state, usage
        self.sender_wake, self.policy, self.clock = sender_wake, policy, clock
        self.model_timeout_seconds = model_timeout_seconds
        self.business_services = business_services

    def _prepare(self, job):
        now = int(self.clock())
        with self.outbox.deliveries.transaction() as cursor:
            # claim_execution performs the authoritative full job/source fence.
            # These reads only select the immutable deadline and parser branch.
            row = cursor.execute("""SELECT i.received_at,i.turn_text,i.turn_text_version,x.deadline_at,x.execution_kind
                FROM qq_official_inbound_events i JOIN messages m ON m.canonical_message_id=i.canonical_message_id
                LEFT JOIN qq_official_turn_executions x ON x.inbox_id=i.inbox_id
                WHERE i.inbox_id=? AND i.account_key=? AND i.dispatch_job_id IS NOT DISTINCT FROM ?""",
                (job.payload.get("inbox_id"), self.token.account_key, job.job_id)).fetchone()
            if row is None:
                raise self.api.ExecutionError("turn_source_unavailable")
            if row["turn_text_version"] != 1 or not isinstance(row["turn_text"], str):
                raise self.api.ExecutionError("turn_input_unavailable")
            text = row["turn_text"]
            kind = "command" if text.lstrip().startswith(("/", "!")) else "chat"
            if row["execution_kind"] is not None and row["execution_kind"] != kind:
                raise self.api.ExecutionError("turn_parser_identity_conflict")
            deadline = row["deadline_at"] or row["received_at"] + self.outbox.max_wait_seconds
            decision = self.api.claim_execution(cursor, token=self.token, job=job, now=now,
                deadline_at=deadline, admission=self.store.admitted, execution_kind=kind)
            if decision.handle is None:
                return decision, None, None
            inbound = load_turn_input(cursor, execution_api=self.api, token=self.token,
                handle=decision.handle, job=job, now=now, admission=self.store.admitted)
            return decision, inbound, text

    def _result(self, life):
        with self.outbox.deliveries.transaction() as cursor:
            row = self.api.assert_execution_current(cursor, **life.args())
            body = self.api.load_result(cursor, **life.args())
            if row["result_status"] in {"silence", "aborted", "cancelled"}:
                self.api.complete_without_plan(cursor, **life.args())
                return None
            return body

    async def __call__(self, job):
        try:
            return await self._execute(job)
        except asyncio.CancelledError:
            raise
        except (ToolLeaseLost, LeaseLost):
            logging.getLogger(__name__).warning('QQ turn stopped: account or permission changed')
            return {'status': 'cancelled'}
        except Exception as error:
            logging.getLogger(__name__).warning('QQ turn error=%s frames=%s', type(error).__name__,
                [(frame.name, frame.lineno) for frame in traceback.extract_tb(error.__traceback__)[-4:]])
            def report():
                from .renderer import render_plan
                from ...message_ir import MessageBody, TextNode
                from ...platform_types import ActorKey, EndpointKey
                with self.outbox.deliveries.transaction() as cursor:
                    row = cursor.execute('SELECT * FROM qq_official_turn_executions WHERE inbox_id=?', (job.payload['inbox_id'],)).fetchone()
                    if row is None:
                        return
                    endpoint, actor = EndpointKey.parse(row['endpoint_key']), ActorKey.parse(row['requester_actor_key'])
                    self.outbox.enqueue(self.token, rendered=render_plan(MessageBody((TextNode(0, '这次处理失败了，请再试一次。'),)), endpoint),
                        requester=actor, logical_key='turn-error:' + row['execution_id'], source_scope_key=row['scope_key'],
                        source_native_message_id=row['source_native_message_id'], source_canonical_message_id=row['canonical_message_id'],
                        expires_at=int(self.clock()) + 300, transaction_cursor=cursor)
                    if row['journal_turn_id'] is not None:
                        self.journal.finish_turn(row['journal_turn_id'], status='crashed', final_text='',
                            finished_at=int(self.clock()), transaction_cursor=cursor)
                    cursor.execute("UPDATE qq_official_turn_executions SET state='cancelled',reason_code='turn_failed',updated_at=? WHERE execution_id=? AND plan_id IS NULL",
                        (int(self.clock()), row['execution_id']))
            await self._calls(report)
            if self.sender_wake:
                self.sender_wake()
            return {'status': 'failed'}

    async def _execute(self, job):
        decision, inbound, text = await self._calls(self._prepare, job)
        if decision.handle is None:
            return {"status": decision.state, "reason": decision.reason}
        handle = decision.handle
        # Capture one callable: separate reads of a bound method create distinct
        # objects, while business effects require the exact same admission port.
        admission = self.store.admitted
        life = OfficialTurnLifecycle(api=self.api, token=self.token, handle=handle,
            job=job, admission=admission, transactions=self.outbox.deliveries,
            journal=self.journal, database_calls=self._calls, usage=self.usage, clock=self.clock)
        binding = ExecutionBinding(self.api, self.token, handle, job, admission)
        platform = OfficialTurnPlatform(inbound, execution=binding, outbox=self.outbox,
            database_calls=self._calls, transport_state=self.transport_state,
            sender_wake=self.sender_wake, policy=self.policy, clock=self.clock)
        if decision.state == "prepared":
            if await platform.ready(inbound.endpoint) is not None:
                raise RuntimeError("official_turn_platform_not_ready")
            try:
                if handle.execution_kind == "command":
                    async def admitted(_inbound):
                        await life.assert_current()
                        return True

                    async def before_effect(_name, _inbound):
                        def begin():
                            with self.outbox.deliveries.transaction() as cursor:
                                self.api.begin_command_execution(cursor, **life.args())
                        await self._calls(begin)

                    services = self.command_services(life, inbound) if callable(self.command_services) else self.command_services
                    result = await route_command(text, inbound, services=services,
                        admission=admitted, before_effect=before_effect,
                        services_factory=(lambda: self.business_services(life,inbound,platform,services))
                            if self.business_services is not None else None)
                    # The source-bound execution fence is the authoritative
                    # current admission check before all command callbacks.
                    if result.kind != "reply" or result.result is None:
                        await life.interrupted(turn_id=None, reason="command_route_suppressed")
                        return {"status": "suppressed"}
                    await life.finish(turn_id=None, status="succeeded", trace=None,
                        final_text=result.result.text, error_code="", scope_key=inbound.scope.key)
                else:
                    seconds = min(self.model_timeout_seconds, max(0, handle.deadline_at - self.clock()))
                    async with asyncio.timeout(seconds):
                        await self.orchestrator.run(inbound, platform, text, durable_lifecycle=life,
                            final_stream_sink=ReplyStream(platform), final_stream_state=FinalStreamState())
            except BaseException:
                try:
                    await life.interrupted(turn_id=None if handle.execution_kind == "command" else decision.journal_turn_id,
                        reason="consumer_execution_interrupted")
                except BaseException:
                    pass
                raise
        body = await self._calls(self._result, life)
        if body is None:
            return {"status": "silent"}
        outcome = await platform.send(SendRequest(endpoint=inbound.endpoint, actor=inbound.actor,
            body=body, delivery_id=self.api.logical_result_key(handle), route_epoch=handle.route_epoch,
            reply_credential=inbound.reply_credential, reply_to_native_message_id=inbound.native_message_id))
        if outcome.status == "failed":
            if outcome.reason in {"needs_attachment", "delivery_expired"}:
                return {"status": outcome.reason}
            raise RuntimeError("official_result_handoff_unavailable")
        # Delivery completion belongs to M3 receipts. Queueing does not claim
        # the user has received a response.
        return {"status": "handed_off"}


def build_turn_consumer(runtime, token, app):
    """Called by the authenticated runtime, never by a message or model tool."""
    def scope_enabled(scope):
        if scope.account != runtime.loaded.account:
            return False
        if app.model_preferences.get_scope_enabled_override(scope) is False:
            return False
        endpoint = EndpointKey(scope.account, scope.kind, scope.native_conversation_id)
        with runtime.deliveries.transaction() as cursor:
            assert_epoch(cursor, token)
            row = cursor.execute("SELECT binding_state,membership_state FROM qq_official_endpoints WHERE endpoint_key=?",
                                 (endpoint.key,)).fetchone()
            # A bound official group is locally enabled by default. This is
            # only a feature switch; execution still checks actual actor
            # admission, source identity and route/account/job fences.
            return bool(row and row["membership_state"] == "present"
                and (row["binding_state"] == "active" if endpoint.kind == "group"
                     else row["binding_state"] != "revoked"))

    async def memory_writer(inbound, platform, ports, user_text, answer):
        def commit():
            if (platform.execution.token is not token or ports.conversation_id != conversation_key(inbound)):
                raise ToolLeaseLost("official_memory_scope_changed")
            with runtime.deliveries.transaction() as cursor:
                row = platform.execution.current(cursor, int(runtime.store.clock()))
                if row["state"] != "executing" or row["deadline_at"] <= int(runtime.store.clock()):
                    raise ToolLeaseLost("official_memory_execution_unavailable")
                # Hold the account/job fences through the existing synchronous
                # memory write; do not resume a second model turn on failure.
                app.memory.append_turn(ports.conversation_id, user_text, answer)
        await runtime._calls(commit)

    service_options = {"memory_writer": memory_writer, "scope_enabled": scope_enabled}
    background = getattr(runtime, 'background_service', None)
    if background is not None:
        service_options['native_background'] = background
    migration = getattr(runtime,'migration_service',None)
    if migration is not None:
        service_options['public_history_factory'] = migration.public_history
        service_options['project_factory'] = migration.project_context
    media = getattr(runtime, 'media_service', None)
    if media is not None:
        service_options['media_factory'] = media.foreground
    files = getattr(runtime, 'file_service', None)
    if files is not None:
        service_options['generation_factory'] = files.generation.foreground
    subagents = runtime.subagent_service
    if subagents is not None and app.settings.subagent_entry_enabled:
        from .subagent_actions import task_registry_for
        service_options.update(subagent_actions_factory=subagents.actions_for, registry_factory=task_registry_for)
    service = PlatformChatService(app, **service_options)
    orchestrator = build_platform_chat_orchestrator(app, service=service)

    def command_services(life, inbound):
        active_cursor = ContextVar("official_command_cursor", default=None)

        @contextmanager
        def effect_scope(current):
            with life.effect_scope(current) as cursor:
                marker = active_cursor.set(cursor)
                try:
                    yield cursor
                finally:
                    active_cursor.reset(marker)

        def policy(cursor, *, group_default=None):
            # These are already host-configured profiles. Personal preferences
            # stay within the actual endpoint/actor; only a current local owner
            # may change that endpoint's group defaults.
            profiles = tuple(profile.name for profile in app.model_catalog.profiles if profile.configured)
            owner = runtime.store.authority.owner_for_actor(cursor, inbound.endpoint, inbound.actor)
            if group_default is None:
                source = cursor.execute("""SELECT turn_text FROM qq_official_inbound_events
                    WHERE inbox_id=? AND account_key=?""", (life.handle.inbox_id, token.account_key)).fetchone()
                parsed = parse_command(source["turn_text"]) if source is not None else None
                words = parsed.argument.split(None, 1) if parsed is not None else ()
                group_default = bool(words and words[0].casefold() in {"group", "群默认"})
            preference = None if group_default else app.model_preferences.get_explicit(conversation_key(inbound))
            if preference is None and inbound.endpoint.kind == "group":
                preference = (app.model_preferences.get_scope_default(inbound.endpoint.scope)
                    or app.settings.model_profile_for_scope(inbound.endpoint.scope))
            profile = app.model_catalog.resolve_preference(preference)
            # The same provider restriction applies in PlatformChatService and
            # the unchanged OneBot command. Unsupported settings must not be
            # reported as applied; clearing a stale override remains allowed.
            efforts = tuple(SUPPORTED_REASONING_EFFORTS) if profile.configured and profile.provider in {"openai", "cliproxy"} else ()
            return CommandPolicy(models=profiles, efforts=efforts,
                default_model=profile.name,
                default_effort=profile.reasoning_effort or "",
                allow_model_read=True, allow_model_change=True,
                allow_effort_read=True, allow_effort_change=True,
                allow_group_default=owner is not None)

        async def host_policy(_inbound):
            def read():
                with life.effect_scope(inbound) as cursor:
                    return policy(cursor)
            return await runtime._calls(read)

        def settings_authority(cursor, _inbound, domain, method, args):
            # Recompute from the actual storage operation at the effect fence,
            # including group defaults independently of an owner's own model.
            current = policy(cursor, group_default="_scope_" in method)
            if domain not in {"model", "effort"}:
                return False
            if method.startswith("get"):
                return True
            if "_scope_" in method and not current.allow_group_default:
                return False
            if method.startswith("set"):
                return args[1] in (current.models if domain == "model" else current.efforts)
            return method.startswith("clear")

        tasks = app.running_tasks
        if subagents is not None:
            from .subagent_commands import TaskRegistryAdapter
            tasks = TaskRegistryAdapter(tasks, token, active_cursor.get, runtime.store.admitted, runtime.store.clock)
        services = CommandServices(running_tasks=tasks,
            model_preferences=app.model_preferences, reasoning_preferences=app.reasoning_preferences,
            host_policy=host_policy, usage=usage_callback(runtime.deliveries.transaction, runtime._calls))
        return guarded_services(services, inbound, effect_scope=effect_scope,
            database_call=runtime._calls, settings_authority=settings_authority)

    def transport_state():
        gateway = runtime._gateway
        return TransportState(runtime.loaded.account, runtime.loaded.credentials is not None,
            runtime._token is token and gateway is not None and gateway.state == "ready")

    from .business_commands import build_business_services
    return OfficialTurnConsumer(api=execution, token=token, store=runtime.store,
        outbox=runtime.outbox, journal=runtime.journal, database_calls=runtime._calls,
        orchestrator=orchestrator, command_services=command_services,
        business_services=build_business_services(runtime,app),
        policy=runtime.loaded.delivery.turn_policy(),
        transport_state=transport_state, usage=app.usage_store,
        sender_wake=lambda: runtime.sender.wake() if runtime.sender is not None else None,
        clock=runtime.store.clock)
