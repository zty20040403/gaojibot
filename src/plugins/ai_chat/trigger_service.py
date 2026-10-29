"""Trigger Service responsibilities extracted from the plugin entrypoint."""

from __future__ import annotations

import time
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from .context_policy import (
    proactive_context_policy,
)
from .agent_loop import (
    AgentTrace,
    DeepSeekConfigError,
    ask_deepseek_json,
)
from .proactive import (
    ProactiveDecision,
    is_candidate_message,
    parse_proactive_decision,
    should_use_proactive_voice,
)
from .proactive_learning import adaptive_for_group
from .onebot_codec import record_onebot_outgoing, scope_from_event
from .matchers import _addressed_to_current_bot
from .runtime_clock import (
    runtime_clock_prompt,
)
from .stickers import (
    ai_reply_message,
)
from .voice import (
    VoiceError,
    synthesize_silk_voice,
)
from .handler_services import HandlerService
from .matchers import (proactive_chat)


from .application_maintenance import MaintenanceMethods


class TriggerService(MaintenanceMethods, HandlerService):
    async def _semantic_index_loop(self):
        return await super()._semantic_index_loop()

    def _maintenance_profile(self, configured_profile, legacy_model):
        return self.services.chat._background_model_profile(configured_profile, legacy_model)

    async def _maintenance_json(self, *args, **kwargs):
        return await ask_deepseek_json(*args, **kwargs)

    async def _generate_proactive_reply(self,
        event: GroupMessageEvent, latest_text: str, *, threshold: int | None = None
    ) -> ProactiveDecision:
        threshold = (
            self.context.settings.proactive_interest_threshold
            if threshold is None else threshold
        )
        from .application.shared_proactive import proactive_prompt
        system_prompt = proactive_prompt(self.context.settings.system_prompt, runtime_clock_prompt(), threshold)
        user_text = (
            "最近群聊：\n"
            f"{(await self.context.db_call(self.services.chat._current_group_context, event, policy=proactive_context_policy()))}\n\n"
            "当前待判断消息：\n"
            f"{(await self.context.db_call(self.services.chat._current_user_identity, event))}: {latest_text}"
        )

        try:
            selected_profile = (
                self.context.model_catalog.try_resolve(self.context.settings.proactive_classifier_profile)
                or self.context.model_catalog.default
            )
            trace = AgentTrace(
                provider=selected_profile.provider_identity,
                model=selected_profile.model,
                profile=selected_profile.name,
            )
            payload = await ask_deepseek_json(
                system_prompt,
                user_text,
                profile=selected_profile,
                trace=trace,
            )
            (
                await self.context.db_call(
                    self._record_background_usage,
                    self.services.chat._conversation_scope(event).key,
                    "proactive-interest",
                    trace,
                )
            )
        except DeepSeekConfigError:
            self.context.logger.warning(
                "Proactive chat skipped: model profile is not configured."
            )
            return ProactiveDecision(0, "", False)
        except RuntimeError as exc:
            self.context.logger.warning(f"Proactive model request failed: {exc}")
            return ProactiveDecision(0, "", False)
        except Exception as exc:
            self.context.logger.exception(f"Unexpected proactive chat error: {exc}")
            return ProactiveDecision(0, "", False)

        decision = parse_proactive_decision(payload)
        if len(decision.reply) > self.context.settings.proactive_max_reply_chars:
            decision = ProactiveDecision(
                decision.interest,
                decision.reply[: self.context.settings.proactive_max_reply_chars].rstrip(),
                decision.voice_suitable,
            )
        return decision











    async def handle_proactive_chat(self, bot: Bot, event: MessageEvent) -> None:
        del bot
        if not self.context.settings.proactive_enabled:
            return
        if not isinstance(event, GroupMessageEvent):
            return
        if event.user_id == event.self_id:
            return
        if _addressed_to_current_bot(event):
            return
        if not (
            await self.context.db_call(self.services.group_enabled, event.group_id)
        ):
            return

        latest_text = self.services.chat._render_message_text(event.original_message)
        if not is_candidate_message(latest_text):
            return
        if not self.services.proactive_gate.allows(
            event.group_id,
            percent=self.context.settings.proactive_gate_percent,
            max_checks_per_hour=self.context.settings.proactive_max_checks_per_hour,
        ):
            return

        learning = self.context.proactive_learning
        adaptive = learning is not None and adaptive_for_group(
            self.context.settings, event.group_id
        )
        scope = scope_from_event(event)
        threshold = self.context.settings.proactive_interest_threshold
        if adaptive:
            now = int(time.time())
            if event.time < now - 60 or event.time > now + 300:
                return
            try:
                threshold = await self.context.db_call(
                    learning.choose_threshold, scope, str(event.self_id)
                )
            except (OSError, RuntimeError, ValueError) as exc:
                self.context.logger.warning(f"Adaptive proactive check skipped: {type(exc).__name__}")
                return
            decision = await self._generate_proactive_reply(event, latest_text, threshold=threshold)
        else:
            decision = await self._generate_proactive_reply(event, latest_text)
        if not decision.should_reply(threshold):
            return

        outgoing: Message | str = ai_reply_message(decision.reply, latest_text)
        if (
            self.context.settings.voice_enabled
            and decision.voice_suitable
            and should_use_proactive_voice(self.context.settings.proactive_voice_percent)
        ):
            try:
                audio, _speech_text = await synthesize_silk_voice(
                    decision.reply,
                    provider=self.context.settings.voice_provider,
                    voice_name=self.context.settings.voice_name,
                    rate=self.context.settings.voice_rate,
                    pitch=self.context.settings.voice_pitch,
                    local_voice_name=self.context.settings.voice_local_name,
                    local_rate=self.context.settings.voice_local_rate,
                    max_chars=self.context.settings.voice_max_chars,
                    timeout_seconds=self.context.settings.voice_timeout_seconds,
                )
                outgoing = Message([MessageSegment.record(audio)])
            except VoiceError as exc:
                self.context.logger.warning(f"Proactive voice generation fell back to text: {exc}")

        if not await self.context.db_call(self.services.group_enabled, event.group_id):
            return
        trial_id = None
        if adaptive:
            try:
                trial_id = await self.context.db_call(
                    learning.begin, scope, str(event.self_id),
                    str(event.message_id), threshold, decision.interest,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                self.context.logger.warning(f"Adaptive proactive send skipped: {type(exc).__name__}")
                return
            if trial_id is None:
                return

        async def confirmed(response, message, _attempt):
            native_id = self.services.replies._sent_message_id(response)
            if trial_id is not None:
                await self.context.db_call(learning.sent, trial_id, str(native_id or ""))
            if native_id is not None and self.context.message_ledger is not None:
                await self.context.db_call(
                    record_onebot_outgoing, self.context.message_ledger,
                    scope, native_message_id=native_id, message=message,
                    occurred_at=int(time.time()),
                )

        async def unknown(*_args):
            if trial_id is not None:
                await self.context.db_call(learning.unsent, trial_id, "unknown")

        async def failed(*_args):
            if trial_id is not None:
                await self.context.db_call(learning.unsent, trial_id, "failed")

        sent = await self.services.replies._finish_safely(
            proactive_chat,
            outgoing,
            "proactive reply",
            retry_on_timeout=isinstance(outgoing, str) and not adaptive,
            on_sent=confirmed,
            on_outcome_unknown=unknown,
            on_failed=failed,
            finish=False,
        )
        if sent and self.context.message_ledger is None:
            (
                await self.context.db_call(
                    self.context.group_context.append,
                    event.group_id,
                    "机器人",
                    decision.reply,
                )
            )
