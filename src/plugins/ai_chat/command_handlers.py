"""Command Handlers responsibilities extracted from the plugin entrypoint."""

from __future__ import annotations

import re
import time
from datetime import (
    datetime,
)
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.exception import (
    FinishedException,
)
from nonebot.params import (
    CommandArg,
)
from .context_policy import (
    ContextPolicy,
)
from .long_term_memory import (
    LongTermMemoryError,
    MemoryEntry,
)
from .model_catalog import (
    ModelCatalogError,
)
from .onebot_codec import (
    scope_from_event,
)
from .output_planner import (
    ACK_FACE_ID,
)
from .ocr import (
    image_sources,
    replied_image_sources,
    reply_message_id,
)
from .reminders import (
    Reminder,
)
from .stickers import (
    clear_learned_stickers,
    learned_sticker_count,
    list_stickers,
)
from .web_search import (
    SearchError,
    render_direct_search_results,
    search_freshness,
    search_web,
)
from .voice import (
    contains_voice,
    replied_voice_message_id,
)
from .handler_services import HandlerService
from .handler_constants import (BOT_VERSION, EMPTY_MENTION_FOLLOW_UP, SHANGHAI_TZ)
from .matchers import (ai, ai_reset, clear_data, control_command, effort_command, image_ocr, memory_command, mention_ai, model_command, pin_command, pins_command, qq_face, shell_command, sticker, sticker_status, task_status, task_stop, unpin_command, usage_command, voice_answer, voice_transcription, web_search)
from .sandbox import SandboxError
from .qq_action_authorization import mobile_command, command_targets


