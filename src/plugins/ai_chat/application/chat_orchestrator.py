from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from src.bot_storage import DatabaseCalls
from ..agent_loop import AgentTrace
from ..conversation_scope import ConversationScope
from ..platform_types import EndpointKey, InboundMessage, TurnPlatform
from ..tasks import LegacyTaskIdentity


class ApplicationLogger(Protocol):
    def info(self, message: object, *args: object, **kwargs: object) -> object: ...

    def warning(self, message: object, *args: object, **kwargs: object) -> object: ...


@dataclass(frozen=True)
class ChatTurnResult:
    reply: Any
    turn_id: int | None
    status: str = "succeeded"
    error_code: str = ""


class ChatFailure(Exception):
    """A failed turn with a safe message that may still be delivered to the user."""

    def __init__(self, reply: str, *, code: str) -> None:
        super().__init__(reply)
        self.reply = reply
        self.code = code


@dataclass(frozen=True)
class ChatPorts:
    """Platform and presentation operations needed by the chat use case."""

    conversation_id: Callable[[InboundMessage], str]
    endpoint_enabled: Callable[[EndpointKey], bool]
    group_default_profile: Callable[[InboundMessage], str | None]
    reply_target_turn: Callable[[InboundMessage], Any | None]
    record_trigger: Callable[[InboundMessage, ConversationScope], int | None]
    current_turn_context: Callable[[InboundMessage, int], Any]
    drain_feedback: Callable[[str], Any]
    ask_agent: Callable[..., Any]
    is_silence_reply: Callable[[Any], bool]
    journal_reply_text: Callable[[Any], str]


class DurableTurnLifecycle(Protocol):
    """Trusted adapter transaction hooks; never populated from model arguments."""

    async def assert_current(self) -> None: ...

    async def begin(self, *, inbound: InboundMessage, selected_profile: Any,
                    objective: str, prompt_version: str) -> int: ...

    async def finish(self, *, turn_id: int | None, status: str, trace: AgentTrace | None,
                     final_text: str, error_code: str, scope_key: str) -> None: ...

    async def interrupted(self, *, turn_id: int | None, reason: str) -> None: ...


