"""Chit-chat thinks at medium, questions that need knowledge at the profile's own effort.

A small model without reasoning labels each message first. Any failure,
timeout or unknown answer keeps the profile's effort: deeper, never shallower.
"""
from __future__ import annotations

import asyncio
import logging

from .agent_loop import ask_deepseek_json

_logger = logging.getLogger(__name__)
CHAT_EFFORT = "medium"
TIMEOUT_SECONDS = 6
PROMPT = (
    "判断这条群聊消息属于哪一类，只输出 JSON。\n"
    "chat：打招呼、接梗、吐槽、开玩笑、随口感慨、闲聊、简单回应、要表情包。\n"
    "question：需要专业知识或认真推理才能答好，比如技术、编程、学术、医疗、法律、财经、数学计算、"
    "分析比较、写文章或方案、查证事实。\n"
    "拿不准时选 question。输出 {\"kind\": \"chat\"} 或 {\"kind\": \"question\"}。"
)


async def chat_effort(catalog, router_profile: str, text: str) -> str | None:
    """CHAT_EFFORT for chit-chat; None means keep the profile's effort."""
    if not router_profile or not text.strip():
        return None
    try:
        profile = catalog.resolve(router_profile)
        async with asyncio.timeout(TIMEOUT_SECONDS):
            answer = await ask_deepseek_json(PROMPT, text[:2000], profile=profile, recover_empty_response=False)
    except Exception as error:
        _logger.warning("Reasoning router failed, keeping the profile effort: %s", type(error).__name__)
        return None
    return CHAT_EFFORT if answer.get("kind") == "chat" else None
