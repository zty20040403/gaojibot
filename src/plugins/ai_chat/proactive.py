"""A small classifier decides whether an unsolicited reply is useful."""
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ProactiveDecision:
    reply: str


def parse_proactive_decision(payload) -> ProactiveDecision:
    reply = payload.get('reply')
    return ProactiveDecision(reply.strip() if isinstance(reply, str) else '')


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