class ChatOrchestrator:
    """Run one model-backed chat turn without knowing the chat platform SDK."""

    def __init__(
        self,
        *,
        ports: ChatPorts,
        running_tasks: Any,
        model_preferences: Any,
        model_catalog: Any,
        message_ledger: Any = None,
        turn_journal: Any = None,
        usage_store: Any = None,
        logger: ApplicationLogger,
        prompt_version: str,
        simple_chat_profile: str = "",
        db_call: DatabaseCalls | None = None,
    ) -> None:
        self.ports = ports
        self.running_tasks = running_tasks
        self.model_preferences = model_preferences
        self.model_catalog = model_catalog
        self.message_ledger = message_ledger
        self.turn_journal = turn_journal
        self.usage_store = usage_store
        self.logger = logger
        self.prompt_version = prompt_version
        self.simple_chat_profile = str(simple_chat_profile).strip()
        self.db_call = db_call if db_call is not None else DatabaseCalls()

    async def run(
        self,
        inbound: InboundMessage,
        platform: TurnPlatform,
        user_text: str,
        *,
        legacy_task_identity: LegacyTaskIdentity | None = None,
        durable_lifecycle: DurableTurnLifecycle | None = None,
        **kwargs: Any,
    ) -> ChatTurnResult | None:
        if platform.account != inbound.endpoint.account:
            raise ValueError("turn platform does not own the inbound endpoint")
        if durable_lifecycle is not None:
            await durable_lifecycle.assert_current()
        conversation_id = self.ports.conversation_id(inbound)
        scope = inbound.scope
        stream_context = kwargs.pop("_stream_context", None)
        if self.usage_store is not None:
            quota = await self.db_call(self.usage_store.status, scope.key)
            if not quota.allowed:
                reply = "今天这个会话的模型额度已经用完了。可以在本机管理页调整配额，或者明天再继续。"
                if durable_lifecycle is not None:
                    await durable_lifecycle.assert_current()
                    await durable_lifecycle.finish(turn_id=None, status="succeeded", trace=None,
                        final_text=reply, error_code="model_quota", scope_key=scope.key)
                return ChatTurnResult(reply=reply, turn_id=None)

        task = self.running_tasks.register_inbound(
            conversation_id=conversation_id,
            inbound=inbound,
            legacy_identity=legacy_task_identity,
            summary=user_text,
        )
        kwargs.setdefault(
            "feedback_provider",
            lambda: self.ports.drain_feedback(task.task_id),
        )
        journal_turn_id: int | None = None
        trace: AgentTrace | None = None

        async def finish(status, text="", error_code=""):
            if durable_lifecycle is not None:
                await durable_lifecycle.assert_current()
                await durable_lifecycle.finish(turn_id=journal_turn_id, status=status, trace=trace,
                    final_text=text, error_code=error_code, scope_key=scope.key)
            else:
                await self.db_call(self._finish_turn, journal_turn_id, status, trace, text,
                                   scope_key=scope.key, error_code=error_code)

        async def interrupted(reason):
            if durable_lifecycle is not None:
                try:
                    await durable_lifecycle.interrupted(turn_id=journal_turn_id, reason=reason)
                except BaseException:
                    # The original failure/cancellation must escape. A stale
                    # lease cannot be reclaimed merely to write a terminal row.
                    pass

        try:
            caller_selected_profile = "selected_profile_override" in kwargs
            explicit_profile = await self.db_call(
                self.model_preferences.get_explicit, conversation_id
            )
            group_default_profile = await self.db_call(
                self.ports.group_default_profile, inbound
            )
            selected_profile = self.model_catalog.resolve_preference(
                explicit_profile or group_default_profile
            )
            simple_chat_routing_allowed = (
                not caller_selected_profile
                and explicit_profile is None
                and group_default_profile is None
            )
            if explicit_profile is None and group_default_profile is None:
                previous_turn = await self.db_call(self.ports.reply_target_turn, inbound)
                if previous_turn is not None:
                    inherited_profile = self.model_catalog.find_runtime(
                        profile=previous_turn.profile,
                        provider=previous_turn.provider,
                        model=previous_turn.model,
                    )
                    if inherited_profile is not None:
                        selected_profile = inherited_profile
                        simple_chat_routing_allowed = False
            kwargs.setdefault("selected_profile_override", selected_profile)
            kwargs.setdefault(
                "simple_chat_profile",
                self.simple_chat_profile if simple_chat_routing_allowed else "",
            )

            if self.usage_store is not None:
                trace = AgentTrace(
                    provider=selected_profile.provider_identity,
                    model=selected_profile.model,
                    profile=selected_profile.name,
                )
                kwargs.setdefault("turn_trace", trace)

            journal_scope_enabled = inbound.endpoint.kind == "private" or (
                await self.db_call(self.ports.endpoint_enabled, inbound.endpoint)
            )
            if durable_lifecycle is not None:
                # This branch is strict: the same journal/execution transaction
                # must commit before model or tool handoff. There is no legacy
                # best-effort fallback if storage is unavailable.
                selected_profile = kwargs["selected_profile_override"]
                await durable_lifecycle.assert_current()
                journal_turn_id = await durable_lifecycle.begin(inbound=inbound,
                    selected_profile=selected_profile, objective=user_text,
                    prompt_version=self.prompt_version)
                if type(journal_turn_id) is not int or journal_turn_id < 1:
                    raise RuntimeError("durable_turn_journal_unavailable")
                # No model call has happened yet. Bind trace identity to the
                # actual selected override, not an earlier routing candidate.
                trace = AgentTrace(provider=selected_profile.provider_identity,
                    model=selected_profile.model, profile=selected_profile.name)
                kwargs["journal_turn_id"] = journal_turn_id
                kwargs["turn_trace"] = trace
                kwargs["turn_context"] = await self.db_call(
                    self.ports.current_turn_context, inbound, journal_turn_id)
            elif (
                self.turn_journal is not None
                and self.message_ledger is not None
                and journal_scope_enabled
            ):
                trigger_message_id = await self.db_call(
                    self.message_ledger.canonical_id_for_native,
                    scope,
                    inbound.native_message_id,
                    endpoint=inbound.endpoint,
                )
                if trigger_message_id is None:
                    try:
                        trigger_message_id = await self.db_call(
                            self.ports.record_trigger, inbound, scope
                        )
                    except Exception as exc:
                        self.logger.warning(
                            f"Could not journal the turn trigger: {exc}"
                        )
                try:

                    def start_turn() -> None:
                        nonlocal journal_turn_id
                        turn = self.turn_journal.start_turn(
                            scope,
                            trigger_canonical_message_id=trigger_message_id,
                            objective=user_text,
                            provider=selected_profile.provider_identity,
                            model=selected_profile.model,
                            profile=selected_profile.name,
                            prompt_version=self.prompt_version,
                        )
                        # db_call drains this write before propagating cancellation.
                        journal_turn_id = turn.turn_id

                    await self.db_call(start_turn)
                    if trace is None:
                        trace = AgentTrace(
                            provider=selected_profile.provider_identity,
                            model=selected_profile.model,
                            profile=selected_profile.name,
                        )
                    kwargs.setdefault("journal_turn_id", journal_turn_id)
                    kwargs.setdefault("turn_trace", trace)
                    kwargs.setdefault(
                        "turn_context",
                        (
                            await self.db_call(
                                self.ports.current_turn_context, inbound, journal_turn_id
                            )
                        ),
                    )
                except Exception as exc:
                    self.logger.warning(f"Could not start the durable AI turn: {exc}")

            if isinstance(stream_context, dict):
                stream_context["turn_id"] = journal_turn_id
            if durable_lifecycle is not None:
                await durable_lifecycle.assert_current()
            reply = await self.ports.ask_agent(inbound, platform, user_text, **kwargs)
            status = "silence" if self.ports.is_silence_reply(reply) else "succeeded"
            await finish(status, self.ports.journal_reply_text(reply))
            return ChatTurnResult(reply=reply, turn_id=journal_turn_id, status=status)
        except ChatFailure as exc:
            try:
                await finish("crashed", exc.reply, exc.code)
            except BaseException:
                await interrupted("result_commit_unavailable")
                raise
            return ChatTurnResult(reply=exc.reply, turn_id=journal_turn_id,
                                  status="crashed", error_code=exc.code)
        except asyncio.CancelledError:
            if durable_lifecycle is not None:
                await interrupted("turn_cancelled_or_owner_lost")
                raise
            self.logger.info(f"AI task {task.task_id} cancelled for {conversation_id}.")
            await finish("aborted")
            return None
        except Exception:
            if durable_lifecycle is not None:
                await interrupted("turn_execution_interrupted")
            else:
                await finish("crashed")
            raise
        except BaseException:
            # Shared tool/model fencing intentionally uses a BaseException so
            # ordinary retry handlers cannot swallow lease loss.
            await interrupted("turn_owner_lost")
            raise
        finally:
            self.running_tasks.finish(task.task_id)

    def _finish_turn(
        self,
        turn_id: int | None,
        status: str,
        trace: AgentTrace | None,
        final_text: str = "",
        *,
        scope_key: str = "",
        error_code: str = "",
    ) -> None:
        if self.turn_journal is not None and turn_id is not None:
            try:
                trace_payload = trace.to_payload() if trace is not None else {}
                if error_code:
                    trace_payload["failure"] = {"code": error_code}
                self.turn_journal.finish_turn(
                    turn_id,
                    status=status,
                    final_text=final_text,
                    trace_payload=trace_payload or None,
                    input_tokens=trace.input_tokens if trace is not None else 0,
                    output_tokens=trace.output_tokens if trace is not None else 0,
                    total_tokens=trace.total_tokens if trace is not None else 0,
                )
            except Exception as exc:
                self.logger.warning(f"Could not finish durable turn {turn_id}: {exc}")
        if (
            self.usage_store is not None
            and trace is not None
            and scope_key
            and (trace.input_tokens > 0 or trace.output_tokens > 0)
        ):
            try:
                self.usage_store.record(
                    scope_key=scope_key,
                    source="turn",
                    provider=trace.provider,
                    model=trace.model,
                    input_tokens=trace.input_tokens,
                    output_tokens=trace.output_tokens,
                    cached_tokens=trace.cached_tokens,
                    turn_id=turn_id,
                )
            except Exception as exc:
                self.logger.warning(f"Could not record model usage: {exc}")