class CommandHandlers(HandlerService):
    def _format_elapsed(self, seconds: int) -> str:
        if seconds < 60:
            return f"{seconds} 秒"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes} 分钟"
        hours, remaining_minutes = divmod(minutes, 60)
        if hours < 24:
            return f"{hours} 小时 {remaining_minutes} 分钟"
        days, remaining_hours = divmod(hours, 24)
        return f"{days} 天 {remaining_hours} 小时"

    def _task_status_text(self, event: MessageEvent) -> str:
        tasks = self.services.chat._running_tasks_for_event(event)
        subagent_tasks = (
            self.context.subagent_store.recent(limit=5, scope_key=scope_from_event(event).key)
            if self.context.subagent_store is not None
            else []
        )
        active_subagents = [
            task
            for task in subagent_tasks
            if task.status in {"received", "planning", "running", "verifying", "cancelling"}
        ]
        if not tasks and not active_subagents:
            return (
                "当前会话没有正在运行的任务。\n"
                "使用 /task 任务内容，可以让主控拆分并派给专业 Sub-Agent。"
            )
        lines = ["正在运行的任务："]
        lines.extend(
            f"- {task.task_id} · {task.elapsed_seconds}s · {task.summary or '未命名任务'}"
            for task in tasks
        )
        lines.extend(
            f"- {task.handle} · {task.status} · {task.objective[:80]}"
            for task in active_subagents
        )
        lines.append("\n停止：/停止 task#编号；不写 ID 时停止当前前台任务。")
        return "\n".join(lines)

    def _usage_text(self, event: MessageEvent) -> str:
        if self.context.turn_journal is None:
            return "Token 用量统计暂时不可用。"
        usage = self.context.turn_journal.usage_summary(scope_from_event(event))
        return (
            "当前可见会话用量：\n"
            f"- 回合：{usage['turns']}\n"
            f"- 输入 Token：{usage['input_tokens']}\n"
            f"- 输出 Token：{usage['output_tokens']}\n"
            f"- 合计 Token：{usage['total_tokens']}\n"
            "这里只统计 API 返回并写入回合日志的 Token，不按价格估算费用。"
        )

    def _memory_scopes(self, event: MessageEvent) -> tuple[str | None, str]:
        group_scope = (
            f"group:{event.group_id}"
            if isinstance(event, GroupMessageEvent)
            else None
        )
        return group_scope, self.services.chat._conversation_id(event)

    def _memory_provenance(self, event: MessageEvent) -> dict[str, int | None]:
        principal_id = 0
        source_message_id: int | None = None
        if self.context.message_ledger is not None:
            scope = scope_from_event(event)
            principal_id = (
                self.context.message_ledger.principal_id_for_native(
                    scope.platform,
                    event.user_id,
                )
                or 0
            )
            source_message_id = self.context.message_ledger.canonical_id_for_native(
                scope,
                event.message_id,
            )
        return {
            "actor_user_id": event.user_id,
            "actor_principal_id": principal_id,
            "source_message_id": source_message_id,
        }

    def _memory_scope_keys(self,
        event: MessageEvent,
        requested_scope: str = "all",
    ) -> list[str]:
        group_scope, user_scope = self._memory_scopes(event)
        if requested_scope == "user":
            return [user_scope]
        if requested_scope == "group":
            return [group_scope] if group_scope is not None else []
        return [
            scope
            for scope in (group_scope, user_scope)
            if scope is not None
        ]

    def _current_long_term_memory(self,
        event: MessageEvent,
        user_text: str,
        policy: ContextPolicy,
    ) -> str:
        group_scope, user_scope = self._memory_scopes(event)
        return self.context.long_term_memory.render_relevant(
            group_scope,
            user_scope,
            user_text,
            include_group=policy.include_group_memory,
            include_user=policy.include_user_memory,
            fallback_group=policy.fallback_group_memory,
            fallback_user=policy.fallback_user_memory,
            max_entries_per_scope=policy.memory_max_entries_per_scope,
            max_chars=policy.memory_max_chars,
        )

    def _memory_entry_payload(self, entry: MemoryEntry) -> dict[str, object]:
        return {
            "id": entry.id,
            "scope": entry.scope_type,
            "content": entry.content,
            "version": entry.version,
            "source_message": (
                f"msg#{entry.source_message_id}"
                if entry.source_message_id is not None
                else None
            ),
            "created_at": entry.created_at,
        }

    def _canonical_message_id(self, value: object) -> int | None:
        matched = re.fullmatch(r"msg#([1-9][0-9]*)", str(value or "").strip())
        return int(matched.group(1)) if matched is not None else None

    def _reminder_id(self, value: object) -> int | None:
        matched = re.fullmatch(
            r"reminder#([1-9][0-9]*)",
            str(value or "").strip(),
        )
        return int(matched.group(1)) if matched is not None else None

    def _parse_reminder_due_at(self, value: object) -> int:
        raw = str(value or "").strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError("due_at 必须是 ISO 8601 时间。") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
        timestamp = int(parsed.timestamp())
        if timestamp > int(time.time()) + 366 * 24 * 3600 * 10:
            raise ValueError("提醒时间不能超过十年。")
        return timestamp

    def _reminder_payload(self, reminder: Reminder) -> dict[str, object]:
        due = datetime.fromtimestamp(
            reminder.scheduled_for,
            SHANGHAI_TZ,
        ).isoformat(timespec="seconds")
        return {
            "handle": reminder.handle,
            "message": reminder.message,
            "due_at": due,
            "status": reminder.status,
            "attempts": reminder.attempts,
        }

    def _pin_target_message_id(self, event: MessageEvent, raw: str = "") -> int | None:
        explicit = self._canonical_message_id(raw)
        if explicit is not None:
            return explicit
        if self.context.message_ledger is None:
            return None
        native_reply_id = reply_message_id(event.original_message)
        if native_reply_id is None:
            return None
        return self.context.message_ledger.canonical_id_for_native(
            scope_from_event(event),
            native_reply_id,
        )

    def _looks_like_secret(self, content: str) -> bool:
        return bool(
            re.search(r"\bsk-[A-Za-z0-9_-]{10,}\b", content)
            or re.search(
                r"(?i)(?:api[_ -]?key|access[_ -]?token|password|secret|密码|验证码)"
                r"\s*[:=：]\s*\S+",
                content,
            )
        )

    def _can_edit_group_memory(self, event: MessageEvent) -> bool:
        if not isinstance(event, GroupMessageEvent):
            return False
        return (
            event.sender.role in {"owner", "admin"}
            or event.user_id in self.context.settings.sandbox_allowed_users
        )

    def _memory_label(self, entry: MemoryEntry) -> str:
        return "群" if entry.scope_type == "group" else "个人"

    def _find_visible_memory(self,
        event: MessageEvent,
        memory_id: int,
    ) -> MemoryEntry | None:
        return next(
            (
                entry
                for entry in self.context.long_term_memory.list_entries(
                    self._memory_scope_keys(event)
                )
                if entry.id == memory_id
            ),
            None,
        )

    async def _direct_web_search(self,
        event: MessageEvent,
        query: str,
    ) -> str:
        if isinstance(event, GroupMessageEvent) and not (
            await self.context.db_call(self.services.group_enabled, event.group_id)
        ):
            return "这个群暂时没有开启搜索。"
        if not self.context.settings.search_enabled:
            return "联网搜索暂时没有开启。"
        if len(query) > self.context.settings.max_input_chars:
            return f"关键词太长了，先压到 {self.context.settings.max_input_chars} 个字符以内。"

        try:
            results = await search_web(
                query,
                max_results=self.context.settings.search_max_results,
                timeout_seconds=self.context.settings.search_timeout_seconds,
                freshness=search_freshness(query),
                provider=self.context.settings.search_provider,
                api_key=self.context.settings.search_api_key,
            )
        except SearchError as exc:
            self.context.logger.warning(f"Direct web search failed: {exc}")
            return "联网搜索失败了，可能是网络或搜索页面暂时不可用。"

        rendered = render_direct_search_results(
            results,
            max_results=self.context.settings.search_max_results,
        )
        return rendered or "没搜到可用结果，换个关键词试试。"

    async def _resolve_ocr_sources(self,
        bot: Bot,
        event: MessageEvent,
    ) -> list[str]:
        sources = image_sources(
            event.original_message,
            max_images=self.context.settings.ocr_max_images,
        )
        if not sources:
            sources = await replied_image_sources(
                bot,
                event.original_message,
                max_images=self.context.settings.ocr_max_images,
            )
        if not sources:
            sources = self.context.recent_images.get(self.services.chat._image_cache_key(event))
        if sources:
            self.context.recent_images.record(self.services.chat._image_cache_key(event), sources)
        return sources

    async def _resolve_voice_message_id(self,
        bot: Bot,
        event: MessageEvent,
    ) -> int | None:
        if contains_voice(event.original_message):
            message_id = event.message_id
        else:
            message_id = await replied_voice_message_id(
                bot,
                event.original_message,
            )
        if message_id is None:
            message_id = self.context.recent_voices.get(self.services.chat._voice_cache_key(event))
        if message_id is not None:
            self.context.recent_voices.record(self.services.chat._voice_cache_key(event), message_id)
        return message_id

    async def _finish_image_ocr(self,
        matcher,
        bot: Bot,
        event: MessageEvent,
        user_text: str,
    ) -> None:
        if not self.context.settings.ocr_enabled:
            await self.services.replies._finish_safely(
                matcher,
                self.services.replies._reply_message(event, "图片文字识别暂时没有开启。"),
            )
            return

        sources = await self._resolve_ocr_sources(bot, event)
        if not sources:
            await self.services.replies._finish_safely(
                matcher,
                self.services.replies._reply_message(
                    event,
                    "请先发一张图片，5 分钟内再让我识图；也可以回复那张图片。",
                ),
            )
            return

        question = user_text.strip() or "请概括并解释图片中的文字。"
        await self.services.replies._finish_tracked_ai(
            matcher,
            bot,
            event,
            question,
            force_ocr=True,
            available_image_sources=sources,
            label="OCR reply",
            retry_on_timeout=True,
        )

    async def _finish_voice_transcription(self,
        matcher,
        bot: Bot,
        event: MessageEvent,
        user_text: str,
    ) -> None:
        if not self.context.settings.voice_enabled:
            await self.services.replies._finish_safely(
                matcher,
                self.services.replies._reply_message(event, "语音功能暂时没有开启。"),
            )
            return

        message_id = await self._resolve_voice_message_id(bot, event)
        if message_id is None:
            await self.services.replies._finish_safely(
                matcher,
                self.services.replies._reply_message(
                    event,
                    "请先发一条语音，5 分钟内再让我听；也可以回复那条语音。",
                ),
            )
            return

        question = user_text.strip() or "请根据这条语音的内容自然回答。"
        await self.services.replies._finish_tracked_ai(
            matcher,
            bot,
            event,
            question,
            force_voice_transcription=True,
            available_voice_message_id=message_id,
            label="voice transcription reply",
            retry_on_timeout=True,
        )

    async def handle_ai(self,
        bot: Bot,
        event: MessageEvent,
        args: Message = CommandArg(),
    ) -> None:
        user_text = args.extract_plain_text().strip()
        if not user_text:
            await self.services.replies._finish_safely(
                ai,
                self.services.replies._reply_message(event, "用法：/ai 你的问题"),
            )

        await self.services.replies._finish_tracked_ai(
            ai,
            bot,
            event,
            user_text,
            label="AI reply",
            retry_on_timeout=True,
        )

    async def handle_web_search(self,
        event: MessageEvent,
        args: Message = CommandArg(),
    ) -> None:
        user_text = args.extract_plain_text().strip()
        if not user_text:
            await self.services.replies._finish_safely(
                web_search,
                self.services.replies._reply_message(event, "用法：/搜 关键词"),
            )

        await self.services.replies._finish_safely(
            web_search,
            self.services.replies._reply_message(
                event,
                await self._direct_web_search(event, user_text),
            ),
            "direct search reply",
            retry_on_timeout=True,
        )

    async def handle_image_ocr(self,
        bot: Bot,
        event: MessageEvent,
        args: Message = CommandArg(),
    ) -> None:
        await self._finish_image_ocr(
            image_ocr,
            bot,
            event,
            args.extract_plain_text().strip(),
        )

    async def handle_voice_answer(self,
        bot: Bot,
        event: MessageEvent,
        args: Message = CommandArg(),
    ) -> None:
        user_text = args.extract_plain_text().strip()
        if not user_text:
            await self.services.replies._finish_safely(
                voice_answer,
                self.services.replies._reply_message(event, "用法：/语音 你的问题"),
            )

        await self.services.replies._finish_tracked_ai(
            voice_answer,
            bot,
            event,
            user_text,
            force_voice_reply=True,
            label="voice reply",
        )

    async def handle_voice_transcription(self,
        bot: Bot,
        event: MessageEvent,
        args: Message = CommandArg(),
    ) -> None:
        await self._finish_voice_transcription(
            voice_transcription,
            bot,
            event,
            args.extract_plain_text().strip(),
        )

    @mobile_command
    async def handle_model_command(self,
        event: MessageEvent,
        args: Message = CommandArg(),
    ) -> None:
        conversation_id = self.services.chat._conversation_id(event)
        current_profile = await self.context.db_call(
            self.services.chat._preferred_model_profile, conversation_id
        )
        requested = args.extract_plain_text().strip()

        if requested.lower() in {"默认", "default", "reset", "重置"}:
            (
                await self.context.db_call(
                    self.context.model_preferences.clear, conversation_id
                )
            )
            default_profile = await self.context.db_call(
                self.services.chat._preferred_model_profile, conversation_id
            )
            if default_profile.provider not in {"openai", "cliproxy"} and (
                await self.context.db_call(
                    self.context.reasoning_preferences.clear, conversation_id
                )
            ):
                default_profile = await self.context.db_call(
                    self.services.chat._preferred_model_profile, conversation_id
                )
            default_scope = (
                "当前群默认模型"
                if self.services.chat._group_default_model_preference(conversation_id) is not None
                else "全局默认模型"
            )
            await self.services.replies._finish_safely(
                model_command,
                self.services.replies._reply_message(
                    event,
                    f"已恢复{default_scope}："
                    f"{default_profile.name}（{default_profile.model}）",
                ),
            )
            return

        if not requested:
            thinking_labels = {
                "auto": "服务端自动",
                "enabled": "开启",
                "disabled": "关闭",
            }
            explicit_effort = await self.context.db_call(
                self.context.reasoning_preferences.get_explicit, conversation_id
            )
            effort_text = current_profile.reasoning_effort or "服务端默认"
            if explicit_effort:
                effort_text += "（当前会话覆盖）"
            lines = [
                "当前模型："
                f"{current_profile.name}（{current_profile.provider} / "
                f"{current_profile.model}）",
                "推理模式：" + thinking_labels.get(
                    current_profile.thinking,
                    current_profile.thinking,
                ),
                f"推理强度：{effort_text}",
                "",
                "可用模型配置：",
            ]
            for profile in self.context.model_catalog.profiles:
                flags = []
                if profile.capabilities.tools:
                    flags.append("工具")
                if profile.capabilities.streaming:
                    flags.append("流式")
                if profile.capabilities.json_mode:
                    flags.append("JSON")
                if profile.capabilities.vision:
                    flags.append("视觉")
                default_label = (
                    " · 默认" if profile.name == self.context.model_catalog.default_name else ""
                )
                configured_label = "" if profile.configured else " · 未配置密钥"
                capability_text = "/".join(flags) or "纯文本"
                reasoning_text = (
                    profile.reasoning_effort
                    or thinking_labels.get(profile.thinking, profile.thinking)
                )
                lines.append(
                    f"- {profile.name}: {profile.provider} / {profile.model} "
                    f"[{capability_text}；推理:{reasoning_text}]"
                    f"{default_label}{configured_label}"
                )
            lines.append("\n切换：/模型 配置名")
            lines.append("恢复：/模型 默认")
            lines.append("推理强度：/effort high（查看可直接发送 /effort）")
            await self.services.replies._finish_safely(
                model_command,
                self.services.replies._reply_message(event, "\n".join(lines)),
            )
            return

        try:
            target_profile = self.context.model_catalog.resolve(requested)
        except ModelCatalogError:
            await self.services.replies._finish_safely(
                model_command,
                self.services.replies._reply_message(
                    event,
                    "没有这个模型配置。发送 /模型 查看可用列表。",
                ),
            )
            return

        (
            await self.context.db_call(
                self.context.model_preferences.set, conversation_id, target_profile.name
            )
        )
        effort_cleared = target_profile.provider not in {"openai", "cliproxy"} and (
            await self.context.db_call(
                self.context.reasoning_preferences.clear, conversation_id
            )
        )
        await self.services.replies._finish_safely(
            model_command,
            self.services.replies._reply_message(
                event,
                f"已切换到：{target_profile.name}（{target_profile.model}）\n"
                "只影响你在当前会话中的回答。"
                + (
                    "\n该提供方不支持强度档位，已清除原来的 /effort 覆盖。"
                    if effort_cleared
                    else ""
                ),
            ),
        )

    @mobile_command
    async def handle_effort_command(self,
        event: MessageEvent,
        args: Message = CommandArg(),
    ) -> None:
        conversation_id = self.services.chat._conversation_id(event)
        profile = await self.context.db_call(
            self.services.chat._preferred_model_profile, conversation_id
        )
        requested = args.extract_plain_text().strip().lower()
        aliases = {
            "最低": "minimal",
            "低": "low",
            "中": "medium",
            "高": "high",
            "超高": "xhigh",
            "最高": "max",
            "关闭": "none",
        }
        requested = aliases.get(requested, requested)
        valid = {"minimal", "low", "medium", "high", "xhigh", "max", "none"}
        explicit = await self.context.db_call(
            self.context.reasoning_preferences.get_explicit, conversation_id
        )
        group_default = self.services.chat._group_member_reasoning_preference(conversation_id)

        if not requested:
            current = profile.reasoning_effort or "服务端默认"
            source = (
                "当前会话覆盖"
                if explicit
                else "群友统一配置"
                if group_default
                else "模型配置"
            )
            support = (
                "支持会话级设置"
                if profile.provider in {"openai", "cliproxy"}
                else "当前提供方只支持推理开关，不支持强度档位"
            )
            await self.services.replies._finish_safely(
                effort_command,
                self.services.replies._reply_message(
                    event,
                    f"当前推理强度：{current}（{source}）\n"
                    f"当前模型：{profile.name} / {profile.model}\n"
                    f"状态：{support}\n"
                    "设置：/effort minimal|low|medium|high|xhigh|max|none\n"
                    "恢复：/effort default",
                ),
            )
            return

        if requested in {"default", "reset", "默认", "重置"}:
            (
                await self.context.db_call(
                    self.context.reasoning_preferences.clear, conversation_id
                )
            )
            restored = await self.context.db_call(
                self.services.chat._preferred_model_profile, conversation_id
            )
            await self.services.replies._finish_safely(
                effort_command,
                self.services.replies._reply_message(
                    event,
                    "已恢复继承的推理强度："
                    f"{restored.reasoning_effort or '服务端默认'}。",
                ),
            )
            return

        if requested not in valid:
            await self.services.replies._finish_safely(
                effort_command,
                self.services.replies._reply_message(
                    event,
                    "不认识这个推理强度。可用：minimal、low、medium、high、"
                    "xhigh、max、none、default。",
                ),
            )
            return

        if profile.provider not in {"openai", "cliproxy"}:
            await self.services.replies._finish_safely(
                effort_command,
                self.services.replies._reply_message(
                    event,
                    f"{profile.name} 不支持按档位调整推理强度。"
                    "先用 /模型 切换到 GPT/CLIProxy 配置。",
                ),
            )
            return

        (
            await self.context.db_call(
                self.context.reasoning_preferences.set, conversation_id, requested
            )
        )
        await self.services.replies._finish_safely(
            effort_command,
            self.services.replies._reply_message(
                event,
                f"推理强度已设为 {requested}。只影响你在当前会话中的回答。",
            ),
        )

    def _shell_owner(self, event: MessageEvent) -> str:
        if isinstance(event, GroupMessageEvent):
            return f"shell:group:{event.group_id}"
        return f"shell:private:{event.user_id}"

    def _format_shell_result(self, result) -> str:
        parts: list[str] = []
        stdout = result.stdout.rstrip()
        stderr = result.stderr.rstrip()
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append("[stderr]\n" + stderr)
        if result.returncode != 0:
            parts.append(f"[退出码 {result.returncode}]")
        return "\n".join(parts) or "（命令执行成功，无输出）"

    @mobile_command
    async def handle_shell_command(self,
        event: MessageEvent,
        args: Message = CommandArg(),
    ) -> None:
        if not self.context.settings.is_sandbox_user_allowed(event.user_id):
            await self.services.replies._finish_safely(
                shell_command,
                self.services.replies._reply_message(event, "当前没有开放命令行沙盒。"),
            )
            return

        owner = self._shell_owner(event)
        command = args.extract_plain_text().strip()
        action = command.casefold()

        if action in {"reset", "重建", "destroy", "销毁"} and event.user_id not in self.context.settings.admin_user_ids:
            await self.services.replies._finish_safely(
                shell_command,
                self.services.replies._reply_message(event, "只有管理员可以重建或销毁命令行沙盒。"),
            )
            return

        if not command or action in {"help", "帮助"}:
            await self.services.replies._finish_safely(
                shell_command,
                self.services.replies._reply_message(
                    event,
                    "用法：/shell Shell命令\n"
                    "示例：/shell ls -lah\n"
                    "状态：/shell status\n"
                    "重建：/shell reset\n"
                    "同一个群复用同一个 /workspace；群与群之间隔离。",
                ),
            )
            return

        outcome = "succeeded"
        try:
            if action in {"status", "状态"}:
                sandboxes = await self.context.sandbox_manager.list(owner)
                shell_sandboxes = [
                    item for item in sandboxes if item.get("purpose") == "shell"
                ]
                text = (
                    "当前群还没有默认命令行沙盒。执行任意 /shell 命令会自动创建。"
                    if not shell_sandboxes
                    else "当前群命令行沙盒：\n"
                    + "\n".join(
                        f"- {item['sandbox_id']} · {item['status']} · /workspace"
                        for item in shell_sandboxes
                    )
                )
            elif action in {"reset", "重建", "destroy", "销毁"}:
                sandboxes = await self.context.sandbox_manager.list(owner)
                approved_targets = command_targets().get("sandbox_ids")
                shell_sandboxes = [
                    item for item in sandboxes if item.get("purpose") == "shell"
                    and (approved_targets is None or str(item["sandbox_id"]) in approved_targets)
                ]
                for item in shell_sandboxes:
                    await self.context.sandbox_manager.destroy(owner, str(item["sandbox_id"]))
                text = (
                    "已销毁当前群的命令行沙盒，下次执行时会创建干净工作区。"
                    if shell_sandboxes
                    else "当前群没有命令行沙盒。"
                )
            else:
                sandbox = await self.context.sandbox_manager.ensure_default(owner, "debian")
                result = await self.context.sandbox_manager.exec(
                    owner,
                    str(sandbox["sandbox_id"]),
                    command,
                    30,
                )
                text = self._format_shell_result(result)
        except SandboxError as exc:
            outcome = "failed"
            text = f"命令行沙盒执行失败：{exc}"

        self.context.logger.info(
            "Sandbox shell action handled: user=%s owner=%s action=%s outcome=%s",
            event.user_id, owner,
            action if action in {"status", "状态", "reset", "重建", "destroy", "销毁"} else "exec",
            outcome,
        )

        await self.services.replies._finish_safely(
            shell_command,
            self.services.replies._reply_message(event, text),
        )

    @mobile_command
    async def handle_memory_command(self,
        event: MessageEvent,
        args: Message = CommandArg(),
    ) -> None:
        requested = " ".join(args.extract_plain_text().split())
        action, _, remainder = requested.partition(" ")
        normalized_action = action.casefold()

        if normalized_action in {"审计", "audit", "history", "历史"}:
            mutations = await self.context.db_call(
                self.context.long_term_memory.audit,
                self._memory_scope_keys(event),
                limit=30,
            )
            if not mutations:
                message = "当前可见范围内还没有长期记忆变更记录。"
            else:
                lines = ["最近的长期记忆变更："]
                for mutation in mutations:
                    actor = (
                        f"[mention#{mutation.actor_principal_id}]"
                        if mutation.actor_principal_id > 0
                        else "本地操作者"
                    )
                    evidence = (
                        f" · msg#{mutation.source_message_id}"
                        if mutation.source_message_id is not None
                        else ""
                    )
                    lines.append(
                        f"- m{mutation.memory_id} {mutation.action} "
                        f"v{mutation.from_version}→v{mutation.to_version} · "
                        f"{actor}{evidence}"
                    )
                message = "\n".join(lines)
            await self.services.replies._finish_safely(
                memory_command,
                self.services.replies._reply_message(event, message),
            )
            return

        if not requested or normalized_action in {"列表", "list", "ls", "查看"}:
            entries = await self.context.db_call(
                self.context.long_term_memory.list_entries,
                self._memory_scope_keys(event),
            )
            if not entries:
                await self.services.replies._finish_safely(
                    memory_command,
                    self.services.replies._reply_message(event, "当前群和你的个人范围都没有长期记忆。"),
                )
                return
            lines = ["当前可见的长期记忆："]
            lines.extend(
                f"- #{entry.id} [{self._memory_label(entry)}] {entry.content}"
                for entry in entries
            )
            lines.append("\n删除：/记忆 删除 ID")
            await self.services.replies._finish_safely(
                memory_command,
                self.services.replies._reply_message(event, "\n".join(lines)),
            )
            return

        if normalized_action in {"删除", "remove", "rm", "forget", "忘记"}:
            raw_id = remainder.strip().lstrip("#")
            try:
                memory_id = int(raw_id)
            except ValueError:
                memory_id = 0
            entry = await self.context.db_call(
                self._find_visible_memory, event, memory_id
            )
            if entry is None:
                message = "没有找到这条可见记忆。发送 /记忆 查看 ID。"
            elif entry.scope_type == "group" and not self._can_edit_group_memory(event):
                message = "只有群管理员或机器人授权用户可以删除群记忆。"
            else:
                (
                    await self.context.db_call(
                        self.context.long_term_memory.remove,
                        memory_id,
                        self._memory_scope_keys(event),
                        **(await self.context.db_call(self._memory_provenance, event)),
                        reason="memory command remove",
                    )
                )
                message = f"已删除 #{memory_id} [{self._memory_label(entry)}] 记忆。"
            await self.services.replies._finish_safely(
                memory_command,
                self.services.replies._reply_message(event, message),
            )
            return

        if normalized_action in {"清空", "clear"}:
            target = remainder.strip().casefold() or "user"
            if target in {"群", "group"}:
                if not self._can_edit_group_memory(event):
                    message = "只有群管理员或机器人授权用户可以清空群记忆。"
                else:
                    removed = await self.context.db_call(
                        self.context.long_term_memory.clear,
                        self._memory_scope_keys(event, "group"),
                        **(await self.context.db_call(self._memory_provenance, event)),
                        reason="memory command clear group",
                    )
                    message = f"已清空当前群长期记忆，共 {removed} 条。"
            elif target in {"全部", "all"}:
                scopes = self._memory_scope_keys(event, "user")
                if self._can_edit_group_memory(event):
                    scopes.extend(self._memory_scope_keys(event, "group"))
                removed = await self.context.db_call(
                    self.context.long_term_memory.clear,
                    scopes,
                    **(await self.context.db_call(self._memory_provenance, event)),
                    reason="memory command clear all visible",
                )
                message = f"已清空你有权修改的长期记忆，共 {removed} 条。"
            else:
                removed = await self.context.db_call(
                    self.context.long_term_memory.clear,
                    self._memory_scope_keys(event, "user"),
                    **(await self.context.db_call(self._memory_provenance, event)),
                    reason="memory command clear user",
                )
                message = f"已清空你的长期记忆，共 {removed} 条。"
            await self.services.replies._finish_safely(
                memory_command,
                self.services.replies._reply_message(event, message),
            )
            return

        scope_type = "user"
        content = requested
        if normalized_action in {"添加", "add", "记住", "remember"}:
            content = remainder.strip()
        if normalized_action in {"群", "group"}:
            scope_type = "group"
            content = remainder.strip()
        elif content.startswith(("群：", "群:", "group:")):
            scope_type = "group"
            content = content.split(":", 1)[-1] if ":" in content else content[2:]
            content = content.lstrip("：").strip()

        group_scope, user_scope = self._memory_scopes(event)
        if scope_type == "group":
            if group_scope is None:
                message = "私聊中不能添加群记忆。"
                await self.services.replies._finish_safely(
                    memory_command,
                    self.services.replies._reply_message(event, message),
                )
                return
            if not self._can_edit_group_memory(event):
                await self.services.replies._finish_safely(
                    memory_command,
                    self.services.replies._reply_message(
                        event,
                        "只有群管理员或机器人授权用户可以添加群记忆。",
                    ),
                )
                return
            scope_key = group_scope
        else:
            scope_key = user_scope

        if self._looks_like_secret(content):
            message = "检测到可能的密码、Token 或密钥，拒绝保存。"
        else:
            provenance = await self.context.db_call(self._memory_provenance, event)
            try:
                entry, created = await self.context.db_call(
                    self.context.long_term_memory.add,
                    scope_key,
                    scope_type,
                    content,
                    creator_user_id=event.user_id,
                    creator_principal_id=int(provenance["actor_principal_id"] or 0),
                    source_message_id=provenance["source_message_id"],
                    reason="memory command add",
                )
            except LongTermMemoryError as exc:
                message = str(exc)
            else:
                verb = "已记住" if created else "这条已经记过了"
                message = f"{verb}：#{entry.id} [{self._memory_label(entry)}] {entry.content}"
        await self.services.replies._finish_safely(
            memory_command,
            self.services.replies._reply_message(event, message),
        )

    async def _ack_control_command(self,
        matcher,
        bot: Bot,
        event: MessageEvent,
        fallback: str,
    ) -> None:
        if await self.services.replies._set_message_reaction(
            bot,
            event,
            ACK_FACE_ID,
            added=True,
        ):
            raise FinishedException
        await self.services.replies._finish_safely(matcher, self.services.replies._reply_message(event, fallback))

    @mobile_command
    async def handle_control_command(self, bot: Bot, event: MessageEvent) -> None:
        plain = event.message.extract_plain_text().strip()
        matched = re.match(r"^/([A-Za-z]+)(?:\s+(.*))?$", plain, re.DOTALL)
        if matched is None:
            return
        verb = matched.group(1).casefold()
        body = (matched.group(2) or "").strip()

        if verb in {"feedback", "fb"}:
            if not body:
                await self.services.replies._finish_safely(
                    control_command,
                    self.services.replies._reply_message(event, "用法：/feedback 需要补充或修改的内容"),
                )
            replied_id = reply_message_id(event.original_message)
            author = await self.context.db_call(
                self.services.chat._current_user_identity, event
            )
            selected = self.context.running_tasks.push_feedback(
                f"{author}: {body}",
                conversation_id=self.services.chat._conversation_id(event),
                group_id=(
                    event.group_id if isinstance(event, GroupMessageEvent) else None
                ),
                reply_message_id=replied_id,
            )
            if selected is not None:
                await self._ack_control_command(
                    control_command,
                    bot,
                    event,
                    f"反馈已送入 {selected.task_id}。",
                )
            await self.services.replies._finish_tracked_ai(
                control_command,
                bot,
                event,
                body,
                label="feedback fallback reply",
                retry_on_timeout=True,
            )

        if verb == "btw":
            if not body:
                await self.services.replies._finish_safely(
                    control_command,
                    self.services.replies._reply_message(event, "用法：/btw 另一个问题"),
                )
            await self.services.replies._finish_tracked_ai(
                control_command,
                bot,
                event,
                body,
                label="parallel AI reply",
                retry_on_timeout=True,
            )

        if verb == "ps":
            await self.services.replies._finish_safely(
                control_command,
                self.services.replies._reply_message(event, self._task_status_text(event)),
            )

        if verb == "kill":
            task_id = command_targets().get("task_id") or body or None
            stopped = (
                self.context.running_tasks.cancel_for_group(event.group_id, task_id)
                if isinstance(event, GroupMessageEvent)
                else self.context.running_tasks.cancel(self.services.chat._conversation_id(event), task_id)
            )
            if stopped is None:
                await self.services.replies._finish_safely(
                    control_command,
                    self.services.replies._reply_message(event, "当前会话没有匹配的运行任务。发送 /任务 查看。"),
                )
            await self._ack_control_command(
                control_command,
                bot,
                event,
                f"已请求停止 {stopped.task_id}。",
            )

        if verb in {"pin", "unpin"}:
            target_id = await self.context.db_call(
                self._pin_target_message_id, event, body
            )
            if self.context.pin_store is None or self.context.message_ledger is None:
                await self.services.replies._finish_safely(
                    control_command,
                    self.services.replies._reply_message(event, "固定消息功能暂时不可用。"),
                )
            if target_id is None:
                await self.services.replies._finish_safely(
                    control_command,
                    self.services.replies._reply_message(
                        event,
                        f"请引用消息发送 /{verb}，或写 /{verb} msg#编号。",
                    ),
                )
            if verb == "unpin":
                changed = await self.context.db_call(
                    self.context.pin_store.unpin, scope_from_event(event), target_id
                )
                fallback = (
                    f"已取消固定 msg#{target_id}。"
                    if changed
                    else "这条消息没有被固定。"
                )
                if not changed:
                    await self.services.replies._finish_safely(
                        control_command,
                        self.services.replies._reply_message(event, fallback),
                    )
                await self._ack_control_command(
                    control_command,
                    bot,
                    event,
                    fallback,
                )
            scope = scope_from_event(event)
            try:
                pinned, _created = await self.context.db_call(
                    self.context.pin_store.pin,
                    self.context.message_ledger,
                    scope,
                    target_id,
                    pinned_by_principal_id=(
                        await self.context.db_call(
                            self.context.message_ledger.principal_id_for_native,
                            scope.platform,
                            event.user_id,
                        )
                    ),
                )
            except ValueError as exc:
                await self.services.replies._finish_safely(
                    control_command,
                    self.services.replies._reply_message(event, str(exc)),
                )
            await self._ack_control_command(
                control_command,
                bot,
                event,
                f"已固定 msg#{pinned.canonical_message_id}。",
            )

        if verb == "pins":
            if self.context.pin_store is None or self.context.message_ledger is None:
                text = "固定消息功能暂时不可用。"
            else:
                rendered = await self.context.db_call(
                    self.context.pin_store.render,
                    self.context.message_ledger,
                    scope_from_event(event),
                )
                text = rendered or "当前会话还没有固定消息。"
            await self.services.replies._finish_safely(
                control_command,
                self.services.replies._reply_message(event, text),
            )

        if verb == "usage":
            await self.services.replies._finish_safely(
                control_command,
                self.services.replies._reply_message(
                    event, (await self.context.db_call(self._usage_text, event))
                ),
            )

        if verb == "version":
            await self.services.replies._finish_safely(
                control_command,
                self.services.replies._reply_message(
                    event,
                    f"gaoji {BOT_VERSION} · NoneBot2 / OneBot V11 · "
                    "canonical IR + PostgreSQL ledger + durable turn journal",
                ),
            )

        if verb == "help":
            await self.services.replies._finish_safely(
                control_command,
                self.services.replies._reply_message(
                    event,
                    "常用命令：/模型、/effort、/shell、/task 复杂任务、/任务、/停止、"
                    "/feedback、/btw、/pin、/unpin、/pins、/usage、/version。"
                    "普通问题直接 @我。",
                ),
            )

    @mobile_command
    async def handle_pin_command(self,
        event: MessageEvent,
        args: Message = CommandArg(),
    ) -> None:
        target_id = await self.context.db_call(
            self._pin_target_message_id,
            event,
            args.extract_plain_text().strip(),
        )
        if self.context.pin_store is None or self.context.message_ledger is None:
            message = "固定消息功能暂时不可用。"
        elif target_id is None:
            message = "请引用一条消息发送 /pin，或使用 /pin msg#编号。"
        else:
            scope = scope_from_event(event)
            principal_id = await self.context.db_call(
                self.context.message_ledger.principal_id_for_native,
                scope.platform,
                event.user_id,
            )
            try:
                pinned, created = await self.context.db_call(
                    self.context.pin_store.pin,
                    self.context.message_ledger,
                    scope,
                    target_id,
                    pinned_by_principal_id=principal_id,
                )
            except ValueError as exc:
                message = str(exc)
            else:
                verb = "已固定" if created else "这条已经固定过了"
                message = f"{verb}：msg#{pinned.canonical_message_id}。"
        await self.services.replies._finish_safely(
            pin_command,
            self.services.replies._reply_message(event, message),
        )

    @mobile_command
    async def handle_unpin_command(self,
        event: MessageEvent,
        args: Message = CommandArg(),
    ) -> None:
        target_id = await self.context.db_call(
            self._pin_target_message_id,
            event,
            args.extract_plain_text().strip(),
        )
        if self.context.pin_store is None:
            message = "固定消息功能暂时不可用。"
        elif target_id is None:
            message = "请引用一条固定消息发送 /unpin，或使用 /unpin msg#编号。"
        elif await self.context.db_call(
            self.context.pin_store.unpin, scope_from_event(event), target_id
        ):
            message = f"已取消固定：msg#{target_id}。"
        else:
            message = "当前会话没有固定这条消息。"
        await self.services.replies._finish_safely(
            unpin_command,
            self.services.replies._reply_message(event, message),
        )

    async def handle_pins_command(self, event: MessageEvent) -> None:
        if self.context.pin_store is None or self.context.message_ledger is None:
            message = "固定消息功能暂时不可用。"
        else:
            entries = await self.context.db_call(
                self.context.pin_store.messages,
                self.context.message_ledger,
                scope_from_event(event),
            )
            if not entries:
                message = "当前会话还没有固定消息。"
            else:
                lines = ["当前固定消息："]
                lines.extend(
                    f"- msg#{item.canonical_message_id} · "
                    f"{item.sender_display}: {item.rendered_text[:160]}"
                    for _pin, item in entries
                )
                lines.append("\n引用消息发送 /unpin，或输入 /unpin msg#编号 可取消。")
                message = "\n".join(lines)
        await self.services.replies._finish_safely(
            pins_command,
            self.services.replies._reply_message(event, message),
        )

    async def handle_task_status(self,
        bot: Bot,
        event: MessageEvent,
        args: Message = CommandArg(),
    ) -> None:
        objective = args.extract_plain_text().strip()
        if objective and objective.lower() not in {"status", "list", "ls", "状态", "列表"}:
            await self.services.replies._finish_tracked_ai(
                task_status,
                bot,
                event,
                objective,
                label="Sub-Agent task",
                task_mode=True,
            )
            return
        await self.services.replies._finish_safely(
            task_status,
            self.services.replies._reply_message(event, self._task_status_text(event)),
        )

    async def handle_routine_command(self, event: MessageEvent, args: Message) -> None:
        from .routine_commands import handle_routine_command
        await handle_routine_command(self.services, event, args)

    async def handle_project_command(self, event: MessageEvent, args: Message) -> None:
        from .project_commands import handle_project_command
        await handle_project_command(self.services, event, args)

    async def handle_wiki_command(self, event: MessageEvent, args: Message) -> None:
        from .matchers import wiki_command

        wiki = self.services.wiki
        result = await wiki.command(event, args.extract_plain_text()) if wiki is not None else "群百科功能尚未启用。"
        await wiki_command.finish(MessageSegment.text(result))

    async def handle_collaboration_command(self, bot: Bot, event: MessageEvent, args: Message) -> None:
        from .matchers import collaboration_command
        from .collaboration_handlers import collaboration_command as run_command

        result = await run_command(self.services, bot, event, args)
        await collaboration_command.finish(MessageSegment.text(result))

    async def handle_usage_command(self, event: MessageEvent) -> None:
        await self.services.replies._finish_safely(
            usage_command,
            self.services.replies._reply_message(
                event, (await self.context.db_call(self._usage_text, event))
            ),
        )

    @mobile_command
    async def handle_task_stop(self,
        event: MessageEvent,
        args: Message = CommandArg(),
    ) -> None:
        task_id = command_targets().get("task_id") or args.extract_plain_text().strip() or None
        subagent_match = re.fullmatch(r"task#?([1-9][0-9]*)", task_id or "", re.IGNORECASE)
        if subagent_match and self.context.subagent_coordinator is not None:
            subagent_id = int(subagent_match.group(1))
            if self.context.subagent_coordinator.cancel(subagent_id):
                message = f"已请求停止 Sub-Agent 任务 task#{subagent_id}。"
            else:
                message = "这个 Sub-Agent 任务不存在，或已经结束。"
            await self.services.replies._finish_safely(
                task_stop,
                self.services.replies._reply_message(event, message),
            )
            return
        stopped = (
            self.context.running_tasks.cancel_for_group(event.group_id, task_id)
            if isinstance(event, GroupMessageEvent)
            else self.context.running_tasks.cancel(self.services.chat._conversation_id(event), task_id)
        )
        if stopped is None:
            message = "当前会话没有匹配的运行任务。发送 /任务 查看。"
        else:
            message = f"已请求停止任务 {stopped.task_id}。"
        await self.services.replies._finish_safely(
            task_stop,
            self.services.replies._reply_message(event, message),
        )

    async def handle_mention_ai(self, bot: Bot, event: MessageEvent) -> None:
        user_text = event.message.extract_plain_text().strip()
        if not user_text:
            if self.services.chat._has_available_ocr_image(event) or self.services.chat._has_available_voice(event):
                user_text = "请理解我这条消息附带或回复的内容，并自然回答。"
            else:
                user_text = EMPTY_MENTION_FOLLOW_UP

        await self.services.replies._finish_tracked_ai(
            mention_ai,
            bot,
            event,
            user_text,
            label="AI reply",
            retry_on_timeout=True,
        )

    async def handle_sticker(self, event: MessageEvent) -> None:
        await self.services.replies._finish_sticker(sticker, event)

    async def handle_qq_face(self,
        event: MessageEvent, args: Message = CommandArg()
    ) -> None:
        await self.services.replies._finish_qq_face(
            qq_face,
            event,
            args.extract_plain_text().strip(),
        )

    async def handle_sticker_status(self, event: MessageEvent) -> None:
        await self.services.replies._finish_safely(
            sticker_status,
            self.services.replies._reply_message(
                event,
                f"已学习 {learned_sticker_count()} 个 QQ 表情；"
                f"本地内置 {len(list_stickers())} 张图片表情。",
            ),
        )

    @mobile_command
    async def handle_ai_reset(self, event: MessageEvent) -> None:
        conversation_id = self.services.chat._conversation_id(event)
        (await self.context.db_call(self.context.memory.clear, conversation_id))
        scope = scope_from_event(event)
        if self.context.turn_journal is not None:
            (await self.context.db_call(self.context.turn_journal.hide_history, scope))
        if self.context.message_ledger is not None:
            (
                await self.context.db_call(
                    self.context.message_ledger.hide_history, scope
                )
            )
            if self.context.context_store is not None:
                (
                    await self.context.db_call(
                        self.context.context_store.hide_history,
                        scope,
                        (
                            await self.context.db_call(
                                self.context.message_ledger.visible_message_floor, scope
                            )
                        ),
                    )
                )
        if isinstance(event, GroupMessageEvent):
            (
                await self.context.db_call(
                    self.context.group_context.clear, event.group_id
                )
            )
            await self.services.replies._finish_safely(
                ai_reset,
                self.services.replies._reply_message(event, "已清空当前会话记忆和群聊上下文。"),
            )
        await self.services.replies._finish_safely(
            ai_reset,
            self.services.replies._reply_message(event, "已清空当前会话记忆。"),
        )

    @mobile_command
    async def handle_clear_data(self, event: MessageEvent) -> None:
        conversation_id = self.services.chat._conversation_id(event)
        (await self.context.db_call(self.context.memory.clear, conversation_id))
        scope = scope_from_event(event)
        memory_provenance = await self.context.db_call(self._memory_provenance, event)

        cleared_items = ["当前会话记忆"]
        if self.context.turn_journal is not None:
            turn_count = await self.context.db_call(
                self.context.turn_journal.hide_history, scope
            )
            cleared_items.append(f"工作回合 {turn_count} 条")
        if self.context.message_ledger is not None:
            ledger_count = await self.context.db_call(
                self.context.message_ledger.hide_history, scope
            )
            cleared_items.append(f"规范消息上下文 {ledger_count} 条")
            if self.context.context_store is not None:
                compartment_count = await self.context.db_call(
                    self.context.context_store.hide_history,
                    scope,
                    (
                        await self.context.db_call(
                            self.context.message_ledger.visible_message_floor, scope
                        )
                    ),
                )
                cleared_items.append(f"历史摘要 {compartment_count} 条")
        memory_scopes = self._memory_scope_keys(event, "user")
        if self._can_edit_group_memory(event):
            memory_scopes.extend(self._memory_scope_keys(event, "group"))
        long_term_count = await self.context.db_call(
            self.context.long_term_memory.clear,
            memory_scopes,
            **memory_provenance,
            reason="clear command",
        )
        cleared_items.append(f"长期记忆 {long_term_count} 条")
        if await self.context.db_call(
            self.context.model_preferences.clear, conversation_id
        ):
            cleared_items.append("当前模型选择")
        if await self.context.db_call(
            self.context.reasoning_preferences.clear, conversation_id
        ):
            cleared_items.append("当前推理强度")
        if self.context.browser_manager is not None:
            try:
                if await self.context.browser_manager.clear_profile(conversation_id):
                    cleared_items.append("当前用户浏览器登录资料")
            except (OSError, RuntimeError, ValueError) as exc:
                self.context.logger.warning(f"Could not clear browser profile: {exc}")
        if isinstance(event, GroupMessageEvent):
            (
                await self.context.db_call(
                    self.context.group_context.clear, event.group_id
                )
            )
            cleared_items.append("当前群聊上下文")
            profile_count = await self.context.db_call(
                self.context.user_profiles.clear_group, event.group_id
            )
            cleared_items.append(f"当前群成员身份 {profile_count} 个")

        sticker_count = clear_learned_stickers()
        cleared_items.append(f"自动学习表情 {sticker_count} 个")

        await self.services.replies._finish_safely(
            clear_data,
            self.services.replies._reply_message(
                event,
                "已清空：" + "、".join(cleared_items) + "。",
            ),
        )
