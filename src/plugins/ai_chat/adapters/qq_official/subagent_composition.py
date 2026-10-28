"""Bind the existing text Sub-Agent engine to an official account runtime."""
from __future__ import annotations

from types import SimpleNamespace
import asyncio

from src.plugins.ai_chat.agent.control import assert_job_owned, LeaseLost
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.message_ir import body_from_json
from src.plugins.ai_chat.message_lowering import OutboundCapabilities
from src.plugins.ai_chat.platform_types import InboundMessage, PlatformFeatures, SendOutcome, EffectiveCapabilities
from src.plugins.ai_chat.tools import neutral_registry_for
from src.plugins.ai_chat.tools.state import ToolContext, TurnState
from src.plugins.ai_chat.workers.durable_jobs import JobDeferred

from . import execution
from .subagent_actions import SubmissionBinding, text_coordinator
from .subagent_runtime import OfficialSubAgentDispatcher, TEXT_TOOLS, assert_current


class TaskPlatform:
    """Read-only tool context. Final results are dispatched by the task worker."""

    features = PlatformFeatures()
    capabilities = OutboundCapabilities(text=False)
    stream_enabled = False
    progress_enabled = False

    def __init__(self, endpoint):
        self.endpoint, self.account = endpoint, endpoint.account

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
    def __init__(self, runtime, token, app):
        self.runtime, self.token, self.app = runtime, token, app
        original = app.subagent_coordinator
        self.coordinator = text_coordinator(app.subagent_store, app.model_catalog, logger=app.logger,
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
            result_wait_seconds=min(self.runtime.outbox.max_wait_seconds, 86400), clock=self.runtime.store.clock)
        return binding.actions_for(turn, ports, selected_profile, prepared, parent_trace)

    def _restore(self, job):
        with self.runtime.deliveries.transaction() as cursor:
            binding, task = assert_current(cursor, token=self.token, task_id=job.payload["task_id"],
                now=int(self.runtime.store.clock()), admission=self.runtime.store.admitted, job=job)
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
            tools=tools, execute_tool=execute_tool, clock=self.runtime.store.clock)

    async def execute(self, job):
        try:
            return await self._execute(job)
        except LeaseLost:
            return await self._dispatcher().authority_lost(job)
        except (asyncio.CancelledError, JobDeferred):
            raise
        except Exception:
            raise JobDeferred("official_subagent_context_unavailable", 3) from None

    async def _execute(self, job):
        inbound, objective = await self.runtime._calls(self._restore, job)
        capabilities = {"self", "context"}
        if self.app.long_term_memory is not None:
            capabilities.add("memory")
        if self.app.settings.search_enabled:
            capabilities.add("web")
        context = ToolContext(SimpleNamespace(context=self.app), event=None,
            capabilities=frozenset(capabilities), inbound=inbound, platform=TaskPlatform(inbound.endpoint))
        turn = TurnState(context, user_text=objective)
        registry = await self.runtime._calls(neutral_registry_for, context)
        definitions = await self.runtime._calls(registry.definitions, context)
        definitions = tuple(item for item in definitions if item["function"]["name"] in TEXT_TOOLS)

        async def execute_tool(name, arguments):
            assert_job_owned()
            if name not in TEXT_TOOLS:
                return '{"ok":false,"error":"tool_unavailable"}'
            return await registry.execute(turn, name, arguments)

        result = await self._dispatcher(tools=definitions, execute_tool=execute_tool).execute(job)
        if self.runtime.sender is not None:
            self.runtime.sender.wake()
        return result

    async def reconcile(self):
        items = await self._dispatcher().reconcile_results(after_task_id=self.cursor, limit=20)
        self.cursor = items[-1][0] if len(items) == 20 else 0
        if any(item[1] == "handed_off" for item in items) and self.runtime.sender is not None:
            self.runtime.sender.wake()
