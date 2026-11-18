"""Decisions from the group attention classifier."""
from dataclasses import dataclass
import re

NOTE_MAX_CHARS = 40


@dataclass(frozen=True)
class ProactiveDecision:
    action: str
    reason: str


def parse_proactive_decision(payload: dict) -> ProactiveDecision:
    action = payload.get("action")
    if action == "silent":
        return ProactiveDecision(action, "")
    reason = payload.get("reason")
    if action not in ("speak", "sticker") or not isinstance(reason, str) or not reason.strip():
        raise ValueError("invalid proactive decision")
    return ProactiveDecision(action, reason.strip()[:NOTE_MAX_CHARS])


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
