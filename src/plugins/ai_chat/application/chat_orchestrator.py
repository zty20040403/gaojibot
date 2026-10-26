from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ..agent import AgentTrace


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

    conversation_id: Callable[[Any], str]
    conversation_scope: Callable[[Any], Any]
    is_group_event: Callable[[Any], bool]
    group_id: Callable[[Any], int | None]
    group_enabled: Callable[[int], bool]
    group_default_profile: Callable[[str], str | None]
    reply_target_turn: Callable[[Any], Any | None]
    record_trigger: Callable[[Any, Any], int | None]
    current_turn_context: Callable[[Any, int], Any]
    drain_feedback: Callable[[str], Any]
    ask_agent: Callable[..., Any]
    is_silence_reply: Callable[[Any], bool]
    journal_reply_text: Callable[[Any], str]


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

    async def run(
        self,
        bot: Any,
        event: Any,
        user_text: str,
        **kwargs: Any,
    ) -> ChatTurnResult | None:
        conversation_id = self.ports.conversation_id(event)
        scope = self.ports.conversation_scope(event)
        stream_context = kwargs.pop("_stream_context", None)
        if self.usage_store is not None:
            quota = self.usage_store.status(scope.key)
            if not quota.allowed:
                return ChatTurnResult(
                    reply=(
                        "今天这个会话的模型额度已经用完了。"
                        "可以在本机管理页调整配额，或者明天再继续。"
                    ),
                    turn_id=None,
                )

        group_id = self.ports.group_id(event)
        task = self.running_tasks.register_current(
            conversation_id=conversation_id,
            user_id=int(event.user_id),
            group_id=group_id,
            message_id=int(event.message_id),
            summary=user_text,
        )
        kwargs.setdefault(
            "feedback_provider",
            lambda: self.ports.drain_feedback(task.task_id),
        )
        journal_turn_id: int | None = None
        trace: AgentTrace | None = None
        caller_selected_profile = "selected_profile_override" in kwargs
        explicit_profile = self.model_preferences.get_explicit(conversation_id)
        group_default_profile = self.ports.group_default_profile(conversation_id)
        selected_profile = self.model_catalog.resolve_preference(
            explicit_profile or group_default_profile
        )
        simple_chat_routing_allowed = (
            not caller_selected_profile
            and explicit_profile is None
            and group_default_profile is None
        )
        if explicit_profile is None and group_default_profile is None:
            previous_turn = self.ports.reply_target_turn(event)
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

        journal_scope_enabled = (
            group_id is None or self.ports.group_enabled(group_id)
        )
        if (
            self.turn_journal is not None
            and self.message_ledger is not None
            and journal_scope_enabled
        ):
            trigger_message_id = self.message_ledger.canonical_id_for_native(
                scope,
                int(event.message_id),
            )
            if trigger_message_id is None:
                try:
                    trigger_message_id = self.ports.record_trigger(event, scope)
                except Exception as exc:
                    self.logger.warning(f"Could not journal the turn trigger: {exc}")
            try:
                turn = self.turn_journal.start_turn(
                    scope,
                    trigger_canonical_message_id=trigger_message_id,
                    objective=user_text,
                    provider=selected_profile.provider_identity,
                    model=selected_profile.model,
                    profile=selected_profile.name,
                    prompt_version=self.prompt_version,
                )
                journal_turn_id = turn.turn_id
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
                    self.ports.current_turn_context(event, journal_turn_id),
                )
            except Exception as exc:
                self.logger.warning(f"Could not start the durable AI turn: {exc}")

        if isinstance(stream_context, dict):
            stream_context["turn_id"] = journal_turn_id
        try:
            reply = await self.ports.ask_agent(bot, event, user_text, **kwargs)
            status = "silence" if self.ports.is_silence_reply(reply) else "succeeded"
            self._finish_turn(
                journal_turn_id,
                status,
                trace,
                self.ports.journal_reply_text(reply),
                scope_key=scope.key,
            )
            return ChatTurnResult(reply=reply, turn_id=journal_turn_id, status=status)
        except ChatFailure as exc:
            self._finish_turn(
                journal_turn_id, "crashed", trace, exc.reply,
                scope_key=scope.key, error_code=exc.code,
            )
            return ChatTurnResult(
                reply=exc.reply, turn_id=journal_turn_id,
                status="crashed", error_code=exc.code,
            )
        except asyncio.CancelledError:
            self.logger.info(
                f"AI task {task.task_id} cancelled for {conversation_id}."
            )
            self._finish_turn(
                journal_turn_id,
                "aborted",
                trace,
                scope_key=scope.key,
            )
            return None
        except Exception:
            self._finish_turn(
                journal_turn_id,
                "crashed",
                trace,
                scope_key=scope.key,
            )
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
