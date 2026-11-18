"""Group member roster and [mention#id] -> MentionNode resolution (Max-style @mention)."""
from __future__ import annotations

import re

from src.plugins.ai_chat.message_ir import MentionNode, MessageBody, MessageNode, TextNode
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey

MAX_AUTHORITY_ACTORS = 256
MAX_MENTIONS_PER_MESSAGE = 10
_MENTION_TOKEN = re.compile(r"\[mention#([1-9][0-9]{0,17})(?:[:：][^\n\]]{0,80})?\]")
_FENCE_BLOCK = re.compile(r"(```[\s\S]*?```|~~~[\s\S]*?~~~)")


def endpoint_mention_roster(cursor, endpoint: EndpointKey) -> dict[int, tuple[ActorKey, str]]:
    """Return {principal_id: (ActorKey, display_name)} for members seen in this group endpoint."""
    if not isinstance(endpoint, EndpointKey) or endpoint.kind != "group":
        return {}
    rows = cursor.execute(
        """
        SELECT m.sender_principal_id, m.sender_actor_key, m.sender_display
        FROM messages m
        JOIN conversation_visibility v USING (conversation_id)
        WHERE m.source_endpoint_key = ?
          AND m.canonical_message_id >= v.min_canonical_message_id
          AND m.direction = 'inbound'
          AND m.sender_principal_id IS NOT NULL
          AND m.sender_actor_key <> ''
        ORDER BY m.canonical_message_id DESC
        LIMIT 1000
        """,
        (endpoint.key,),
    ).fetchall()
    roster: dict[int, tuple[ActorKey, str]] = {}
    for row in rows:
        pid = int(row["sender_principal_id"])
        if pid in roster:
            continue
        try:
            actor = ActorKey.parse(str(row["sender_actor_key"]))
        except Exception:
            continue
        if actor.identity_kind != "group_member" or not actor.matches_endpoint(endpoint):
            continue
        display = str(row["sender_display"] or f"群友{pid}").replace("\n", " ").replace("\r", " ").strip() or f"群友{pid}"
        roster[pid] = (actor, display)
        if len(roster) >= MAX_AUTHORITY_ACTORS:
            break
    return roster


def endpoint_mention_authority(cursor, endpoint: EndpointKey) -> frozenset[ActorKey]:
    return frozenset(actor for actor, _ in endpoint_mention_roster(cursor, endpoint).values())


def parse_mention_body(
    cursor,
    endpoint: EndpointKey,
    text: str,
    *,
    max_mentions: int = MAX_MENTIONS_PER_MESSAGE,
) -> MessageBody:
    """Convert [mention#id] tokens outside fenced code blocks into MentionNode items."""
    if not text or "[mention#" not in text or endpoint.kind != "group":
        return MessageBody((TextNode(0, text),))
    roster = endpoint_mention_roster(cursor, endpoint)
    if not roster:
        return MessageBody((TextNode(0, text),))

    raw_pieces: list[tuple[str, object]] = []
    used = 0

    def append_text(chunk: str) -> None:
        if not chunk:
            return
        if raw_pieces and raw_pieces[-1][0] == "text":
            raw_pieces[-1] = ("text", str(raw_pieces[-1][1]) + chunk)
        else:
            raw_pieces.append(("text", chunk))

    for idx, segment in enumerate(_FENCE_BLOCK.split(text)):
        if idx % 2 == 1:
            append_text(segment)
            continue
        pos = 0
        for match in _MENTION_TOKEN.finditer(segment):
            append_text(segment[pos:match.start()])
            pos = match.end()
            pid = int(match.group(1))
            entry = roster.get(pid)
            if entry is None:
                append_text(match.group(0))
                continue
            actor, display = entry
            if used < max_mentions:
                used += 1
                raw_pieces.append(("mention", (actor, display, pid)))
            else:
                append_text(f"@{display}")
        append_text(segment[pos:])

    if not raw_pieces:
        return MessageBody((TextNode(0, ""),))
    nodes: list[MessageNode] = []
    for seg_index, (kind, payload) in enumerate(raw_pieces):
        if kind == "text":
            nodes.append(TextNode(seg_index, str(payload)))
        else:
            actor, display, pid = payload  # type: ignore[misc]
            nodes.append(MentionNode(seg_index, actor.native_user_id, display, principal_id=pid))
    return MessageBody(tuple(nodes))
