"""Bind the existing text Sub-Agent engine to an official account runtime."""
from __future__ import annotations

from types import SimpleNamespace
import asyncio

from src.plugins.ai_chat.agent.control import assert_job_owned, LeaseLost
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.message_ir import body_from_json
from src.plugins.ai_chat.message_lowering import OutboundCapabilities
from src.plugins.ai_chat.platform_types import BackgroundToolSource, InboundMessage, PlatformFeatures, SendOutcome, EffectiveCapabilities
from src.plugins.ai_chat.tools import neutral_registry_for
from src.plugins.ai_chat.tools.state import ToolContext, TurnState
from src.plugins.ai_chat.workers.durable_jobs import JobDeferred

from . import execution
from .subagent_actions import SubmissionBinding, text_coordinator, file_coordinator
from .subagent_runtime import OfficialSubAgentDispatcher, TaskToolSession, FILE_TOOLS, TEXT_TOOLS, assert_current


class TaskPlatform:
    """Read-only tool context. Final results are dispatched by the task worker."""

    features = PlatformFeatures()
    capabilities = OutboundCapabilities(text=False)
    stream_enabled = False
    def __init__(self, endpoint, progress=None):
        self.endpoint, self.account = endpoint, endpoint.account
        self.progress = progress
        self.progress_enabled = progress is not None

    async def ready(self, endpoint):
        if endpoint != self.endpoint:
            return "task_endpoint_mismatch"
        assert_job_owned()
        return None

    async def effective_capabilities(self, endpoint):
        if endpoint != self.endpoint:
            raise LeaseLost("task_endpoint_mismatch")
        assert_job_owned()
        return EffectiveCapabilities(endpoint, 0, {})

    async def send(self, request):
        return SendOutcome("failed", reason="background_results_require_outbox")

    async def recall(self, endpoint, native_message_id):
        return False


