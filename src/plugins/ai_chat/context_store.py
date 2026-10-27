from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator

from src.bot_storage import DatabaseSource, open_store_connection, StoreCursor, StoreRow

from .conversation_scope import ConversationScope
from .ledger import CanonicalMessage, MessageLedger
from .message_ir import body_to_json
from .prompt_context import HISTORY_BLOCK_LINES


@dataclass(frozen=True)
class CompartmentRecord:
    compartment_id: int
    scope_key: str
    ordinal: int
    expand_handle: str
    start_message_id: int
    end_message_id: int
    source_message_ids: tuple[int, ...]
    source_hash: str
    message_count: int
    token_estimate: int
    summary_p1: str
    summary_p2: str
    summary_p3: str
    created_at: int
    summary_p4: str = ""
    topic: str = ""
    importance: float = 0.5
    confidence: float = 0.5
    participants: tuple[str, ...] = ()
    evidence_ids: tuple[int, ...] = ()
    generation_mode: str = "legacy"


@dataclass(frozen=True)
class ContextProjection:
    text: str
    token_estimate: int
    compartment_handles: tuple[str, ...]
    raw_message_ids: tuple[int, ...]
    materialized_count: int = 0
    degraded: bool = False


@dataclass(frozen=True)
class CaptureCandidate:
    scope_key: str
    expected_cursor: int
    messages: tuple[CanonicalMessage, ...]
    source_hash: str


