"""A small classifier decides whether gaoji should chime in, outputting {trigger, reason}."""
from dataclasses import dataclass
import json
import re

NOTE_MAX_CHARS = 40


@dataclass(frozen=True)
class ProactiveDecision:
    trigger: bool = False
    reason: str = ""


def parse_proactive_decision(payload) -> ProactiveDecision:
    if isinstance(payload, str):
        start = payload.find("{")
        end = payload.rfind("}")
        if start < 0 or end < start:
            return ProactiveDecision(False, "")
        try:
            payload = json.loads(payload[start : end + 1])
        except ValueError:
            return ProactiveDecision(False, "")
    if not isinstance(payload, dict):
        return ProactiveDecision(False, "")
    if "trigger" in payload:
        trigger = payload.get("trigger")
        if not isinstance(trigger, bool):
            return ProactiveDecision(False, "")
        raw_reason = payload.get("reason")
        reason = str(raw_reason).strip()[:NOTE_MAX_CHARS] if isinstance(raw_reason, str) else ""
        return ProactiveDecision(trigger, reason if trigger else "")
    # Compatibility fallback if a legacy test fixture returns {"reply": "..."}
    reply = payload.get("reply")
    if isinstance(reply, str) and reply.strip():
        return ProactiveDecision(True, reply.strip()[:NOTE_MAX_CHARS])
    return ProactiveDecision(False, "")


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
