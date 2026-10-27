"""Community feedback is evidence, not an administrative judgement."""
from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping, Sequence

from .conversation_scope import ConversationScope


class ReactionVoteStoreMixin:
    def set_reaction_vote(self, scope: ConversationScope, turn_id: int, *,
                          reactor_principal_id: int, message_id: int,
                          emoji_id: str, vote: int, is_add: bool,
                          source_time: int, now: int | None = None) -> bool:
        if (scope.kind != "group" or vote not in {-1, 1} or type(is_add) is not bool
                or not re.fullmatch(r"[0-9]{1,12}", emoji_id)
                or reactor_principal_id <= 0 or source_time <= 0):
            raise ValueError("Invalid reaction vote")
        timestamp = int(time.time()) if now is None else now
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtext(?))",
                (f"reaction:{turn_id}:{reactor_principal_id}",),
            )
            # Recheck visibility and the actual outgoing link in the write transaction.
            row = cursor.execute("""SELECT trigger.sender_principal_id AS asker
                FROM agent_turns AS t
                JOIN turn_visibility AS v ON v.scope_key = t.scope_key
                JOIN turn_send_links AS link ON link.turn_id = t.turn_id
                JOIN messages AS sent ON sent.canonical_message_id = link.canonical_message_id
                JOIN conversations AS c ON c.conversation_id = sent.conversation_id
                JOIN conversation_visibility AS cv ON cv.conversation_id = c.conversation_id
                JOIN messages AS trigger ON trigger.canonical_message_id = t.trigger_canonical_message_id
                JOIN conversations AS tc ON tc.conversation_id = trigger.conversation_id
                WHERE t.turn_id = ? AND t.scope_key = ? AND c.scope_key = ? AND tc.scope_key = ?
                  AND link.canonical_message_id = ? AND t.turn_ordinal >= v.min_turn_ordinal
                  AND sent.canonical_message_id >= cv.min_canonical_message_id
                  AND trigger.canonical_message_id >= cv.min_canonical_message_id""",
                (turn_id, scope.key, scope.key, scope.key, message_id)).fetchone()
            if row is None:
                return False
            previous = cursor.execute("""SELECT * FROM turn_reaction_votes
                WHERE turn_id = ? AND reactor_principal_id = ?""", (turn_id, reactor_principal_id)).fetchone()
            value = vote if is_add else 0
            if previous is not None:
                if source_time < int(previous["source_time"]):
                    return False
                same_source = previous["emoji_id"] == emoji_id and int(previous["message_id"]) == message_id
                if not is_add and not same_source:
                    return False
                if source_time == int(previous["source_time"]) and same_source:
                    if int(previous["vote"]) == value or (int(previous["vote"]) == 0 and is_add):
                        return False
            # A zero tombstone prevents late duplicate additions from resurrecting a removed vote.
            cursor.execute("""INSERT INTO turn_reaction_votes
                (turn_id, reactor_principal_id, message_id, is_asker, vote, emoji_id,
                 source_time, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(turn_id, reactor_principal_id) DO UPDATE SET
                    message_id = excluded.message_id, is_asker = excluded.is_asker,
                    vote = excluded.vote, emoji_id = excluded.emoji_id,
                    source_time = excluded.source_time, updated_at = excluded.updated_at""",
                (turn_id, reactor_principal_id, message_id, int(row["asker"] == reactor_principal_id),
                 value, emoji_id, source_time, timestamp, timestamp))
        return True

    def reaction_totals(self, turn_ids: Sequence[int]) -> dict[int, dict]:
        if not turn_ids:
            return {}
        placeholders = ",".join("?" for _ in turn_ids)
        with self._lock:
            rows = self._connection.execute(f"""SELECT r.turn_id,
                MAX(CASE WHEN r.is_asker = 1 AND r.vote <> 0 THEN r.vote END) AS asker,
                SUM(CASE WHEN r.is_asker = 0 AND r.vote = 1 THEN 1 ELSE 0 END) AS others_up,
                SUM(CASE WHEN r.is_asker = 0 AND r.vote = -1 THEN 1 ELSE 0 END) AS others_down
                FROM turn_reaction_votes AS r JOIN agent_turns AS t ON t.turn_id = r.turn_id
                JOIN turn_visibility AS v ON v.scope_key = t.scope_key
                WHERE r.turn_id IN ({placeholders}) AND t.turn_ordinal >= v.min_turn_ordinal
                GROUP BY r.turn_id""", tuple(turn_ids)).fetchall()
        return {int(row["turn_id"]): {"asker": row["asker"],
            "others_up": int(row["others_up"]), "others_down": int(row["others_down"])} for row in rows}


def record_reaction_feedback(payload: Mapping, *, bot_id: int, ledger, journal,
                             group_enabled: Callable[[int], bool], positive_ids: Sequence[str],
                             negative_ids: Sequence[str], now: int | None = None) -> int:
    if ledger is None or journal is None or payload.get("notice_type") != "group_msg_emoji_like":
        return 0
    try:
        group_id, user_id = int(payload["group_id"]), int(payload["user_id"])
        self_id, occurred_at = int(payload["self_id"]), int(payload["time"])
        message_id = str(int(payload["message_id"]))
    except (KeyError, TypeError, ValueError):
        return 0
    if (group_id <= 0 or user_id <= 0 or user_id == bot_id or self_id != bot_id
            or type(payload.get("is_add")) is not bool or occurred_at <= 0
            or occurred_at > (int(time.time()) if now is None else now) + 300
            or not group_enabled(group_id)):
        return 0
    positive, negative = set(positive_ids), set(negative_ids)
    if positive & negative:
        return 0
    likes = payload.get("likes")
    if not isinstance(likes, list) or len(likes) > 50:
        return 0
    selected = {str(item.get("emoji_id")) for item in likes if isinstance(item, dict)} & (positive | negative)
    if len(selected) != 1:
        return 0
    emoji_id = selected.pop()
    if not re.fullmatch(r"[0-9]{1,12}", emoji_id):
        return 0
    scope = ConversationScope("onebot-v11", "group", str(group_id))
    canonical_id = ledger.canonical_id_for_native(scope, message_id)
    sent = ledger.get_in_scope(scope, canonical_id) if canonical_id is not None else None
    if sent is None or str(sent.sender_native_user_id) != str(bot_id):
        return 0
    turn = journal.find_turn_for_reply(scope, canonical_id)
    if turn is None:
        return 0
    principal = ledger.principal_id_for_native("onebot-v11", str(user_id))
    if principal is None:
        principal = ledger.ensure_principal_identity("onebot-v11", str(user_id), "", seen_at=occurred_at)
    return int(journal.set_reaction_vote(scope, turn.turn_id, reactor_principal_id=principal,
        message_id=canonical_id, emoji_id=emoji_id, vote=1 if emoji_id in positive else -1,
        is_add=payload["is_add"], source_time=occurred_at, now=now))