class ContextStore:
    """Rebuildable chronological projections over the canonical ledger."""

    def __init__(
        self,
        path: DatabaseSource,
        *,
        input_budget_tokens: int = 32768,
        high_watermark_tokens: int = 32768,
        low_watermark_tokens: int = 16384,
        compartment_target_tokens: int = 1200,
        raw_tail_min_messages: int = 8,
        max_compartments: int = 12,
        historian_managed: bool = False,
    ) -> None:
        self.path, self._connection = open_store_connection(path)
        self.input_budget_tokens = max(int(input_budget_tokens), 1000)
        self.high_watermark_tokens = min(
            max(int(high_watermark_tokens), 500),
            self.input_budget_tokens,
        )
        self.low_watermark_tokens = min(
            max(int(low_watermark_tokens), 250),
            self.high_watermark_tokens,
        )
        self.compartment_target_tokens = max(
            int(compartment_target_tokens),
            250,
        )
        self.raw_tail_min_messages = max(int(raw_tail_min_messages), 1)
        self.max_compartments = min(max(int(max_compartments), 1), 50)
        self.historian_managed = bool(historian_managed)
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def build_projection(
        self,
        ledger: MessageLedger,
        scope: ConversationScope,
        *,
        exclude_native_message_id: str | int | None = None,
        protected_message_ids: tuple[int, ...] = (),
        exclude_canonical_message_ids: tuple[int, ...] = (),
        materialize: bool = True,
        token_budget: int | None = None,
    ) -> ContextProjection:
        projection_budget = min(
            max(int(token_budget or self.input_budget_tokens), 1000),
            self.input_budget_tokens,
        )
        floor = ledger.visible_message_floor(scope)
        self._sync_visibility(scope.key, floor)
        materialized = (
            self._materialize_backlog(
                ledger,
                scope,
                set(int(item) for item in protected_message_ids),
            )
            if materialize and not self.historian_managed
            else 0
        )
        cursor = self._cursor(scope.key, floor)
        raw_messages = ledger.visible_messages_after(
            scope,
            cursor,
            limit=5000,
        )
        excluded_canonical_ids = set(
            int(item) for item in exclude_canonical_message_ids
        )
        if excluded_canonical_ids:
            raw_messages = [
                message
                for message in raw_messages
                if message.canonical_message_id not in excluded_canonical_ids
            ]
        if exclude_native_message_id is not None:
            excluded = str(exclude_native_message_id)
            raw_messages = [
                message
                for message in raw_messages
                if message.native_message_id != excluded
            ]

        compartments = self._active_compartments(scope.key)
        raw_lines = [self._message_line(message) for message in raw_messages]
        raw_text, raw_ids, raw_tokens, degraded = self._fit_raw_tail(
            raw_messages,
            raw_lines,
            token_budget=projection_budget,
        )
        remaining = max(projection_budget - raw_tokens, 0)
        chosen: list[tuple[CompartmentRecord, str]] = []
        for age, compartment in enumerate(reversed(compartments)):
            if len(chosen) >= self.max_compartments:
                break
            if excluded_canonical_ids.intersection(
                compartment.source_message_ids
            ):
                continue
            summary = (
                compartment.summary_p1
                if age < 2
                else compartment.summary_p2
                if age < 6
                else compartment.summary_p3
            )
            block = self._compartment_block(compartment, summary)
            cost = estimate_tokens(block)
            if cost > remaining:
                continue
            chosen.append((compartment, block))
            remaining -= cost
        chosen.reverse()

        parts = []
        if chosen:
            parts.append(
                "[conversation compartments - chronological projections; "
                "details require context_expand]\n"
                + "\n".join(block for _item, block in chosen)
            )
        if raw_text:
            parts.append("[protected live tail]\n" + raw_text)
        text = "\n\n".join(parts)
        return ContextProjection(
            text=text,
            token_estimate=estimate_tokens(text),
            compartment_handles=tuple(
                item.expand_handle for item, _block in chosen
            ),
            raw_message_ids=tuple(raw_ids),
            materialized_count=materialized,
            degraded=degraded,
        )

    def expand(
        self,
        ledger: MessageLedger,
        scope: ConversationScope,
        expand_handle: str,
        *,
        max_chars: int = 12000,
    ) -> str | None:
        floor = ledger.visible_message_floor(scope)
        self._sync_visibility(scope.key, floor)
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM context_compartments
                WHERE scope_key = ? AND expand_handle = ? AND active = 1
                  AND end_message_id >= ?
                """,
                (scope.key, expand_handle, floor),
            ).fetchone()
        if row is None:
            return None
        compartment = self._row_to_compartment(row)
        messages = ledger.visible_messages_by_ids(
            scope,
            compartment.source_message_ids,
        )
        if (
            tuple(message.canonical_message_id for message in messages)
            != compartment.source_message_ids
            or self._source_hash(messages) != compartment.source_hash
        ):
            return None
        lines = [
            f"[episode#{compartment.expand_handle} exact evidence]",
            f"范围: msg#{compartment.start_message_id}..msg#{compartment.end_message_id}; "
            f"消息: {compartment.message_count}; source_hash: {compartment.source_hash[:12]}",
            compartment.summary_p1,
            "[source transcript]",
            *(self._message_line(message) for message in messages),
        ]
        return "\n".join(lines)[: max(int(max_chars), 1000)]

    def search(
        self,
        scope: ConversationScope,
        query: str,
        *,
        limit: int = 5,
    ) -> list[CompartmentRecord]:
        query = " ".join(query.split()).casefold()
        if not query:
            return []
        limit = min(max(int(limit), 1), 20)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM context_compartments
                WHERE scope_key = ? AND active = 1
                ORDER BY ordinal DESC
                LIMIT 500
                """,
                (scope.key,),
            ).fetchall()
        matches = [
            self._row_to_compartment(row)
            for row in rows
            if query
            in " ".join(
                (
                    str(row["summary_p1"]),
                    str(row["summary_p2"]),
                    str(row["summary_p3"]),
                    str(_row_value(row, "summary_p4", "")),
                    str(_row_value(row, "topic", "")),
                )
            ).casefold()
        ]
        return matches[:limit]

    def active_compartments(self, *, limit: int = 5000) -> list[CompartmentRecord]:
        bounded = min(max(int(limit), 1), 20000)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM context_compartments
                WHERE active = 1
                ORDER BY compartment_id DESC LIMIT ?
                """,
                (bounded,),
            ).fetchall()
        return [self._row_to_compartment(row) for row in reversed(rows)]

    def compartment_ids_after(
        self, ledger: MessageLedger, scope: ConversationScope, *,
        after_id: int = 0, limit: int = 100,
    ) -> tuple[int, ...]:
        """Page chapter references, including invalid evidence so callers can advance."""
        floor = ledger.visible_message_floor(scope)
        self._sync_visibility(scope.key, floor)
        with self._lock:
            rows = self._connection.execute(
                "SELECT compartment_id FROM context_compartments WHERE scope_key=? "
                "AND active=1 AND start_message_id>=? AND compartment_id>? "
                "ORDER BY compartment_id LIMIT ?",
                (scope.key, floor, max(0, int(after_id)), min(max(int(limit), 1), 100)),
            ).fetchall()
        return tuple(int(row["compartment_id"]) for row in rows)

    def visible_compartments(
        self, ledger: MessageLedger, scope: ConversationScope, *, limit: int = 32,
        compartment_ids: tuple[int, ...] | None = None,
    ) -> list[CompartmentRecord]:
        """Return only scoped chapters whose original evidence remains intact."""
        floor = ledger.visible_message_floor(scope)
        self._sync_visibility(scope.key, floor)
        bounded = min(max(int(limit), 1), 100)
        conditions = ""
        values: list[object] = [scope.key, floor]
        if compartment_ids is not None:
            ids = tuple(dict.fromkeys(int(item) for item in compartment_ids))
            if not ids:
                return []
            if len(ids) > 100:
                raise ValueError("At most 100 compartment references can be checked")
            conditions = " AND compartment_id IN (" + ",".join("?" for _ in ids) + ")"
            values.extend(ids)
        values.append(bounded)
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM context_compartments WHERE scope_key=? AND active=1 "
                "AND start_message_id>=?" + conditions + " ORDER BY ordinal DESC LIMIT ?",
                tuple(values),
            ).fetchall()
        result = []
        for row in reversed(rows):
            chapter = self._row_to_compartment(row)
            messages = ledger.visible_messages_by_ids(scope, chapter.source_message_ids)
            if (tuple(item.canonical_message_id for item in messages) == chapter.source_message_ids
                    and self._source_hash(messages) == chapter.source_hash):
                result.append(chapter)
        return result

    def capture_candidate(
        self,
        ledger: MessageLedger,
        scope: ConversationScope,
        *,
        protected_message_ids: tuple[int, ...] = (),
        settled: bool = False,
    ) -> CaptureCandidate | None:
        floor = ledger.visible_message_floor(scope)
        self._sync_visibility(scope.key, floor)
        cursor = self._cursor(scope.key, floor)
        messages = ledger.visible_messages_after(scope, cursor, limit=5000)
        if not messages:
            return None
        total_tokens = sum(
            estimate_tokens(self._message_line(message))
            for message in messages
        )
        if not settled and total_tokens <= self.high_watermark_tokens:
            return None
        maximum = (
            len(messages)
            if settled
            else len(messages) - self.raw_tail_min_messages
        )
        protected = {int(item) for item in protected_message_ids}
        first_protected = next(
            (
                index
                for index, message in enumerate(messages)
                if message.canonical_message_id in protected
            ),
            maximum,
        )
        maximum = min(maximum, first_protected)
        if maximum <= 0:
            return None
        prefix: list[CanonicalMessage] = []
        used_tokens = 0
        for message in messages[:maximum]:
            cost = estimate_tokens(self._message_line(message))
            if prefix and used_tokens + cost > self.compartment_target_tokens:
                break
            prefix.append(message)
            used_tokens += cost
        if not prefix:
            return None
        return CaptureCandidate(
            scope_key=scope.key,
            expected_cursor=cursor,
            messages=tuple(prefix),
            source_hash=self._source_hash(prefix),
        )

    def publish_generated(
        self,
        candidate: CaptureCandidate,
        summaries: tuple[str, str, str],
        *,
        summary_p4: str = "",
        topic: str = "",
        importance: float = 0.5,
        confidence: float = 0.5,
        participants: tuple[str, ...] = (),
        evidence_ids: tuple[int, ...] = (),
        generation_mode: str = "historian",
    ) -> CompartmentRecord:
        normalized = tuple(" ".join(item.split()).strip() for item in summaries)
        if len(normalized) != 3 or any(not item for item in normalized):
            raise ValueError("historian summaries must contain non-empty P1/P2/P3")
        if self._source_hash(list(candidate.messages)) != candidate.source_hash:
            raise RuntimeError("historian source changed before publication")
        compartment_id = self._publish(
            candidate.scope_key,
            candidate.expected_cursor,
            list(candidate.messages),
            summaries=(
                normalized[0][:4000],
                normalized[1][:2000],
                normalized[2][:500],
            ),
            summary_p4=" ".join(summary_p4.split())[:300],
            topic=" ".join(topic.split())[:300],
            importance=_bounded_score(importance),
            confidence=_bounded_score(confidence),
            participants=tuple(
                dict.fromkeys(
                    " ".join(item.split())[:120]
                    for item in participants
                    if item.strip()
                )
            )[:32],
            evidence_ids=tuple(dict.fromkeys(int(item) for item in evidence_ids)),
            generation_mode=(
                generation_mode
                if generation_mode in {"historian", "fallback", "legacy"}
                else "historian"
            ),
        )
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM context_compartments WHERE compartment_id = ?",
                (compartment_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("historian compartment publication disappeared")
        return self._row_to_compartment(row)

    def restore_capture_candidate(
        self,
        ledger: MessageLedger,
        scope: ConversationScope,
        *,
        expected_cursor: int,
        source_message_ids: tuple[int, ...],
        source_hash: str,
    ) -> CaptureCandidate:
        floor = ledger.visible_message_floor(scope)
        self._sync_visibility(scope.key, floor)
        if self._cursor(scope.key, floor) != int(expected_cursor):
            raise RuntimeError("historian capture cursor is stale")
        messages = ledger.visible_messages_by_ids(scope, source_message_ids)
        actual_ids = tuple(item.canonical_message_id for item in messages)
        if actual_ids != tuple(int(item) for item in source_message_ids):
            raise RuntimeError("historian capture evidence is no longer visible")
        actual_hash = self._source_hash(messages)
        if actual_hash != str(source_hash):
            raise RuntimeError("historian capture source hash changed")
        return CaptureCandidate(
            scope_key=scope.key,
            expected_cursor=int(expected_cursor),
            messages=tuple(messages),
            source_hash=actual_hash,
        )

    def hide_history(self, scope: ConversationScope, ledger_floor: int) -> int:
        with self._transaction() as cursor:
            rows = cursor.execute(
                """
                SELECT COUNT(*) FROM context_compartments
                WHERE scope_key = ? AND active = 1
                """,
                (scope.key,),
            ).fetchone()
            count = int(rows[0]) if rows is not None else 0
            cursor.execute(
                """
                UPDATE context_compartments SET active = 0
                WHERE scope_key = ? AND active = 1
                """,
                (scope.key,),
            )
            self._ensure_state(cursor, scope.key, ledger_floor)
            cursor.execute(
                """
                UPDATE context_state
                SET visibility_floor = ?, last_compacted_message_id = ?,
                    updated_at = ?
                WHERE scope_key = ?
                """,
                (
                    int(ledger_floor),
                    max(int(ledger_floor) - 1, 0),
                    int(time.time()),
                    scope.key,
                ),
            )
            return count

    def verify_scope(
        self,
        ledger: MessageLedger,
        scope: ConversationScope,
    ) -> tuple[bool, str]:
        floor = ledger.visible_message_floor(scope)
        self._sync_visibility(scope.key, floor)
        previous_end = floor - 1
        for compartment in self._active_compartments(scope.key):
            messages = ledger.visible_messages_by_ids(
                scope,
                compartment.source_message_ids,
            )
            if not messages:
                return False, f"{compartment.expand_handle}: source missing"
            first = messages[0].canonical_message_id
            between = ledger.visible_messages_after(
                scope,
                previous_end,
                limit=len(messages),
            )
            if not between or between[0].canonical_message_id != first:
                return False, f"{compartment.expand_handle}: coverage gap"
            if tuple(item.canonical_message_id for item in between) != (
                compartment.source_message_ids
            ):
                return False, f"{compartment.expand_handle}: range mismatch"
            if self._source_hash(messages) != compartment.source_hash:
                return False, f"{compartment.expand_handle}: hash mismatch"
            previous_end = compartment.end_message_id
        if self._cursor(scope.key, floor) != previous_end:
            return False, "coverage cursor does not match published history"
        return True, "ok"

    def _materialize_backlog(
        self,
        ledger: MessageLedger,
        scope: ConversationScope,
        protected_message_ids: set[int],
    ) -> int:
        materialized = 0
        floor = ledger.visible_message_floor(scope)
        for _iteration in range(100):
            cursor = self._cursor(scope.key, floor)
            total_count = ledger.visible_message_count_after(scope, cursor)
            messages = ledger.visible_messages_after(scope, cursor, limit=500)
            if not messages:
                break
            total_tokens = sum(
                estimate_tokens(self._message_line(message))
                for message in messages
            )
            if total_count > len(messages):
                maximum = len(messages)
            elif total_tokens > self.high_watermark_tokens:
                maximum = max(
                    len(messages) - self.raw_tail_min_messages,
                    0,
                )
            else:
                break
            first_protected = next(
                (
                    index
                    for index, message in enumerate(messages)
                    if message.canonical_message_id in protected_message_ids
                ),
                maximum,
            )
            maximum = min(maximum, first_protected)
            if maximum <= 0:
                break
            prefix: list[CanonicalMessage] = []
            used_tokens = 0
            for message in messages[:maximum]:
                cost = estimate_tokens(self._message_line(message))
                if prefix and used_tokens + cost > self.compartment_target_tokens:
                    break
                prefix.append(message)
                used_tokens += cost
            if not prefix:
                break
            self._publish(scope.key, cursor, prefix)
            materialized += 1
        return materialized

    def _publish(
        self,
        scope_key: str,
        expected_cursor: int,
        messages: list[CanonicalMessage],
        *,
        summaries: tuple[str, str, str] | None = None,
        summary_p4: str = "",
        topic: str = "",
        importance: float = 0.5,
        confidence: float = 0.5,
        participants: tuple[str, ...] = (),
        evidence_ids: tuple[int, ...] = (),
        generation_mode: str = "legacy",
    ) -> int:
        source_ids = tuple(
            message.canonical_message_id for message in messages
        )
        if not source_ids:
            raise ValueError("cannot publish an empty context compartment")
        generated = summaries or self._summaries(messages)
        source_hash = self._source_hash(messages)
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtext(?))",
                (f"context:{scope_key}",),
            )
            state = cursor.execute(
                """
                SELECT last_compacted_message_id, next_ordinal
                FROM context_state WHERE scope_key = ?
                """,
                (scope_key,),
            ).fetchone()
            if state is None or int(state[0]) != int(expected_cursor):
                raise RuntimeError("context cursor changed during publication")
            ordinal = int(state[1])
            handle = str(uuid.uuid4())
            cursor.execute(
                """
                INSERT INTO context_compartments (
                    scope_key, ordinal, expand_handle,
                    start_message_id, end_message_id,
                    source_message_ids_json, source_hash,
                    message_count, token_estimate,
                    summary_p1, summary_p2, summary_p3,
                    summary_p4, topic, importance, confidence,
                    participants_json, evidence_ids_json, generation_mode,
                    active, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    scope_key,
                    ordinal,
                    handle,
                    source_ids[0],
                    source_ids[-1],
                    json.dumps(source_ids, separators=(",", ":")),
                    source_hash,
                    len(source_ids),
                    sum(
                        estimate_tokens(self._message_line(message))
                        for message in messages
                    ),
                    generated[0],
                    generated[1],
                    generated[2],
                    summary_p4,
                    topic,
                    _bounded_score(importance),
                    _bounded_score(confidence),
                    json.dumps(
                        participants,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        evidence_ids or source_ids,
                        separators=(",", ":"),
                    ),
                    generation_mode,
                    int(time.time()),
                ),
            )
            compartment_id = int(cursor.lastrowid)
            cursor.execute(
                """
                UPDATE context_state
                SET last_compacted_message_id = ?, next_ordinal = ?,
                    updated_at = ?
                WHERE scope_key = ?
                """,
                (source_ids[-1], ordinal + 1, int(time.time()), scope_key),
            )
            return compartment_id

    def _fit_raw_tail(
        self,
        messages: list[CanonicalMessage],
        lines: list[str],
        *,
        token_budget: int,
    ) -> tuple[str, list[int], int, bool]:
        if not messages:
            return "", [], 0, False
        chosen_indexes: set[int] = set()
        used = 0
        for index in range(len(messages) - 1, -1, -1):
            if index in chosen_indexes:
                continue
            cost = estimate_tokens(lines[index])
            if chosen_indexes and used + cost > token_budget:
                # The live tail is a chronological suffix, not a bag of
                # individually cheap messages. Skipping one line and then
                # admitting older lines creates invisible holes that change
                # the apparent conversation.
                break
            chosen_indexes.add(index)
            used += cost
            if used >= token_budget:
                break
        ordered = sorted(chosen_indexes)
        # Drop complete blocks, leaving room for several new messages before
        # the oldest visible line moves again. Never drop the live minimum.
        aligned = ((ordered[0] + HISTORY_BLOCK_LINES - 1) // HISTORY_BLOCK_LINES) * HISTORY_BLOCK_LINES
        if len(messages) - aligned >= min(self.raw_tail_min_messages, len(ordered)):
            ordered = list(range(aligned, len(messages)))
            used = sum(estimate_tokens(lines[index]) for index in ordered)
        chosen_lines = [lines[index] for index in ordered]
        chosen_ids = [messages[index].canonical_message_id for index in ordered]
        return (
            "\n".join(line for line in chosen_lines if line),
            chosen_ids,
            used,
            len(ordered) < len(messages),
        )

    def _summaries(
        self,
        messages: list[CanonicalMessage],
    ) -> tuple[str, str, str]:
        p1 = self._bounded_summary_lines(messages, per_message=300, cap=4000)
        p2 = self._bounded_summary_lines(messages, per_message=140, cap=2000)
        prompt_messages = [message for message in messages if message.prompt_text]
        participants: list[str] = []
        for message in prompt_messages:
            if message.sender_display not in participants:
                participants.append(message.sender_display)
        keywords = _keywords(" ".join(message.prompt_text for message in prompt_messages))
        start = datetime.fromtimestamp(messages[0].occurred_at).strftime(
            "%m-%d %H:%M"
        )
        end = datetime.fromtimestamp(messages[-1].occurred_at).strftime(
            "%m-%d %H:%M"
        )
        p3 = (
            f"{start} 至 {end}，{len(messages)} 条消息；参与者："
            f"{', '.join(participants[:8]) or '未知'}；关键词："
            f"{', '.join(keywords) or '无明显关键词'}。"
        )
        return p1, p2, p3[:500]

    def _bounded_summary_lines(
        self,
        messages: list[CanonicalMessage],
        *,
        per_message: int,
        cap: int,
    ) -> str:
        lines = []
        for message in messages:
            text = " ".join(message.prompt_text.split())[:per_message]
            if not text:
                continue
            lines.append(
                f"msg#{message.canonical_message_id} "
                f"{message.sender_display}: {text or '[非文本消息]'}"
            )
        rendered = "\n".join(lines)
        return rendered[:cap]

    @staticmethod
    def _message_line(message: CanonicalMessage) -> str:
        if not message.prompt_text:
            return ""
        sender = (
            f"[mention#{message.sender_principal_id}] {message.sender_display}"
            if message.sender_principal_id is not None
            else message.sender_display
        )
        reply = (
            f" reply:msg#{message.reply_to_canonical_message_id}"
            if message.reply_to_canonical_message_id is not None
            else ""
        )
        stamp = datetime.fromtimestamp(message.occurred_at).strftime(
            "%m-%d %H:%M"
        )
        return (
            f"[msg#{message.canonical_message_id}{reply} | {stamp} | "
            f"{sender}] {message.prompt_text}"
        )

    @staticmethod
    def _compartment_block(
        compartment: CompartmentRecord,
        summary: str,
    ) -> str:
        details = ""
        if compartment.topic:
            details = (
                f" | topic:{compartment.topic} | "
                f"importance:{compartment.importance:.2f} | "
                f"confidence:{compartment.confidence:.2f}"
            )
        return (
            f"[episode#{compartment.expand_handle} | "
            f"msg#{compartment.start_message_id}..msg#{compartment.end_message_id} | "
            f"{compartment.message_count} messages{details}]\n{summary}"
        )

    @staticmethod
    def _source_hash(messages: list[CanonicalMessage]) -> str:
        payload = [
            {
                "id": message.canonical_message_id,
                "sender": message.sender_principal_id,
                "direction": message.direction,
                "body": body_to_json(message.body),
                "reply": message.reply_to_canonical_message_id,
            }
            for message in messages
        ]
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _cursor(self, scope_key: str, floor: int) -> int:
        with self._transaction() as cursor:
            self._ensure_state(cursor, scope_key, floor)
            row = cursor.execute(
                """
                SELECT last_compacted_message_id FROM context_state
                WHERE scope_key = ?
                """,
                (scope_key,),
            ).fetchone()
            return int(row[0]) if row is not None else max(floor - 1, 0)

    def _sync_visibility(self, scope_key: str, floor: int) -> None:
        floor = max(int(floor), 1)
        with self._transaction() as cursor:
            self._ensure_state(cursor, scope_key, floor)
            row = cursor.execute(
                """
                SELECT visibility_floor FROM context_state
                WHERE scope_key = ?
                """,
                (scope_key,),
            ).fetchone()
            current_floor = int(row[0]) if row is not None else 1
            if floor <= current_floor:
                return
            cursor.execute(
                """
                UPDATE context_compartments SET active = 0
                WHERE scope_key = ? AND start_message_id < ?
                """,
                (scope_key, floor),
            )
            cursor.execute(
                """
                UPDATE context_state
                SET visibility_floor = ?, last_compacted_message_id = ?,
                    updated_at = ?
                WHERE scope_key = ?
                """,
                (floor, floor - 1, int(time.time()), scope_key),
            )

    @staticmethod
    def _ensure_state(
        cursor: StoreCursor,
        scope_key: str,
        floor: int,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO context_state (
                scope_key, visibility_floor, last_compacted_message_id,
                next_ordinal, updated_at
            ) VALUES (?, ?, ?, 1, ?)
            ON CONFLICT (scope_key) DO NOTHING
            """,
            (scope_key, max(int(floor), 1), max(int(floor) - 1, 0), int(time.time())),
        )

    def _active_compartments(self, scope_key: str) -> list[CompartmentRecord]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM context_compartments
                WHERE scope_key = ? AND active = 1
                ORDER BY ordinal ASC
                """,
                (scope_key,),
            ).fetchall()
        return [self._row_to_compartment(row) for row in rows]

    @staticmethod
    def _row_to_compartment(row: StoreRow) -> CompartmentRecord:
        try:
            raw_ids = json.loads(str(row["source_message_ids_json"]))
        except json.JSONDecodeError:
            raw_ids = []
        ids = tuple(int(item) for item in raw_ids if isinstance(item, int))
        participants = _json_text_tuple(
            _row_value(row, "participants_json", "[]")
        )
        evidence_ids = _json_int_tuple(
            _row_value(row, "evidence_ids_json", "[]")
        )
        return CompartmentRecord(
            compartment_id=int(row["compartment_id"]),
            scope_key=str(row["scope_key"]),
            ordinal=int(row["ordinal"]),
            expand_handle=str(row["expand_handle"]),
            start_message_id=int(row["start_message_id"]),
            end_message_id=int(row["end_message_id"]),
            source_message_ids=ids,
            source_hash=str(row["source_hash"]),
            message_count=int(row["message_count"]),
            token_estimate=int(row["token_estimate"]),
            summary_p1=str(row["summary_p1"]),
            summary_p2=str(row["summary_p2"]),
            summary_p3=str(row["summary_p3"]),
            created_at=int(row["created_at"]),
            summary_p4=str(_row_value(row, "summary_p4", "")),
            topic=str(_row_value(row, "topic", "")),
            importance=_bounded_score(_row_value(row, "importance", 0.5)),
            confidence=_bounded_score(_row_value(row, "confidence", 0.5)),
            participants=participants,
            evidence_ids=evidence_ids or ids,
            generation_mode=str(_row_value(row, "generation_mode", "legacy")),
        )

    @contextmanager
    def _transaction(self) -> Iterator[StoreCursor]:
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute("BEGIN")
                yield cursor
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()
            finally:
                cursor.close()


def estimate_tokens(text: str) -> int:
    units = 0.0
    for character in text:
        units += 1.0 if ord(character) > 127 else 0.25
    return max(int(units + 0.999), 1) if text else 0


def _row_value(row: StoreRow, key: str, default: object) -> object:
    return row[key] if key in row.keys() else default


def _json_text_tuple(value: object) -> tuple[str, ...]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return ()
    if not isinstance(decoded, list):
        return ()
    return tuple(str(item) for item in decoded if str(item).strip())


def _json_int_tuple(value: object) -> tuple[int, ...]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return ()
    if not isinstance(decoded, list):
        return ()
    return tuple(int(item) for item in decoded if isinstance(item, int))


def _bounded_score(value: object) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        score = 0.5
    return min(max(score, 0.0), 1.0)


def _keywords(text: str, limit: int = 8) -> list[str]:
    candidates = re.findall(r"[A-Za-z][A-Za-z0-9_.+-]{2,}|[\u4e00-\u9fff]{2,6}", text)
    ignored = {
        "这个",
        "那个",
        "可以",
        "然后",
        "还是",
        "什么",
        "怎么",
        "一下",
        "我们",
        "你们",
        "他们",
    }
    counts: dict[str, int] = {}
    original: dict[str, str] = {}
    for candidate in candidates:
        folded = candidate.casefold()
        if folded in ignored:
            continue
        counts[folded] = counts.get(folded, 0) + 1
        original.setdefault(folded, candidate)
    ranked = sorted(counts, key=lambda item: (-counts[item], item))
    return [original[item] for item in ranked[:limit]]
