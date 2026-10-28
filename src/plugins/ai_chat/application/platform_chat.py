"""SDK-free structured text-turn service, directly constructible by an adapter.

The service uses the same context, replay and agent-loop stages as the OneBot
compatibility edge. A caller provides application storage/runtime ports, never
HandlerServices, a synthetic event, or a numeric identity substitute.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from src.bot_storage import DatabaseError
from . import ChatFailure
from . import shared_agent_turn as shared_core
from .shared_agent_turn import SharedTurnPorts
from ..agent.control import assert_job_owned
from ..agent_loop import AgentTrace, DeepSeekConfigError, ask_deepseek_with_tools
from ..ai_tools import WEB_SEARCH_TOOL_NAME, force_tool
from ..context_pipeline import ReferenceResolver
from ..model_budget import ModelBudgetExceeded
from ..model_catalog import ModelProfile
from ..observability import telemetry
from ..platform_types import InboundMessage, TurnPlatform
from ..prompt_context import quote_evidence
from ..tool_policy import approval_from_user_text
from ..tools import neutral_registry_for
from ..tools.state import ToolContext, TurnState
from ..web_search import render_search_sources
from ..workers.durable_jobs import JobDeferred


class PlatformChatService:
    def __init__(
        self, app, *, reference_resolver=None, scope_enabled=None,
        registry_factory=neutral_registry_for, llm=ask_deepseek_with_tools,
    ) -> None:
        self.context = app
        # Neutral handlers need only the explicit app port. Missing legacy
        # helpers are intentional: platform authorization cannot fall back.
        self.services = SimpleNamespace(context=app)
        self.reference_resolver = reference_resolver or ReferenceResolver(
            graph_store=getattr(app, "topic_graph_store", None),
        )
        self.scope_enabled = scope_enabled or self._scope_enabled
        self.registry_factory = registry_factory
        self.llm = llm

    def _scope_enabled(self, scope) -> bool:
        override = self.context.model_preferences.get_scope_enabled_override(scope)
        return override if override is not None else self.context.settings.is_scope_enabled(scope)

    async def ask(
        self, inbound: InboundMessage, platform: TurnPlatform, user_text: str, *,
        ports: SharedTurnPorts | None = None,
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

        Durable Sub-Agent, host/project, media and continuation adapters have not
        established platform authorization yet; they cannot be enabled here by
        a model-selected tool name or by the sender's platform role.
        """
        if platform.account != inbound.endpoint.account:
            raise ValueError("turn platform does not own inbound endpoint")
        if inbound.endpoint.account.platform != "qq-official":
            raise ValueError("structured preparation currently supports qq-official only")
        if any((task_mode, resume_task_id is not None, routine_execution is not None,
                _mcp_execution is not None, _approved_call is not None)):
            raise ChatFailure("当前平台尚未开放持久任务和宿主操作。", code="platform_capability")
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
        if ports is None:
            from .platform_turn_ports import platform_turn_ports
            ports = platform_turn_ports(self.context, inbound, self.reference_resolver)
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

        capabilities = {"self"}
        if self.context.long_term_memory is not None:
            capabilities.add("memory")
        if any(store is not None for store in (
            self.context.turn_journal, self.context.context_store, self.context.message_ledger,
        )):
            capabilities.add("context")
        if self.context.settings.search_enabled and (force_search or self.context.settings.search_auto_enabled):
            capabilities.add("web")
        turn = TurnState(
            context=ToolContext(
                self.services, event=None, capabilities=frozenset(capabilities),
                journal_turn_id=journal_turn_id, inbound=inbound, platform=platform,
            ),
            user_text=user_text,
        )
        registry = await self.context.db_call(self.registry_factory, turn.context)
        tools = await self.context.db_call(registry.definitions, turn.context)
        prepared = await shared_core.prepare_shared_turn_context(
            self.context, inbound, ports, user_text=user_text, selected_profile=selected_profile,
            tools=tools, journal_turn_id=journal_turn_id, turn_context=turn_context,
        )
        if prepared.blocked_reply:
            return prepared.blocked_reply

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
            group_context, memory_context = await shared_core.build_shared_prompt_context(
                self.context, inbound, ports, prepared, user_text=user_text,
                selected_profile=selected_profile, journal_turn_id=journal_turn_id,
            )
            context_parts = []
            if prepared.turn_context:
                context_parts.append(quote_evidence(prepared.turn_context, "turn_context"))
            if prepared.replay_prefix:
                context_parts.append("[host replay status]\n先前回合记录仅作为上下文；不要重复已提交的发送或写入效果。")
            if prepared.replay_digest_prefix:
                context_parts.append(quote_evidence(prepared.replay_digest_prefix, "older_turn_digest"))
            prompt = "[输出约定]\n直接用文字回答。不要编造原生 @、引用、媒体发送或工具成功回执。"
            if platform.features.markdown:
                prompt += "可以使用 Markdown 排版。"
            skill_index = self.context.skill_registry.prompt_index()
            if skill_index:
                prompt += "\n\n" + skill_index
            answer = await shared_core.run_shared_agent_turn(
                user_text, tools, execute_tool, app=self.context, inbound=inbound,
                ports=ports, prepared=prepared, llm=self.llm,
                group_context=group_context, memory_context=memory_context,
                tool_choice=force_tool(WEB_SEARCH_TOOL_NAME) if force_search else "auto",
                profile=selected_profile, max_tool_rounds=self.context.settings.tool_simple_max_rounds,
                tool_context="\n\n".join(context_parts), system_context=prompt,
                trace=turn_trace, feedback_provider=feedback_provider,
                event_sink=(
                    lambda event: shared_core.record_shared_loop_event(self.context, journal_turn_id, event)
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
        await shared_core.append_shared_turn_memory(self.context, ports, user_text, answer)
        visible = answer
        if final_stream_state is not None and final_stream_state.sent_prefix:
            prefix = final_stream_state.sent_prefix.rstrip()
            if visible.startswith(prefix):
                visible = visible[len(prefix):].lstrip("\r\n")
        sources = render_search_sources(turn.search_results)
        return f"{visible}\n\n{sources}" if sources else visible

