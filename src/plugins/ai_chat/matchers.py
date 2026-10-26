from __future__ import annotations

import re

from nonebot import on_command, on_message, on_notice, on_regex
from nonebot.adapters.onebot.v11 import MessageEvent, NoticeEvent
from nonebot.rule import Rule


def _mentions_current_bot(event: MessageEvent) -> bool:
    if event.user_id == event.self_id:
        return False
    return any(
        segment.type == "at"
        and str(segment.data.get("qq") or "") == str(event.self_id)
        for segment in event.original_message
    )


def _addressed_to_current_bot(event: MessageEvent) -> bool:
    return event.is_tome() or _mentions_current_bot(event)


def _is_reaction_notice(event: NoticeEvent) -> bool:
    return event.notice_type == "group_msg_emoji_like"


ai = on_command("ai", aliases={"ds", "deepseek", "问"}, priority=10, block=True)
web_search = on_command(
    "搜", aliases={"搜索", "联网搜索", "查一下", "search"}, priority=10, block=True
)
image_ocr = on_command(
    "ocr",
    aliases={"OCR", "识图", "图片文字", "看图"},
    priority=10,
    block=True,
)
voice_answer = on_command(
    "语音",
    aliases={"语音回答", "voice"},
    priority=10,
    block=True,
)
voice_transcription = on_command(
    "听",
    aliases={"听语音", "语音识别", "语音转文字"},
    priority=10,
    block=True,
)
model_command = on_command(
    "模型",
    aliases={"model", "切换模型"},
    priority=10,
    block=True,
)
effort_command = on_command(
    "effort",
    aliases={"推理强度", "思考强度", "reasoning"},
    priority=10,
    block=True,
)
shell_command = on_command(
    "shell",
    aliases={"终端", "执行", "cmd"},
    priority=9,
    block=True,
)
memory_command = on_command(
    "记忆", aliases={"memory", "长期记忆"}, priority=10, block=True
)
pin_command = on_command(
    "pin", aliases={"固定", "固定消息"}, priority=10, block=True
)
unpin_command = on_command(
    "unpin", aliases={"取消固定"}, priority=10, block=True
)
pins_command = on_command(
    "pins", aliases={"固定列表"}, priority=10, block=True
)
control_command = on_regex(
    r"^/(?:feedback|fb|btw|help|version)(?:\s|$)",
    flags=re.IGNORECASE,
    priority=9,
    block=True,
)
task_status = on_command(
    "任务", aliases={"task", "tasks", "ps"}, priority=10, block=True
)
usage_command = on_command(
    "usage", aliases={"用量", "token用量"}, priority=10, block=True
)
task_stop = on_command(
    "停止", aliases={"stop", "kill", "取消任务"}, priority=10, block=True
)
ai_reset = on_command("ai_reset", aliases={"清空记忆"}, priority=10, block=True)
clear_data = on_command(
    "clear",
    aliases={"清空上下文", "清空数据", "清空存储", "重置数据"},
    priority=10,
    block=True,
)
sticker = on_command(
    "表情", aliases={"表情包", "贴纸", "meme", "sticker"}, priority=10, block=True
)
qq_face = on_command(
    "qq表情",
    aliases={"QQ表情", "自带表情", "小黄脸", "face"},
    priority=10,
    block=True,
)
sticker_status = on_command(
    "表情状态", aliases={"表情库", "表情数量"}, priority=10, block=True
)
mention_ai = on_message(
    rule=Rule(_addressed_to_current_bot),
    priority=20,
    block=True,
)
canonical_ingest_tracker = on_message(priority=0, block=False)
reaction_feedback_tracker = on_notice(rule=Rule(_is_reaction_notice), priority=5, block=False)
group_activity_tracker = on_message(priority=1, block=False)
image_auto_description = on_message(priority=70, block=False)
proactive_chat = on_message(priority=80, block=False)
group_context_recorder = on_message(priority=90, block=False)
