"""Decisions from the group attention classifier."""
from dataclasses import dataclass
import re

NOTE_MAX_CHARS = 40
CLASSIFICATION_REASONS = {
    "speak": ("有人寻求机器人帮助", "可以提供具体帮助", "可以纠正错误"),
    "sticker": ("表情足以回应喜讯", "表情足以回应笑话"),
    "silent": ("群友之间的对话", "只是议论机器人", "要求机器人保持沉默", "通知或机器人播报", "没有具体帮助可提供", "不确定是否适合接话"),
}


@dataclass(frozen=True)
class ProactiveDecision:
    action: str
    reason: str
    classification_reason: str | None = None


def parse_proactive_decision(payload: dict) -> ProactiveDecision:
    action = payload.get("action")
    if not isinstance(action, str) or action not in CLASSIFICATION_REASONS:
        raise ValueError("invalid proactive decision")
    basis = payload.get("classification_reason")
    basis = basis if isinstance(basis, str) and basis in CLASSIFICATION_REASONS[action] else None
    if action == "silent":
        return ProactiveDecision(action, "", basis)
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("invalid proactive decision")
    return ProactiveDecision(action, reason.strip()[:NOTE_MAX_CHARS], basis)


_LOW_VALUE_PLACEHOLDERS = frozenset(
    {"[图片]", "[表情]", "[语音]", "[视频]", "[文件]", "[json]"}
)
_URL_ONLY = re.compile(r"^(?:https?://|www\.)\S+$", re.IGNORECASE)


def is_candidate_message(text: str) -> bool:
    normalized = " ".join(str(text).split())
    if not normalized or normalized.startswith("/"):
        return False
    if normalized.casefold() in _LOW_VALUE_PLACEHOLDERS:
        return False
    if _URL_ONLY.fullmatch(normalized):
        return False
    meaningful = re.sub(r"[^\w\u4e00-\u9fff]+", "", normalized)
    return len(meaningful) >= 6
