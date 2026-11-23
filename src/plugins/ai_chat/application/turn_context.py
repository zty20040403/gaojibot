"""What one chat turn shows the model: the chat tail, the memory file and the quoted message."""
from __future__ import annotations

from ..conversation_window import MEMORY_TOKENS, build_tail
from ..ledger import CanonicalMessage
from ..message_ir import render_prompt_text
from ..platform_types import InboundMessage
from ..prompt_context import quote_evidence
from ..stances import render_stances, verified_stances
from ..status_line import compute_status_line
from ..timeline import estimate_tokens
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


def speaker(inbound: InboundMessage, *, reply_to_id: int | None = None) -> str:
    """The new message's speaker; in a group, marks one that @'s or replies to the bot.

    The @ itself is stripped from the text, so without the mark the model cannot
    tell an insult aimed at it from one between members.
    """
    name = inbound.sender_display or "群友"
    if inbound.mentions_bot and inbound.endpoint.kind == "group":
        name += "（@你）"
    if reply_to_id is not None:
        name += f" 回复#{reply_to_id}"
    return name


def build_roster(ledger, inbound: InboundMessage, *, limit: int = 80) -> str:
    """Render [上下文里的人] ([mention#id] 名字) for group conversations."""
    if ledger is None or inbound.endpoint.kind != "group":
        return ""
    rows = ledger.history(inbound.scope, limit=400, newest=True)
    seen: dict[int, tuple[str, bool]] = {}
    for message in reversed(rows):
        if message.source_endpoint_key and message.source_endpoint_key != inbound.endpoint.key:
            continue
        pid = message.sender_principal_id
        if pid is None or pid in seen:
            continue
        is_bot = message.direction == "outbound"
        name = ("gaoji" if is_bot else (message.sender_display or f"群友{pid}")).replace("\n", " ").replace("\r", " ").strip()
        seen[pid] = (name or f"群友{pid}", is_bot)
        if len(seen) >= limit:
            break
    if not seen:
        return ""
    lines = [
        f"[mention#{pid}] {name}{'（你）' if is_bot else ''}"
        for pid, (name, is_bot) in sorted(seen.items())
    ]
    return "[上下文里的人]\n" + "\n".join(lines)


def turn_window(
    app, inbound: InboundMessage, profile, *, extra_exclude_ids: frozenset[int] = frozenset(),
) -> tuple[str, str, str, CanonicalMessage | None]:
    ledger, store = app.message_ledger, getattr(app, "conversations", None)
    if ledger is None or store is None:
        return "", "", "", None
    scope = inbound.scope
    current = ledger.canonical_id_for_native(scope, inbound.native_message_id, endpoint=inbound.endpoint)
    exclude = frozenset((*inbound.batch_message_ids, *extra_exclude_ids, *((current,) if current is not None else ())))
    tail = build_tail(
        ledger, store, scope, high_tokens=tail_budget(profile),
        exclude_ids=exclude, before_id=current if not inbound.batch_message_ids else None,
    )
    parts: list[str] = []
    speakers: list[tuple[str, str]] = [(inbound.actor.key, inbound.sender_display)]
    for cid in inbound.batch_message_ids:
        batched = ledger.get_in_scope(scope, cid)
        if batched is not None and batched.direction == "inbound":
            speakers.append((batched.sender_actor_key, batched.sender_display))
    roster = build_roster(ledger, inbound)
    if roster:
        parts.append(roster)
    reply = None
    if inbound.reply_to_native_message_id is not None:
        target = ledger.canonical_id_for_native(scope, inbound.reply_to_native_message_id, endpoint=inbound.endpoint)
        reply = ledger.get_in_scope(scope, target) if target is not None else None
        if reply is not None and reply.direction == "inbound":
            speakers.append((reply.sender_actor_key, reply.sender_display))
    self_files = getattr(app, "self_files", None)
    if self_files is not None:
        cards = self_files.select_impressions_for_batch(scope.key, speakers)
        if cards:
            parts.append("[对说话人的印象]\n" + "\n\n".join(cards))
        promises = self_files.relevant_promises(
            scope.key,
            query_text=render_prompt_text(inbound.body),
        )
        if promises:
            parts.append("[未兑现的承诺]\n" + "\n".join(item.format_line() for item in promises))
        stances = verified_stances(self_files, ledger, inbound.endpoint)
        if stances:
            prefix = quote_evidence(render_stances(stances), "past_stances", stable=True)
            tail = f"[本群过往看法]\n{prefix}\n\n{tail}"
    status = compute_status_line(
        ledger,
        scope,
        now=inbound.occurred_at or None,
        current_message_id=current,
        mentions_bot=inbound.mentions_bot,
    )
    if status:
        parts.append("[状态]\n" + status)
    return tail, render_memory(store.memory(scope.key)), "\n\n".join(parts), reply


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
