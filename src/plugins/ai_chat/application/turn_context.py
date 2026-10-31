"""What one chat turn shows the model: the chat tail, the memory file and the quoted message."""
from __future__ import annotations

from ..conversation_window import MEMORY_TOKENS, build_tail
from ..platform_types import InboundMessage
from ..timeline import estimate_tokens, render_line
from ..turn_journal import tool_effect_labels


def tail_budget(profile) -> int:
    """High watermark for the raw tail: an eighth of the window, between 8k and 64k tokens."""
    window = getattr(profile, "max_input_tokens", None) or 128_000
    return max(8_000, min(window // 8, 64_000))


def render_memory(text: str) -> str:
    """The memory file as injected: whole if it fits, otherwise its first lines and a note."""
    text = text.strip()
    if not text:
        return ""
    if estimate_tokens(text) <= MEMORY_TOKENS:
        if estimate_tokens(text) > MEMORY_TOKENS * 0.8:
            text += "\n（记忆快满了，找机会用 memory_write 整理精简。）"
        return text
    kept, used = [], 0
    for line in text.splitlines():
        used += estimate_tokens(line)
        if used > MEMORY_TOKENS:
            break
        kept.append(line)
    return "\n".join(kept) + "\n（后面的没加载：记忆超过上限了，尽快用 memory_write 整理精简。）"


def turn_window(app, inbound: InboundMessage, profile) -> tuple[str, str, str]:
    ledger, store = app.message_ledger, getattr(app, "conversations", None)
    if ledger is None or store is None:
        return "", "", ""
    scope = inbound.scope
    current = ledger.canonical_id_for_native(scope, inbound.native_message_id, endpoint=inbound.endpoint)
    exclude = frozenset({current}) if current is not None else frozenset()
    tail = build_tail(ledger, store, scope, high_tokens=tail_budget(profile), exclude_ids=exclude)
    quoted = ""
    if inbound.reply_to_native_message_id is not None:
        target = ledger.canonical_id_for_native(scope, inbound.reply_to_native_message_id, endpoint=inbound.endpoint)
        message = ledger.get_in_scope(scope, target) if target is not None else None
        if message is not None:
            quoted = "[引用的消息]\n" + render_line(message, max_chars=4000)
    return tail, render_memory(store.memory(scope.key)), quoted


async def record_loop_event(app, turn_id: int, event) -> None:
    if app.turn_journal is None:
        return
    labels = event.side_effects or tool_effect_labels(event.tool_name)
    metadata = {key: value for key, value in {
        "call_id": event.call_id, "fingerprint": event.fingerprint, "risk": event.risk,
        "idempotency": event.idempotency, "execution_mode": event.execution_mode,
        "approval": event.approval, "duration_ms": event.duration_ms,
    }.items() if value not in {"", 0}}
    journal = app.turn_journal
    if event.kind == "model_note":
        await app.db_call(journal.record_model_note, turn_id, event.sequence, event.note)
    elif event.kind == "tool_started":
        await app.db_call(journal.record_tool_started, turn_id, event.sequence, event.tool_name,
                          event.arguments, labels, metadata)
    elif event.kind == "tool_rejected":
        await app.db_call(journal.record_tool_rejected, turn_id, event.sequence, event.tool_name,
                          event.arguments, event.result, labels, metadata)
    elif event.kind in {"tool_finished", "tool_compensated"}:
        await app.db_call(journal.record_tool_finished, turn_id, event.sequence, event.tool_name,
                          event.state, event.result, labels, metadata)
