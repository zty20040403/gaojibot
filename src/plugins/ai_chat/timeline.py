"""One line per chat message, shared by the context window and the history tool."""

from __future__ import annotations

from datetime import datetime
import re

from .ledger import CanonicalMessage

# Other bots' tool traces and stored error payloads are not conversation.
_NOISE = re.compile(r'^(⚙ |↳ |\{"error"\s*:)')


def is_noise(message: CanonicalMessage) -> bool:
    return not message.prompt_text.strip() or bool(_NOISE.match(message.prompt_text.lstrip()))


def render_line(message: CanonicalMessage, *, max_chars: int | None = None) -> str:
    text = message.prompt_text
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars] + f"…（全文用 history around=#{message.canonical_message_id} 读）"
    when = datetime.fromtimestamp(message.occurred_at).strftime("%m-%d %H:%M")
    who = "你" if message.direction == "outbound" else message.sender_display
    reply = f" 回复#{message.reply_to_canonical_message_id}" if message.reply_to_canonical_message_id else ""
    return f"[{when} {who} #{message.canonical_message_id}{reply}] {text}"
