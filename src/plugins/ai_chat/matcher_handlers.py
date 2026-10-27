"""Typed matcher adapters; service instances are resolved after driver startup."""

from __future__ import annotations

from nonebot.adapters.onebot.v11 import Bot, GroupIncreaseNoticeEvent, Message, MessageEvent, NoticeEvent
from nonebot.params import CommandArg

from . import matchers
from .onebot_runtime import OneBotRuntime


def register_matcher_handlers(runtime: OneBotRuntime) -> None:
    async def handle_ai(
        bot: Bot, event: MessageEvent, args: Message = CommandArg()
    ) -> None:
        await runtime.handlers.commands.handle_ai(bot, event, args)

    async def handle_web_search(
        event: MessageEvent, args: Message = CommandArg()
    ) -> None:
        await runtime.handlers.commands.handle_web_search(event, args)

    async def handle_image_ocr(
        bot: Bot, event: MessageEvent, args: Message = CommandArg()
    ) -> None:
        await runtime.handlers.commands.handle_image_ocr(bot, event, args)

    async def handle_voice_answer(
        bot: Bot, event: MessageEvent, args: Message = CommandArg()
    ) -> None:
        await runtime.handlers.commands.handle_voice_answer(bot, event, args)

    async def handle_voice_transcription(
        bot: Bot, event: MessageEvent, args: Message = CommandArg()
    ) -> None:
        await runtime.handlers.commands.handle_voice_transcription(bot, event, args)

    async def handle_model_command(
        event: MessageEvent, args: Message = CommandArg()
    ) -> None:
        await runtime.handlers.commands.handle_model_command(event, args)

    async def handle_memory_command(
        event: MessageEvent, args: Message = CommandArg()
    ) -> None:
        await runtime.handlers.commands.handle_memory_command(event, args)

    async def handle_control_command(bot: Bot, event: MessageEvent) -> None:
        await runtime.handlers.commands.handle_control_command(bot, event)

    async def handle_effort_command(
        event: MessageEvent, args: Message = CommandArg()
    ) -> None:
        await runtime.handlers.commands.handle_effort_command(event, args)

    async def handle_shell_command(
        event: MessageEvent, args: Message = CommandArg()
    ) -> None:
        await runtime.handlers.commands.handle_shell_command(event, args)

    async def handle_pin_command(
        event: MessageEvent, args: Message = CommandArg()
    ) -> None:
        await runtime.handlers.commands.handle_pin_command(event, args)

    async def handle_unpin_command(
        event: MessageEvent, args: Message = CommandArg()
    ) -> None:
        await runtime.handlers.commands.handle_unpin_command(event, args)

    async def handle_pins_command(event: MessageEvent) -> None:
        await runtime.handlers.commands.handle_pins_command(event)

    async def handle_task_status(
        bot: Bot, event: MessageEvent, args: Message = CommandArg()
    ) -> None:
        await runtime.handlers.commands.handle_task_status(bot, event, args)

    async def handle_usage_command(event: MessageEvent) -> None:
        await runtime.handlers.commands.handle_usage_command(event)

    async def handle_routine_command(event: MessageEvent, args: Message = CommandArg()) -> None:
        await runtime.handlers.commands.handle_routine_command(event, args)

    async def handle_project_command(event: MessageEvent, args: Message = CommandArg()) -> None:
        await runtime.handlers.commands.handle_project_command(event, args)

    async def handle_wiki_command(event: MessageEvent, args: Message = CommandArg()) -> None:
        await runtime.handlers.commands.handle_wiki_command(event, args)

    async def handle_collaboration_command(bot: Bot, event: MessageEvent, args: Message = CommandArg()) -> None:
        await runtime.handlers.commands.handle_collaboration_command(bot, event, args)

    async def handle_task_stop(
        event: MessageEvent, args: Message = CommandArg()
    ) -> None:
        await runtime.handlers.commands.handle_task_stop(event, args)

    async def handle_mention_ai(bot: Bot, event: MessageEvent) -> None:
        await runtime.handlers.commands.handle_mention_ai(bot, event)

    async def handle_canonical_ingest(event: MessageEvent) -> None:
        await runtime.handlers.ingest.handle_canonical_ingest(event)

    async def handle_reaction_notice(bot: Bot, event: NoticeEvent) -> None:
        await runtime.handlers.ingest.handle_reaction_notice(bot, event)

    async def handle_proactive_notice(bot: Bot, event: NoticeEvent) -> None:
        await runtime.handlers.ingest.handle_proactive_notice(bot, event)

    async def handle_wiki_newcomer(event: GroupIncreaseNoticeEvent) -> None:
        if runtime.handlers.wiki is not None:
            await runtime.handlers.wiki.newcomer_notice(event)

    async def handle_group_activity(event: MessageEvent) -> None:
        await runtime.handlers.ingest.handle_group_activity(event)

    async def handle_image_auto_description(event: MessageEvent) -> None:
        await runtime.handlers.ingest.handle_image_auto_description(event)

    async def handle_proactive_chat(bot: Bot, event: MessageEvent) -> None:
        await runtime.handlers.triggers.handle_proactive_chat(bot, event)

    async def handle_sticker(event: MessageEvent) -> None:
        await runtime.handlers.commands.handle_sticker(event)

    async def handle_qq_face(event: MessageEvent, args: Message = CommandArg()) -> None:
        await runtime.handlers.commands.handle_qq_face(event, args)

    async def handle_sticker_status(event: MessageEvent) -> None:
        await runtime.handlers.commands.handle_sticker_status(event)

    async def handle_group_context_recorder(event: MessageEvent) -> None:
        await runtime.handlers.ingest.handle_group_context_recorder(event)

    async def handle_ai_reset(event: MessageEvent) -> None:
        await runtime.handlers.commands.handle_ai_reset(event)

    async def handle_clear_data(event: MessageEvent) -> None:
        await runtime.handlers.commands.handle_clear_data(event)

    for matcher, handler in (
        (matchers.ai, handle_ai),
        (matchers.web_search, handle_web_search),
        (matchers.image_ocr, handle_image_ocr),
        (matchers.voice_answer, handle_voice_answer),
        (matchers.voice_transcription, handle_voice_transcription),
        (matchers.model_command, handle_model_command),
        (matchers.memory_command, handle_memory_command),
        (matchers.control_command, handle_control_command),
        (matchers.effort_command, handle_effort_command),
        (matchers.shell_command, handle_shell_command),
        (matchers.pin_command, handle_pin_command),
        (matchers.unpin_command, handle_unpin_command),
        (matchers.pins_command, handle_pins_command),
        (matchers.task_status, handle_task_status),
        (matchers.usage_command, handle_usage_command),
        (matchers.routine_command, handle_routine_command),
        (matchers.project_command, handle_project_command),
        (matchers.wiki_command, handle_wiki_command),
        (matchers.collaboration_command, handle_collaboration_command),
        (matchers.task_stop, handle_task_stop),
        (matchers.mention_ai, handle_mention_ai),
        (matchers.canonical_ingest_tracker, handle_canonical_ingest),
        (matchers.reaction_feedback_tracker, handle_reaction_notice),
        (matchers.proactive_feedback_tracker, handle_proactive_notice),
        (matchers.wiki_newcomer_tracker, handle_wiki_newcomer),
        (matchers.group_activity_tracker, handle_group_activity),
        (matchers.image_auto_description, handle_image_auto_description),
        (matchers.proactive_chat, handle_proactive_chat),
        (matchers.sticker, handle_sticker),
        (matchers.qq_face, handle_qq_face),
        (matchers.sticker_status, handle_sticker_status),
        (matchers.group_context_recorder, handle_group_context_recorder),
        (matchers.ai_reset, handle_ai_reset),
        (matchers.clear_data, handle_clear_data),
    ):
        matcher.handle()(handler)
