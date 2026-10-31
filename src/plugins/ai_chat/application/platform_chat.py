"""One official QQ chat turn: model choice, context, tools and the agent loop."""
from __future__ import annotations

from dataclasses import replace
import json
from types import SimpleNamespace

from src.bot_storage import DatabaseError
from . import ChatFailure
from .turn_context import record_loop_event, turn_window
from ..agent.control import assert_job_owned
from ..agent_loop import AgentTrace, DeepSeekConfigError, ask_deepseek_with_tools, request_sink
from ..ai_tools import WEB_SEARCH_TOOL_NAME, force_tool
from ..conversation_keys import conversation_key
from ..model_budget import ModelBudgetExceeded
from ..model_catalog import ModelProfile
from ..observability import telemetry
from ..platform_types import InboundMessage, TurnPlatform
from ..prompt import system_prompt
from ..prompt_context import quote_evidence
from ..tool_policy import ToolCatalog, approval_from_user_text, policy_for_tool
from ..tools import neutral_registry_for
from ..tools.state import ToolContext, TurnState
from ..web_search import render_search_sources
from ..workers.durable_jobs import JobDeferred


class PlatformChatService:
    def __init__(
        self, app, *, scope_enabled=None,
        registry_factory=neutral_registry_for, llm=ask_deepseek_with_tools,
        generation_factory=None, media_factory=None,
    ) -> None:
        self.context = app
        # Neutral handlers need only the explicit app port. Missing legacy
        # helpers are intentional: platform authorization cannot fall back.
        self.services = SimpleNamespace(context=app)
        self.scope_enabled = scope_enabled or self._scope_enabled
        self.registry_factory = registry_factory
        self.llm = llm
        self.generation_factory = generation_factory
        self.media_factory = media_factory

    def _scope_enabled(self, scope) -> bool:
        override = self.context.model_preferences.get_scope_enabled_override(scope)
        return override if override is not None else self.context.settings.is_scope_enabled(scope)

    async def ask(
        self, inbound: InboundMessage, platform: TurnPlatform, user_text: str, *,
        force_search: bool = False, force_ocr: bool = False,
        force_voice_reply: bool = False, force_voice_transcription: bool = False,
        available_image_sources=None, available_voice_message_id=None,
        journal_turn_id: int | None = None, turn_trace: AgentTrace | None = None,
        turn_context: str = "", selected_model_override: str | None = None,
        selected_profile_override: ModelProfile | None = None,
        feedback_provider=None, final_stream_sink=None, final_stream_state=None,
        task_mode: bool = False, simple_chat_profile: str = "", resume_task_id=None,
        _approved_call=None, routine_execution=None, _mcp_execution=None,
    ) -> str:
        """Prepare a structured text turn for the same context and agent core.

        Text delegation is available only through the trusted runtime factory.
        Optional file/task host factories supply their exact current
        source authority. Other host/media and revision continuations remain
        unavailable through this foreground entry point.
        """
        if platform.account != inbound.endpoint.account:
            raise ValueError("turn platform does not own inbound endpoint")
        if inbound.endpoint.account.platform != "qq-official":
            raise ValueError("structured preparation currently supports qq-official only")
        if any((task_mode, resume_task_id is not None, routine_execution is not None,
                _mcp_execution is not None, _approved_call is not None)):
            raise ChatFailure("当前平台尚未开放此任务续接入口和宿主操作。", code="platform_capability")
        if any((force_ocr, force_voice_reply, force_voice_transcription,
                available_image_sources, available_voice_message_id is not None)):
            raise ChatFailure("当前平台尚未开放这项媒体能力。", code="platform_capability")
        if inbound.endpoint.kind == "group" and not await self.context.db_call(
            self.scope_enabled, inbound.endpoint.scope,
        ):
            return "这个群暂时没有开启 AI。"
        if len(user_text) > self.context.settings.max_input_chars:
            return f"问题太长了，先压到 {self.context.settings.max_input_chars} 个字符以内。"
        if force_search and not self.context.settings.search_enabled:
            return "联网搜索暂时没有开启。"
        ports = SimpleNamespace(conversation_id=conversation_key(inbound))
        selected_profile = selected_profile_override
        if selected_profile is None:
            preference = await self.context.db_call(
                self.context.model_preferences.get_explicit, ports.conversation_id,
            )
            if preference is None and inbound.endpoint.kind == "group":
                preference = await self.context.db_call(
                    self.context.model_preferences.get_scope_default, inbound.endpoint.scope,
                ) or self.context.settings.model_profile_for_scope(inbound.endpoint.scope)
            selected_profile = self.context.model_catalog.resolve_preference(preference)
        if selected_model_override:
            selected_profile = (
                self.context.model_catalog.try_resolve(selected_model_override)
                or selected_profile.with_model(selected_model_override)
            )
        elif simple_chat_profile and not force_search:
            selected_profile = self.context.model_catalog.try_resolve(simple_chat_profile) or selected_profile

        # The orchestrator chooses a profile before calling this service; that
        # must not suppress the actor's persisted reasoning preference.
        effort = await self.context.db_call(
            self.context.reasoning_preferences.get_explicit, ports.conversation_id,
        )
        if effort is None and inbound.endpoint.kind == "group":
            effort = await self.context.db_call(
                self.context.reasoning_preferences.get_scope_member_default, inbound.endpoint.scope,
            )
        if selected_profile.provider in {"openai", "cliproxy"}:
            from ..model_catalog import SUPPORTED_REASONING_EFFORTS
            if effort in SUPPORTED_REASONING_EFFORTS:
                selected_profile = selected_profile.with_reasoning_effort(effort)

        media_session = (await self.media_factory(inbound, platform) if self.media_factory is not None else None)
        if media_session is not None:
            platform.media_session = media_session
        capabilities = {"self"}
        if media_session is not None and media_session.enabled("send_sticker"):
            capabilities.add("media")
        if getattr(self.context, "conversations", None) is not None:
            capabilities.add("memory")
        if self.context.message_ledger is not None:
            capabilities.add("context")
        if self.context.settings.search_enabled and (force_search or self.context.settings.search_auto_enabled):
            capabilities.add("web")
        context = ToolContext(
            self.services, event=None, capabilities=frozenset(capabilities),
            journal_turn_id=journal_turn_id, inbound=inbound, platform=platform,
        )
        file_session = (await self.generation_factory(inbound, platform)
                        if self.generation_factory is not None else None)
        sent: list[dict] = []

        def keep_last(request: dict) -> None:
            sent[:] = [request]

        sink_token = request_sink.set(keep_last)
        try:
            if file_session is not None:
                context = file_session.context(context)
            turn = TurnState(context=context, user_text=user_text, model_profile=selected_profile)
            registry = await self.context.db_call(self.registry_factory, turn.context)
            tools = await self.context.db_call(registry.definitions, turn.context)
            async def execute_tool(name, arguments):
                from src.storage_admission import CapacityError, require_tool_capacity
                from ..tool_policy import policy_for_tool
                assert_job_owned()
                try:
                    require_tool_capacity(name, policy_for_tool(name).side_effects)
                except CapacityError as exc:
                    return json.dumps({"ok": False, "error": str(exc), "code": "capacity_blocked"}, ensure_ascii=False)
                async with telemetry.tool(name):
                    result = await registry.execute(turn, name, arguments)
                return result if result is not None else json.dumps({
                    "ok": False, "code": "tool_unavailable", "error": "当前会话不能使用此工具。",
                }, ensure_ascii=False)

            try:
                group_context, memory_context, quoted = await self.context.db_call(
                    turn_window, self.context, inbound, selected_profile)
                context_parts = [quoted] if quoted else []
                if turn_context:
                    context_parts.append(quote_evidence(turn_context, "turn_context"))
                skill_index = self.context.skill_registry.prompt_index()
                system = system_prompt(self.context.settings.system_prompt, skill_index)
                answer = await self.llm(
                    user_text, [], tools, execute_tool,
                    current_user=inbound.sender_display or "群友",
                    group_context=group_context, memory_context=memory_context,
                    tool_choice=force_tool(WEB_SEARCH_TOOL_NAME) if force_search else "auto",
                    profile=selected_profile,
                    tool_catalog=ToolCatalog(tools, ignore_unknown_fields=True, policies={
                        'send_file_from_sandbox': replace(policy_for_tool('send_file_from_sandbox'), timeout_seconds=310),
                        'send_file': replace(policy_for_tool('send_file_from_sandbox'), timeout_seconds=310),
                    }),
                    max_calls_per_round=24, max_total_calls=160,
                    max_tool_rounds=(
                        max(48, self.context.settings.tool_max_rounds) if file_session is not None
                        else max(16, self.context.settings.tool_simple_max_rounds)
                    ),
                    tool_context="\n\n".join(context_parts), system_prompt=system,
                    trace=turn_trace, feedback_provider=feedback_provider,
                    event_sink=(
                        lambda event: record_loop_event(self.context, journal_turn_id, event)
                    ) if journal_turn_id is not None else None,
                    final_text_sink=final_stream_sink, final_stream_state=final_stream_state,
                    approval_checker=lambda _policy, name, arguments: approval_from_user_text(user_text, name, arguments),
                )
            except (ModelBudgetExceeded, JobDeferred, ChatFailure):
                raise
            except DeepSeekConfigError as exc:
                raise ChatFailure(f"模型配置 {selected_profile.name} 不可用，请检查 API Key 和模型配置。", code="model_config") from exc
            except DatabaseError as exc:
                raise ChatFailure("我读取聊天上下文时遇到数据库问题，等会儿再试。", code="database") from exc
            except RuntimeError as exc:
                raise ChatFailure(f"{selected_profile.provider} 暂时没回上来，等会儿再试。", code="model_request") from exc
            except Exception as exc:
                raise ChatFailure("我这边处理消息时出错了。", code="internal") from exc
            if not answer:
                raise ChatFailure("模型没有返回内容。", code="empty_response")
            assert_job_owned()
            if media_session is not None:
                await media_session.current()
                answer = media_session.finish(answer)
            if answer.strip() == "[silence]":
                return answer.strip()
            visible = answer
            if final_stream_state is not None and final_stream_state.sent_prefix:
                prefix = final_stream_state.sent_prefix.rstrip()
                if visible.startswith(prefix):
                    visible = visible[len(prefix):].lstrip("\r\n")
            sources = render_search_sources(turn.search_results)
            return f"{visible}\n\n{sources}" if sources else visible

        finally:
            request_sink.reset(sink_token)
            if sent and getattr(self.context, "conversations", None) is not None:
                try:
                    await self.context.db_call(self.context.conversations.record_request, turn_id=journal_turn_id,
                        scope_key=inbound.scope.key, model=str(sent[-1].get("model", "")),
                        messages=sent[-1].get("messages", []), tools=sent[-1].get("tools", []))
                except DatabaseError:
                    self.context.logger.warning("Could not save the model request for this turn.")
            if file_session is not None:
                await file_session.close()
