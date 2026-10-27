"""Learning uses verified proactive sends, never general chat activity as approval."""

from __future__ import annotations

from contextlib import contextmanager
import random
import re
import threading
import time
import uuid

from src.bot_storage import DatabaseSource, open_store_connection

from .conversation_scope import ConversationScope


THRESHOLDS = (90, 94, 98)
PRIORS = {90: (1.0, 3.0), 94: (2.0, 3.0), 98: (3.0, 2.0)}
_REJECTION = re.compile(
    r"^(?:请|你|先|给我|麻烦你)*\s*(?:闭嘴|别插话|别说话|不要插话|不要说话|安静点|shut\s+up|stop\s+talking)[！!。.,，\s]*$",
    re.I,
)


def adaptive_for_group(settings, group_id: int) -> bool:
    return bool(
        settings.proactive_adaptive and group_id in settings.proactive_adaptive_groups
    )


def is_rejection(text: str) -> bool:
    return bool(_REJECTION.fullmatch(text.strip()))


class ProactiveLearningStore:
    def __init__(self, source: DatabaseSource, *, feedback_seconds: int = 300):
        _, self._connection = open_store_connection(source)
        self._lock = threading.RLock()
        self.feedback_seconds = min(max(int(feedback_seconds), 60), 3600)

    def close(self):
        with self._lock:
            self._connection.close()

    @contextmanager
    def _transaction(self):
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute("BEGIN")
                yield cursor
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
            finally:
                cursor.close()

    @staticmethod
    def _scope(scope: ConversationScope, bot_id: str):
        if (
            scope.platform != "onebot-v11"
            or scope.kind != "group"
            or not scope.native_conversation_id.isdigit()
            or int(scope.native_conversation_id) <= 0
            or not str(bot_id).isdigit()
            or int(bot_id) <= 0
            or (scope.bot_native_user_id and scope.bot_native_user_id != str(bot_id))
        ):
            raise ValueError("Proactive learning requires an explicit QQ group and bot")

    def arm_stats(self, scope: ConversationScope, bot_id: str) -> list[dict]:
        self._scope(scope, bot_id)
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM proactive_arms WHERE scope_key=? AND bot_id=?",
                (scope.key, str(bot_id)),
            ).fetchall()
        learned = {int(row["threshold"]): row for row in rows}
        return [
            dict(
                threshold=threshold,
                alpha=PRIORS[threshold][0]
                + float(learned[threshold]["successes"] if threshold in learned else 0),
                beta=PRIORS[threshold][1]
                + float(learned[threshold]["failures"] if threshold in learned else 0),
                observations=int(
                    learned[threshold]["observations"] if threshold in learned else 0
                ),
            )
            for threshold in THRESHOLDS
        ]

    def choose_threshold(
        self, scope: ConversationScope, bot_id: str, *, sample=random.betavariate
    ) -> int:
        return max(
            self.arm_stats(scope, bot_id),
            key=lambda arm: (sample(arm["alpha"], arm["beta"]), arm["threshold"]),
        )["threshold"]

    def begin(
        self,
        scope: ConversationScope,
        bot_id: str,
        trigger_message_id: str,
        threshold: int,
        interest: int,
        *,
        now: int | None = None,
    ) -> str | None:
        self._scope(scope, bot_id)
        timestamp = int(time.time()) if now is None else now
        if (
            threshold not in THRESHOLDS
            or not threshold <= interest <= 100
            or not trigger_message_id
        ):
            raise ValueError("Invalid proactive experiment")
        identifier = uuid.uuid4().hex
        with self._transaction() as cursor:
            inserted = cursor.execute(
                """INSERT INTO proactive_trials
                (trial_id, scope_key, bot_id, trigger_message_id, threshold, interest, status, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                ON CONFLICT (scope_key, bot_id, trigger_message_id) DO NOTHING""",
                (
                    identifier,
                    scope.key,
                    str(bot_id),
                    str(trigger_message_id),
                    threshold,
                    interest,
                    timestamp,
                    timestamp + 600,
                ),
            )
            return identifier if inserted.rowcount == 1 else None

    def sent(
        self, trial_id: str, native_message_id: str, *, now: int | None = None
    ) -> None:
        timestamp = int(time.time()) if now is None else now
        if not native_message_id:
            self.unsent(trial_id, "unknown")
            return
        with self._transaction() as cursor:
            cursor.execute(
                """UPDATE proactive_trials SET status='sent', native_message_id=?, sent_at=?, expires_at=?
                WHERE trial_id=? AND status='pending'""",
                (
                    str(native_message_id),
                    timestamp,
                    timestamp + self.feedback_seconds,
                    trial_id,
                ),
            )

    def unsent(self, trial_id: str, status: str) -> None:
        if status not in {"unknown", "failed"}:
            raise ValueError("Invalid proactive send outcome")
        with self._transaction() as cursor:
            cursor.execute(
                "UPDATE proactive_trials SET status=? WHERE trial_id=? AND status='pending'",
                (status, trial_id),
            )

    def feedback(
        self,
        scope: ConversationScope,
        bot_id: str,
        *,
        native_message_id: str | None,
        actor_id: str,
        kind: str,
        value: int,
        source_id: str,
        occurred_at: int,
        now: int | None = None,
    ) -> bool:
        self._scope(scope, bot_id)
        timestamp = int(time.time()) if now is None else now
        if (
            kind not in {"reaction", "follow_up", "rejected", "recalled"}
            or value not in {-1, 0, 1}
            or not str(actor_id).isdigit()
            or int(actor_id) <= 0
            or str(actor_id) == str(bot_id)
            or occurred_at <= 0
            or occurred_at > timestamp + 300
            or not source_id
        ):
            return False
        # Only explicitly addressed follow-ups may resolve to the latest proactive reply.
        if native_message_id is None and kind not in {"follow_up", "rejected"}:
            return False
        target = "AND native_message_id=?" if native_message_id is not None else ""
        with self._transaction() as cursor:
            trial = cursor.execute(
                f"""SELECT * FROM proactive_trials
                WHERE scope_key=? AND bot_id=? AND status='sent' AND sent_at<=? AND expires_at>=? AND expires_at>=?
                {target} ORDER BY sent_at DESC, created_at DESC, trial_id DESC LIMIT 1 FOR UPDATE""",
                (
                    scope.key,
                    str(bot_id),
                    occurred_at,
                    occurred_at,
                    timestamp,
                    *(
                        (str(native_message_id),)
                        if native_message_id is not None
                        else ()
                    ),
                ),
            ).fetchone()
            if trial is None:
                return False
            previous = cursor.execute(
                "SELECT * FROM proactive_feedback WHERE trial_id=? AND actor_id=? AND kind=?",
                (trial["trial_id"], str(actor_id), kind),
            ).fetchone()
            if previous is not None:
                if occurred_at < int(previous["occurred_at"]):
                    return False
                if value == 0 and source_id != previous["source_id"]:
                    return False
                if (
                    occurred_at == int(previous["occurred_at"])
                    and source_id == previous["source_id"]
                ):
                    if int(previous["value"]) in {value, 0}:
                        return False
            cursor.execute(
                """INSERT INTO proactive_feedback
                (trial_id, actor_id, kind, value, source_id, occurred_at) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(trial_id, actor_id, kind) DO UPDATE SET value=excluded.value,
                    source_id=excluded.source_id, occurred_at=excluded.occurred_at""",
                (trial["trial_id"], str(actor_id), kind, value, source_id, occurred_at),
            )
            return True

    def settle_due(self, *, now: int | None = None, limit: int = 100) -> int:
        timestamp = int(time.time()) if now is None else now
        with self._transaction() as cursor:
            cursor.execute(
                "UPDATE proactive_trials SET status='unknown' WHERE status='pending' AND expires_at<?",
                (timestamp,),
            )
            trials = cursor.execute(
                """SELECT * FROM proactive_trials WHERE status='sent' AND expires_at<?
                ORDER BY expires_at, trial_id LIMIT ? FOR UPDATE SKIP LOCKED""",
                (timestamp, min(max(limit, 1), 100)),
            ).fetchall()
            for trial in trials:
                signals = cursor.execute(
                    "SELECT kind, value FROM proactive_feedback WHERE trial_id=?",
                    (trial["trial_id"],),
                ).fetchall()
                if any(row["value"] < 0 for row in signals):
                    reward, weight, outcome = 0.0, 1.0, "negative"
                elif any(
                    row["kind"] == "reaction" and row["value"] > 0 for row in signals
                ):
                    reward, weight, outcome = 1.0, 1.0, "positive_reaction"
                elif any(row["value"] > 0 for row in signals):
                    reward, weight, outcome = 1.0, 0.5, "addressed_follow_up"
                else:
                    reward, weight, outcome = 0.5, 0.1, "no_feedback"
                cursor.execute(
                    """INSERT INTO proactive_arms (scope_key, bot_id, threshold, successes, failures, observations)
                    VALUES (?, ?, ?, ?, ?, 1) ON CONFLICT(scope_key, bot_id, threshold) DO UPDATE SET
                        successes=proactive_arms.successes+excluded.successes,
                        failures=proactive_arms.failures+excluded.failures,
                        observations=proactive_arms.observations+1""",
                    (
                        trial["scope_key"],
                        trial["bot_id"],
                        trial["threshold"],
                        reward * weight,
                        (1 - reward) * weight,
                    ),
                )
                cursor.execute(
                    """UPDATE proactive_trials SET status='settled', reward=?, weight=?, outcome=?, settled_at=?
                    WHERE trial_id=?""",
                    (reward, weight, outcome, timestamp, trial["trial_id"]),
                )
            return len(trials)

    def observe_notice(
        self,
        payload: dict,
        *,
        bot_id: str,
        positive_ids,
        negative_ids,
        now: int | None = None,
    ) -> bool:
        timestamp = int(time.time()) if now is None else now
        try:
            if str(payload["self_id"]) != str(bot_id) or int(payload["group_id"]) <= 0:
                return False
            scope = ConversationScope(
                "onebot-v11", "group", str(int(payload["group_id"]))
            )
            message_id = str(int(payload["message_id"]))
            occurred_at = int(payload["time"])
            if payload.get("notice_type") == "group_recall":
                if str(payload["user_id"]) != str(bot_id):
                    return False
                return self.feedback(
                    scope,
                    bot_id,
                    native_message_id=message_id,
                    actor_id=str(payload["operator_id"]),
                    kind="recalled",
                    value=-1,
                    source_id=message_id,
                    occurred_at=occurred_at,
                    now=timestamp,
                )
            if (
                payload.get("notice_type") != "group_msg_emoji_like"
                or type(payload.get("is_add")) is not bool
            ):
                return False
            positive, negative = set(positive_ids), set(negative_ids)
            likes = payload.get("likes")
            if positive & negative or not isinstance(likes, list) or len(likes) > 50:
                return False
            selected = {
                str(item.get("emoji_id")) for item in likes if isinstance(item, dict)
            } & (positive | negative)
            if len(selected) != 1:
                return False
            emoji = selected.pop()
            if not re.fullmatch(r"[0-9]{1,12}", emoji):
                return False
            vote = (1 if emoji in positive else -1) if payload["is_add"] else 0
            return self.feedback(
                scope,
                bot_id,
                native_message_id=message_id,
                actor_id=str(payload["user_id"]),
                kind="reaction",
                value=vote,
                source_id=emoji,
                occurred_at=occurred_at,
                now=timestamp,
            )
        except (KeyError, TypeError, ValueError):
            return False
