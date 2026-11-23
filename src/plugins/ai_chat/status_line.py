"""Ephemeral 10-minute mood/status line computed on the fly from the ledger.

Never stored separately: when the window passes, mood resets automatically.
Placed at the end of user turn context so the system prompt prefix stays cached.
"""

from __future__ import annotations

import re
import time

DEFAULT_WINDOW_SECONDS = 600
_PRAISE_RE = re.compile(
    r"(谢谢|感谢|多谢|牛[逼比b]?|厉害|真强|好强|靠谱|好用|赞一个|不错|真棒|太棒|神了|爱了|666|\bnb\b|\bnice\b|\bawesome\b|\bthanks\b|\bthx\b)",
    re.IGNORECASE,
)
_PUSHBACK_RE = re.compile(
    r"(闭嘴|别说了|少说两句|别吵|吵死|滚|傻[逼比b]?|笨死|智障|废物|胡说|瞎编|胡扯|满嘴跑火车|错了吧|你错了|菜鸡|好菜|别插嘴|退下|烦人|太吵)",
    re.IGNORECASE,
)
_NAME_RE = re.compile(r"(?:\bgaoji\b|高寄)", re.IGNORECASE)


def _explicit_mentions(ledger, message_ids: list[int]) -> set[int]:
    if not message_ids or not hasattr(ledger, "_connection"):
        return set()
    try:
        placeholders = ",".join("?" for _ in message_ids)
        rows = ledger._connection.execute(
            f"SELECT canonical_message_id FROM qq_official_inbound_events "
            f"WHERE canonical_message_id IN ({placeholders}) AND explicit_mention = TRUE",
            tuple(message_ids),
        ).fetchall()
        return {int(row["canonical_message_id"]) for row in rows if row["canonical_message_id"] is not None}
    except Exception:
        return set()


def compute_status_line(
    ledger,
    scope,
    *,
    now: int | None = None,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    current_message_id: int | None = None,
    mentions_bot: bool = False,
) -> str:
    """Compute a single-line status/mood summary from recent messages in the ledger."""
    if ledger is None:
        return ""
    current_now = int(time.time()) if now is None else int(now)
    cutoff = current_now - max(60, int(window_seconds))
    try:
        rows = ledger.history(scope, limit=120, newest=True)
    except Exception:
        return ""

    recent = sorted(
        (row for row in rows if int(row.occurred_at or 0) >= cutoff),
        key=lambda row: (int(row.occurred_at or 0), int(row.canonical_message_id)),
    )
    if not recent and not mentions_bot:
        return ""

    outbound_ids = {int(row.canonical_message_id) for row in rows if row.direction == "outbound"}
    inbound_rows = [row for row in recent if row.direction == "inbound"]
    mentioned = _explicit_mentions(ledger, [int(row.canonical_message_id) for row in inbound_rows])

    inbound_count = len(inbound_rows)
    mention_count = 0
    praise_count = 0
    pushback_count = 0
    last_bot_at: int | None = None
    saw_current = False

    for row in recent:
        if row.direction == "outbound":
            last_bot_at = int(row.occurred_at or 0)
            continue
        if row.direction != "inbound":
            continue
        cid = int(row.canonical_message_id)
        if current_message_id is not None and cid == current_message_id:
            saw_current = True
        text = str(row.prompt_text or row.rendered_text or "")
        to_bot = (
            cid in mentioned
            or (row.reply_to_canonical_message_id in outbound_ids)
            or bool(_NAME_RE.search(text))
            or (current_message_id is not None and cid == current_message_id and mentions_bot)
        )
        if to_bot:
            mention_count += 1
        near_bot = (row.reply_to_canonical_message_id is None and last_bot_at is not None
                    and 0 <= int(row.occurred_at or 0) - last_bot_at <= 180)
        if to_bot or near_bot:
            if _PUSHBACK_RE.search(text):
                pushback_count += 1
            elif _PRAISE_RE.search(text):
                praise_count += 1

    if mentions_bot and not saw_current:
        mention_count += 1
        inbound_count += 1

    if inbound_count == 0 and mention_count == 0:
        return ""

    if pushback_count > praise_count and pushback_count > 0:
        mood = f"刚被怼/叫闭嘴 {pushback_count} 次 → 说话更短更克制，别顶嘴添乱"
    elif praise_count > pushback_count and praise_count > 0:
        mood = f"刚被夸了 {praise_count} 次 → 嘴上“就这”，其实挺受用，照常帮忙"
    elif mention_count >= 5:
        mood = "连续被叫好几次，有点忙 → 直说重点，少废话"
    elif mention_count == 0 and inbound_count <= 2:
        mood = "刚冒头，群里挺安静 → 语气松弛自然"
    else:
        mood = "气氛正常 → 照常接话"

    return f"10分钟内群里 {inbound_count} 条发言，被 @/点名 {mention_count} 次（{mood}）。"