class OfficialSubAgentService:
    def __init__(self, runtime, token, app, *, migration_authority=None):
        self.runtime, self.token, self.app = runtime, token, app
        self.migration_authority = migration_authority
        original = app.subagent_coordinator
        files = getattr(runtime, 'file_service', None)
        self.approval = getattr(runtime, 'approval_service', None)
        self.projects = getattr(runtime, 'project_service', None)
        coordinator_factory = file_coordinator if files is not None else text_coordinator
        if self.approval is not None and self.approval.host_session_factory is not None:
            from .host_session import host_coordinator
            from functools import partial
            coordinator_factory = partial(host_coordinator, files_enabled=files is not None)
        self.coordinator = coordinator_factory(app.subagent_store, app.model_catalog, logger=app.logger,
            max_steps=app.settings.subagent_max_steps, max_parallelism=app.settings.subagent_max_parallelism,
            max_tool_rounds=app.settings.subagent_max_tool_rounds,
            timeout_seconds=app.settings.subagent_timeout_seconds,
            profile_overrides=original.profile_overrides)
        self.cursor = 0

    def actions_for(self, turn, ports, selected_profile, prepared, parent_trace=None):
        platform = turn.context.platform
        if platform is None or platform.execution.token is not self.token:
            raise LeaseLost("subagent_parent_account_changed")
        authority = platform.execution
        now = int(self.runtime.store.clock())
        binding = SubmissionBinding(token=self.token, source_api=execution,
            source_handle=authority.handle, source_job=authority.job,
            transaction=self.runtime.deliveries.transaction, database_calls=self.runtime._calls,
            admission=self.runtime.store.admitted, coordinator=self.coordinator, jobs=self.runtime.jobs,
            execution_deadline=min(authority.handle.deadline_at,
                now + min(max(int(self.app.settings.subagent_timeout_seconds), 1), 7200)),
            result_wait_seconds=min(self.runtime.outbox.max_wait_seconds, 86400), clock=self.runtime.store.clock,
            host_admission=self.approval.host_admission if self.approval is not None else None)
        return binding.actions_for(turn, ports, selected_profile, prepared, parent_trace)

    def _restore(self, job):
        with self.runtime.deliveries.transaction() as cursor:
            binding, task = assert_current(cursor, token=self.token, task_id=job.payload["task_id"],
                now=int(self.runtime.store.clock()), admission=self.runtime.store.admitted, job=job,
                migration_authority=self.migration_authority)
            if binding.get("migration_item_id") is not None:
                source = self.migration_authority.tool_source(cursor, task_id=task["task_id"],
                    job=job, now=int(self.runtime.store.clock()))
                return source, task["objective"]
            source = cursor.execute("""SELECT m.*,c.scope_key FROM messages m
                JOIN conversations c USING(conversation_id)
                WHERE m.canonical_message_id=?""", (binding["source_canonical_message_id"],)).fetchone()
            record = self.coordinator.store._task_row(task)
            if (source is None or source["source_endpoint_key"] != record.endpoint.key
                    or source["sender_actor_key"] != record.requester_actor.key
                    or source["native_message_id"] != binding["source_native_message_id"]
                    or source["scope_key"] != record.scope_key or source["direction"] != "inbound"):
                raise LeaseLost("subagent_source_changed")
            scope = ConversationScope.from_key(record.scope_key)
            inbound = InboundMessage(endpoint=record.endpoint, actor=record.requester_actor,
                event_id=None, native_message_id=source["native_message_id"],
                sender_display=source["sender_display"], platform_role="",
                body=body_from_json(source["body_json"]), mentions_bot=False,
                reply_to_native_message_id=None, occurred_at=source["occurred_at"],
                reply_credential=None,
                canonical_scope=scope if scope != record.endpoint.scope else None,
                route_epoch=binding["route_epoch"])
            return inbound, record.objective

    def _dispatcher(self, *, tools=(), execute_tool=None):
        return OfficialSubAgentDispatcher(token=self.token, coordinator=self.coordinator,
            jobs=self.runtime.jobs, outbox=self.runtime.outbox,
            transaction=self.runtime.deliveries.transaction, database_calls=self.runtime._calls,
            admission=self.runtime.store.admitted, profile=self.app.model_catalog.try_resolve,
            tools=tools, execute_tool=execute_tool, clock=self.runtime.store.clock,
            execution_factory=self._prepare_tool_session, migration_authority=self.migration_authority,
            project_finalizer=self.projects.finalize if self.projects is not None else None,
            project_authority=self.projects.assert_current if self.projects is not None else None,
            present_result=self._present_result)

    async def _present_result(self, job, task, previous_session=None):
        if task.result.get('frontend_answer'):
            return
        from .task_result import present_task_result
        control = await self.runtime._calls(self.coordinator.store.control, task.task_id)
        profile = self.app.model_catalog.try_resolve(control['dispatch'].get('profile'))
        from ...subagents import _checkpoint_context_packet
        checkpoints = await self.runtime._calls(self.coordinator.store.checkpoints, task.task_id)
        context = _checkpoint_context_packet(checkpoints)
        session = None
        try:
            if task.status != 'cancelled':
                files = control['dispatch'].get('tool_profile') in {'official-files-v1', 'official-projects-v1'}
                if task.project_id is None and control['dispatch'].get('tool_profile') != 'official-host-v1':
                    session = await self._prepare_tool_session(job, task, files)
                # Result delivery may read the workspace and send attachments, but
                # does not reopen host or project mutations after terminal state.
                if session is not None:
                    session.tools = tuple(item for item in session.tools if item['function']['name'] in TEXT_TOOLS | FILE_TOOLS)
            answer = await present_task_result(task, profile=profile, session=session,
                context=context.as_payload() if context is not None else None)
            await self.runtime._calls(self.coordinator.store.set_task_state, task.task_id, task.status,
                result={**task.result, 'answer': answer, 'frontend_answer': True}, error=task.last_error)
        finally:
            if session is not None and session.close is not None:
                await session.close()

    async def execute(self, job):
        try:
            return await self._execute(job)
        except LeaseLost:
            return await self._dispatcher().authority_lost(job)
        except (asyncio.CancelledError, JobDeferred):
            raise
        except Exception:
            raise JobDeferred("official_subagent_context_unavailable", 3) from None

    def _progress_for(self, job, task):
        from .progress import ProgressContext, ProgressEmitter
        from ...platform_types import EndpointKey, ActorKey
        def current(cursor, now):
            binding, row = assert_current(cursor, token=self.token, task_id=task.task_id,
                now=now, admission=self.runtime.store.admitted, job=job,
                migration_authority=self.migration_authority,
                project_authority=self.projects.assert_current if self.projects is not None else None)
            return ProgressContext("task:" + str(task.task_id) + ":1",
                EndpointKey.parse(binding["endpoint_key"]), ActorKey.parse(binding["requester_actor_key"]),
                row["scope_key"], max(binding["execution_deadline_at"], now + 600),
                binding["source_native_message_id"] or "", binding["source_canonical_message_id"])
        return ProgressEmitter(token=self.token, outbox=self.runtime.outbox, current=current,
            database_calls=self.runtime._calls,
            wake=lambda: self.runtime.sender.wake() if self.runtime.sender is not None else None)

    async def _prepare_tool_session(self, job, task, files_enabled):
        # Invoked by the dispatcher only while its actual job fence is active.
        def selected_profile():
            with self.runtime.deliveries.transaction() as cursor:
                row = cursor.execute('SELECT tool_profile FROM qq_official_subagent_dispatches WHERE task_id=?',
                    (task.task_id,)).fetchone()
                return row['tool_profile'] if row else None
        selected = await self.runtime._calls(selected_profile)
        progress = self._progress_for(job, task)
        if selected == 'official-projects-v1':
            if self.projects is None:
                raise JobDeferred('official_project_context_unavailable', 30)
            session = await self.projects.prepare(job,task,files_enabled)
            session.progress = progress
            return session
        if selected == 'official-host-v1':
            if self.approval is None or self.approval.host_session_factory is None:
                raise JobDeferred('official_host_context_unavailable', 3)
            session = await self.approval.host_session_factory(job, task, files_enabled)
            session.progress = progress
            return session
        inbound, objective = await self.runtime._calls(self._restore, job)
        capabilities = {"self", "context"}
        if self.app.long_term_memory is not None:
            capabilities.add("memory")
        if self.app.settings.search_enabled:
            capabilities.add("web")
        context = ToolContext(SimpleNamespace(context=self.app), event=None,
            capabilities=frozenset(capabilities),
            inbound=inbound if isinstance(inbound, InboundMessage) else None,
            background_source=inbound if isinstance(inbound, BackgroundToolSource) else None,
            platform=TaskPlatform(inbound.endpoint, progress=progress))
        file_session = None
        try:
            if files_enabled:
                files = getattr(self.runtime, 'file_service', None)
                if files is None:
                    raise JobDeferred('official_task_files_unavailable', 30)
                file_session = await files.generation.subagent(job, task)
                if file_session is None:
                    raise JobDeferred('official_task_files_unavailable', 30)
                context = file_session.context(context)
            turn = TurnState(context, user_text=objective)
            registry = await self.runtime._calls(neutral_registry_for, context)
            definitions = await self.runtime._calls(registry.definitions, context)
            allowed = TEXT_TOOLS | FILE_TOOLS if file_session is not None else TEXT_TOOLS
            definitions = tuple(item for item in definitions if item["function"]["name"] in allowed)

            async def execute_tool(name, arguments):
                assert_job_owned()
                if name not in allowed:
                    return '{"ok":false,"error":"tool_unavailable"}'
                return await registry.execute(turn, name, arguments)

            return TaskToolSession(definitions, execute_tool,
                hooks=file_session.hooks if file_session is not None else None,
                close=file_session.close if file_session is not None else None,
                files_enabled=file_session is not None, progress=progress)
        except BaseException:
            if file_session is not None:
                await file_session.close()
            raise

    async def _execute(self, job):
        result = await self._dispatcher().execute(job)
        if self.runtime.sender is not None:
            self.runtime.sender.wake()
        return result

    async def reconcile(self):
        items = await self._dispatcher().reconcile_results(after_task_id=self.cursor, limit=20)
        self.cursor = items[-1][0] if len(items) == 20 else 0
        if any(item[1] == "handed_off" for item in items) and self.runtime.sender is not None:
            self.runtime.sender.wake()
