"""Transient image/video understanding and per-turn voice output."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from nonebot.adapters.onebot.v11 import Bot, MessageEvent, MessageSegment
from nonebot.adapters.onebot.v11.exception import ActionFailed

from src.bot_storage import DatabaseError
from ..ai_tools import (
    READ_IMAGE_TEXT_TOOL,
    VIEW_IMAGE_TOOL,
    VIEW_VIDEO_TOOL,
    TRANSCRIBE_VOICE_TOOL,
    REPLY_WITH_VOICE_TOOL,
)
from ..ocr import OCRError, recognize_images, reply_message_id
from ..model_budget import ModelBudgetExceeded
from ..onebot_codec import scope_from_event
from ..video import (
    VideoReference,
    contains_video,
    indexed_video_sources,
    message_video_sources,
    replied_video_message_id,
)
from ..video_analysis import DeepVideoAnalysisError
from ..voice import VoiceError, synthesize_silk_voice, transcribe_voice
from .registry import FunctionTool
from .state import ToolContext, TurnState

if TYPE_CHECKING:
    from ..handler_services import HandlerServices


def ocr_enabled(context: ToolContext) -> bool:
    return "ocr" in context.capabilities


def image_enabled(context: ToolContext) -> bool:
    return "media" in context.capabilities


def video_enabled(context: ToolContext) -> bool:
    return "video" in context.capabilities


def transcription_enabled(context: ToolContext) -> bool:
    return "transcription" in context.capabilities


def voice_enabled(context: ToolContext) -> bool:
    return "voice" in context.capabilities


async def resolve_video_reference(
    services: HandlerServices,
    bot: Bot,
    event: MessageEvent,
    *,
    message_handle: str = "",
    segment_index: int | None = None,
) -> VideoReference | None:
    scope = scope_from_event(event)
    requested_handle = str(message_handle).strip()
    if requested_handle:
        canonical_id = services.commands._canonical_message_id(requested_handle)
        target = (
            (
                await services.context.db_call(
                    services.context.message_ledger.get_in_scope, scope, canonical_id
                )
            )
            if services.context.message_ledger is not None and canonical_id is not None
            else None
        )
        if target is None or not target.native_message_id:
            raise ValueError("当前会话看不到这条视频消息。")
        native_message_id = int(target.native_message_id)
        source_items = await message_video_sources(bot, native_message_id)
    else:
        native_message_id = int(event.message_id)
        source_items = indexed_video_sources(event.original_message)
        if contains_video(event.original_message) and not source_items:
            source_items = await message_video_sources(bot, native_message_id)
        if not source_items:
            replied_id = replied_video_message_id(event.original_message)
            if replied_id is not None:
                replied_sources = await message_video_sources(bot, replied_id)
                if replied_sources:
                    native_message_id = replied_id
                    source_items = replied_sources
        if not source_items:
            recent_id = services.context.recent_videos.get(
                services.chat._video_cache_key(event)
            )
            if recent_id is not None:
                recent_sources = await message_video_sources(bot, recent_id)
                if recent_sources:
                    native_message_id = recent_id
                    source_items = recent_sources
    if segment_index is None:
        selected = source_items[0] if source_items else None
    else:
        selected = next(
            (item for item in source_items if item[0] == int(segment_index)),
            None,
        )
    if selected is None:
        return None
    selected_index, source_url = selected
    return VideoReference(native_message_id, selected_index, source_url)


async def read_image_text(turn: TurnState, arguments: dict[str, object]) -> str:
    if not turn.image_sources:
        return json.dumps(
            {"ok": False, "error": "本轮没有可读取的图片。"},
            ensure_ascii=False,
        )
    try:
        text = await recognize_images(
            turn.bot,
            turn.image_sources,
            timeout_seconds=turn.context.app.settings.ocr_timeout_seconds,
            max_chars=turn.context.app.settings.ocr_max_chars,
        )
    except OCRError as exc:
        turn.context.app.logger.warning(f"Image OCR tool failed: {exc}")
        return json.dumps(
            {"ok": False, "error": "图片文字识别暂时失败。"},
            ensure_ascii=False,
        )
    if not text:
        return json.dumps(
            {
                "ok": False,
                "error": "图片中没有识别到清晰文字，无法理解纯画面。",
            },
            ensure_ascii=False,
        )
    turn.used_ocr_texts.append(text)
    return json.dumps(
        {"ok": True, "text": text},
        ensure_ascii=False,
    )


async def view_image(turn: TurnState, arguments: dict[str, object]) -> str:
    if turn.context.app.vision_worker is None:
        return json.dumps(
            {"ok": False, "error": "图片理解服务暂时不可用。"},
            ensure_ascii=False,
        )
    scope = scope_from_event(turn.context.event)
    native_message_id: str | int = turn.context.event.message_id
    source_items: list[tuple[int, str]] = []
    requested_handle = str(arguments.get("message_handle") or "").strip()
    try:
        if requested_handle:
            canonical_id = turn.context.services.commands._canonical_message_id(
                requested_handle
            )
            target = (
                (
                    await turn.context.app.db_call(
                        turn.context.app.message_ledger.get_in_scope,
                        scope,
                        canonical_id,
                    )
                )
                if turn.context.app.message_ledger is not None
                and canonical_id is not None
                else None
            )
            if target is None or not target.native_message_id:
                return json.dumps(
                    {"ok": False, "error": "当前会话看不到这条图片消息。"},
                    ensure_ascii=False,
                )
            native_message_id = target.native_message_id
            raw_target = await turn.bot.get_msg(message_id=int(native_message_id))
            source_items = turn.context.services.chat._indexed_image_sources(
                raw_target.get("message") if isinstance(raw_target, dict) else None
            )
        else:
            source_items = turn.context.services.chat._indexed_image_sources(
                turn.context.event.original_message
            )
            if not source_items:
                replied_id = reply_message_id(turn.context.event.original_message)
                if replied_id is not None:
                    native_message_id = replied_id
                    raw_target = await turn.bot.get_msg(message_id=replied_id)
                    source_items = turn.context.services.chat._indexed_image_sources(
                        raw_target.get("message")
                        if isinstance(raw_target, dict)
                        else None
                    )
            if not source_items:
                recent_sources = turn.context.app.recent_images.get(
                    turn.context.services.chat._image_cache_key(turn.context.event)
                )
                source_items = list(enumerate(recent_sources))
        raw_segment_index = arguments.get("segment_index")
        if raw_segment_index is not None:
            requested_index = int(raw_segment_index)
            selected = next(
                (item for item in source_items if item[0] == requested_index),
                None,
            )
        else:
            selected = source_items[0] if source_items else None
        if selected is None:
            return json.dumps(
                {
                    "ok": False,
                    "error": (
                        "没有找到可识别的图片，请发送图片、回复图片，"
                        "或检查 segment_index。"
                    ),
                },
                ensure_ascii=False,
            )
        segment_index, source_url = selected
        mode = str(arguments.get("mode") or "summary").strip().lower()
        question = str(arguments.get("question") or "").strip()
        result = await turn.context.app.vision_worker.submit_and_wait(
            scope_key=scope.key,
            native_message_id=native_message_id,
            segment_index=segment_index,
            requester_native_user_id=turn.context.event.user_id,
            source_url=source_url,
            mode=mode,
            question=question,
            wait_seconds=min(turn.context.app.settings.media_timeout_seconds, 45),
        )
    except ModelBudgetExceeded:
        raise
    except (
        ActionFailed,
        OSError,
        RuntimeError,
        ValueError,
        DatabaseError,
    ) as exc:
        turn.context.app.logger.warning(f"Transient vision tool failed: {exc}")
        result = None
    if result is None:
        return json.dumps(
            {
                "ok": False,
                "error": "这次识图没有及时完成，请重试；图片不会保存到媒体库。",
            },
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "ok": True,
            **result.as_dict(),
            "stored": False,
            "message": "本次结果已消费，仔细查看时会重新调用视觉模型。",
        },
        ensure_ascii=False,
    )


async def view_video(turn: TurnState, arguments: dict[str, object]) -> str:
    if turn.context.services.video_analyzer is None:
        return json.dumps(
            {"ok": False, "error": "视频分析服务暂时不可用。"},
            ensure_ascii=False,
        )
    try:
        raw_index = arguments.get("segment_index")
        target_video = await resolve_video_reference(
            turn.context.services,
            turn.bot,
            turn.context.event,
            message_handle=str(arguments.get("message_handle") or ""),
            segment_index=(int(raw_index) if raw_index is not None else None),
        )
        if target_video is None:
            return json.dumps(
                {
                    "ok": False,
                    "error": (
                        "没有找到可分析的视频，请发送视频、回复视频，"
                        "或检查 message_handle 和 segment_index。"
                    ),
                },
                ensure_ascii=False,
            )
        result = await turn.context.services.video_analyzer.analyze_qq_video(
            target_video.source_url,
            question=str(arguments.get("question") or "").strip(),
        )
    except ModelBudgetExceeded:
        raise
    except (ActionFailed, OSError, RuntimeError, ValueError) as exc:
        turn.context.app.logger.warning(f"QQ video analysis tool failed: {exc}")
        error = (
            str(exc)
            if isinstance(exc, (DeepVideoAnalysisError, ValueError))
            else "视频读取或分析失败，请稍后重试。"
        )
        return json.dumps(
            {"ok": False, "error": error},
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "ok": True,
            "segment_index": target_video.segment_index,
            **result,
        },
        ensure_ascii=False,
    )


async def transcribe(turn: TurnState, arguments: dict[str, object]) -> str:
    if turn.voice_message_id is None:
        return json.dumps(
            {"ok": False, "error": "本轮没有可读取的 QQ 语音。"},
            ensure_ascii=False,
        )
    try:
        text = await transcribe_voice(
            turn.bot,
            turn.voice_message_id,
            timeout_seconds=turn.context.app.settings.voice_timeout_seconds,
        )
    except VoiceError as exc:
        turn.context.app.logger.warning(f"Voice transcription tool failed: {exc}")
        return json.dumps(
            {"ok": False, "error": "QQ 语音转文字暂时失败。"},
            ensure_ascii=False,
        )
    if not text:
        return json.dumps(
            {"ok": False, "error": "这条语音没有识别出清晰文字。"},
            ensure_ascii=False,
        )
    turn.used_voice_texts.append(text)
    return json.dumps(
        {"ok": True, "text": text},
        ensure_ascii=False,
    )


async def reply_voice(turn: TurnState, arguments: dict[str, object]) -> str:
    text = str(arguments.get("text", "")).strip()
    if not text:
        return json.dumps(
            {"ok": False, "error": "没有提供要朗读的回答。"},
            ensure_ascii=False,
        )
    if turn.voice_reply_segment is not None:
        return json.dumps(
            {"ok": True, "message": "语音回复已经生成。"},
            ensure_ascii=False,
        )
    try:
        audio, speech_text = await synthesize_silk_voice(
            text,
            provider=turn.context.app.settings.voice_provider,
            voice_name=turn.context.app.settings.voice_name,
            rate=turn.context.app.settings.voice_rate,
            pitch=turn.context.app.settings.voice_pitch,
            local_voice_name=turn.context.app.settings.voice_local_name,
            local_rate=turn.context.app.settings.voice_local_rate,
            max_chars=turn.context.app.settings.voice_max_chars,
            timeout_seconds=turn.context.app.settings.voice_timeout_seconds,
        )
    except VoiceError as exc:
        turn.context.app.logger.warning(f"Voice reply tool failed: {exc}")
        return json.dumps(
            {"ok": False, "error": "本地 QQ 语音生成暂时失败。"},
            ensure_ascii=False,
        )
    turn.voice_reply_segment = MessageSegment.record(audio)
    turn.voice_reply_text = speech_text
    return json.dumps(
        {"ok": True, "message": "语音回复已经生成并等待发送。"},
        ensure_ascii=False,
    )


HANDLERS = (
    FunctionTool(READ_IMAGE_TEXT_TOOL, ocr_enabled, read_image_text),
    FunctionTool(VIEW_IMAGE_TOOL, image_enabled, view_image),
    FunctionTool(VIEW_VIDEO_TOOL, video_enabled, view_video),
    FunctionTool(TRANSCRIBE_VOICE_TOOL, transcription_enabled, transcribe),
    FunctionTool(REPLY_WITH_VOICE_TOOL, voice_enabled, reply_voice),
)
