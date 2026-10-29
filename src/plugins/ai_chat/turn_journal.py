from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import zlib
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator, Literal

from src.bot_storage import DatabaseSource, open_store_connection, StoreCursor, StoreRow

from .conversation_scope import ConversationScope
from .reaction_feedback import ReactionVoteStoreMixin


TurnStatus = Literal[
    "running",
    "succeeded",
    "silence",
    "aborted",
    "crashed",
]
ToolState = Literal[
    "started",
    "rejected",
    "succeeded",
    "failed",
    "committed",
    "outcome-unknown",
    "timed-out",
    "cancelled",
    "compensated",
    "handed-off",
]
SEND_LOOP_SEQUENCE_BASE = 1_000_000


@dataclass(frozen=True)
class TurnRecord:
    turn_id: int
    scope_key: str
    turn_ordinal: int
    trigger_canonical_message_id: int | None
    status: str
    provider: str
    model: str
    profile: str
    objective: str
    final_text: str
    started_at: int
    finished_at: int | None
    tool_call_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    prompt_version: str
    tool_catalog_version: str

    @property
    def handle(self) -> str:
        return f"t#{self.turn_ordinal}"


@dataclass(frozen=True)
class TurnEventRecord:
    event_id: int
    turn_id: int
    loop_sequence: int
    node_id: str
    event_kind: str
    state: str
    tool_name: str
    input_json: str
    result_json: str
    detail: str
    effect_labels: tuple[str, ...]
    occurred_at: int


@dataclass(frozen=True)
class ReplayBundle:
    mode: Literal["verbatim", "digest"]
    messages: tuple[dict[str, Any], ...]
    digest_prefix: str
    reason: str
    turn_ordinals: tuple[int, ...]
    covered_canonical_message_ids: tuple[int, ...]


class TurnJournal(ReactionVoteStoreMixin):
    def __init__(
        self,
        path: DatabaseSource,
        *,
        archive_ttl_days: int = 14,
        archive_max_per_scope: int = 50,
        archive_max_bytes: int = 512 * 1024,
        event_max_chars: int = 12000,
    ) -> None:
        self.path, self._connection = open_store_connection(path)
        self.archive_ttl_seconds = max(int(archive_ttl_days), 0) * 86400
        self.archive_max_per_scope = max(int(archive_max_per_scope), 0)
        self.archive_max_bytes = max(int(archive_max_bytes), 4096)
        self.event_max_chars = max(int(event_max_chars), 1000)
        self._lock = threading.RLock()
        self.recovered_unknown_effects = 0
        self.recovered_crashed_turns = 0

    def recover_interrupted(self) -> None:
        """Only the owning service startup may recover interrupted execution."""
        self.recovered_unknown_effects = self.mark_started_effects_unknown()
        self.recovered_crashed_turns = self.mark_running_turns_crashed()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def start_turn(
        self,
        scope: ConversationScope,
        *,
        trigger_canonical_message_id: int | None,
        objective: str,
        provider: str,
        model: str,
        profile: str = "default",
        prompt_version: str = "",
        tool_catalog_version: str = "",
        started_at: int | None = None,
        transaction_cursor: StoreCursor | None = None,
    ) -> TurnRecord:
        now = int(started_at or time.time())
        with (nullcontext(transaction_cursor) if transaction_cursor is not None else self._transaction()) as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtext(?))",
                (f"turn:{scope.key}",),
            )
            self._ensure_visibility(cursor, scope.key)
            row = cursor.execute(
                """
                SELECT COALESCE(MAX(turn_ordinal), 0) + 1 AS next_ordinal
                FROM agent_turns WHERE scope_key = ?
                """,
                (scope.key,),
            ).fetchone()
            ordinal = int(row["next_ordinal"])
            cursor.execute(
                """
                INSERT INTO agent_turns (
                    scope_key, turn_ordinal, trigger_canonical_message_id,
                    status, provider, model, profile, objective,
                    started_at, prompt_version, tool_catalog_version
                ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scope.key,
                    ordinal,
                    trigger_canonical_message_id,
                    provider,
                    model,
                    profile,
                    _safe_text(objective, 1000),
                    now,
                    prompt_version,
                    tool_catalog_version,
                ),
            )
            turn_id = int(cursor.lastrowid)
            stored = self._turn_row(cursor, turn_id)
        if stored is None:
            raise RuntimeError("turn was not stored")
        return self._row_to_turn(stored)

    def update_environment(
        self,
        turn_id: int,
        *,
        model: str,
        prompt_version: str,
        tool_catalog_version: str,
        provider: str | None = None,
        profile: str | None = None,
    ) -> None:
        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE agent_turns
                SET provider = COALESCE(?, provider),
                    model = ?,
                    profile = COALESCE(?, profile),
                    prompt_version = ?,
                    tool_catalog_version = ?
                WHERE turn_id = ? AND status = 'running'
                """,
                (
                    provider,
                    model,
                    profile,
                    prompt_version,
                    tool_catalog_version,
                    int(turn_id),
                ),
            )

    def record_context_plan(
        self,
        turn_id: int,
        payload: dict[str, Any],
        *,
        created_at: int | None = None,
    ) -> None:
        scope_key = str(payload.get("scope_key") or "").strip()
        if not scope_key:
            raise ValueError("context plan scope_key is required")
        now = int(created_at or time.time())
        with self._transaction() as cursor:
            turn = self._turn_row(cursor, int(turn_id))
            if turn is None or str(turn["scope_key"]) != scope_key:
                raise ValueError("context plan must belong to the turn scope")
            cursor.execute(
                """
                INSERT INTO turn_context_plans (
                    turn_id, scope_key, current_message_id,
                    current_principal_id, focus_message_id, confidence,
                    reason_codes_json, related_message_ids_json,
                    candidates_json, recall_route_json, adaptive_budget_json,
                    evidence_guard_json, topic_id, topic_message_ids_json,
                    topic_query, recall_candidates_json, resolver_version,
                    context_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(turn_id) DO UPDATE SET
                    scope_key = excluded.scope_key,
                    current_message_id = excluded.current_message_id,
                    current_principal_id = excluded.current_principal_id,
                    focus_message_id = excluded.focus_message_id,
                    confidence = excluded.confidence,
                    reason_codes_json = excluded.reason_codes_json,
                    related_message_ids_json = excluded.related_message_ids_json,
                    candidates_json = excluded.candidates_json,
                    recall_route_json = excluded.recall_route_json,
                    adaptive_budget_json = excluded.adaptive_budget_json,
                    evidence_guard_json = excluded.evidence_guard_json,
                    topic_id = excluded.topic_id,
                    topic_message_ids_json = excluded.topic_message_ids_json,
                    topic_query = excluded.topic_query,
                    recall_candidates_json = excluded.recall_candidates_json,
                    resolver_version = excluded.resolver_version,
                    context_hash = excluded.context_hash,
                    created_at = excluded.created_at
                """,
                (
                    int(turn_id),
                    scope_key,
                    max(int(payload.get("current_message_id") or 0), 0),
                    payload.get("current_principal_id"),
                    payload.get("focus_message_id"),
                    min(max(float(payload.get("confidence") or 0.0), 0.0), 1.0),
                    _safe_json(payload.get("reason_codes") or [], 2000),
                    _safe_json(payload.get("related_message_ids") or [], 4000),
                    _safe_json(payload.get("candidates") or [], 8000),
                    _safe_json(payload.get("recall_route") or {}, 4000),
                    _safe_json(payload.get("adaptive_budget") or {}, 4000),
                    _safe_json(payload.get("evidence_guard") or {}, 4000),
                    payload.get("topic_id"),
                    _safe_json(payload.get("topic_message_ids") or [], 8000),
                    _safe_text(str(payload.get("topic_query") or ""), 1000),
                    _safe_json(payload.get("recall_candidates") or [], 64000),
                    _safe_text(str(payload.get("resolver_version") or ""), 100),
                    _safe_text(str(payload.get("context_hash") or ""), 64),
                    now,
                ),
            )

    def recent_context_plans(self, *, limit: int = 100, turn_id: int | None = None,
                             negative_only: bool = False) -> list[dict[str, Any]]:
        bounded = min(max(int(limit), 1), 500)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT plan.*, turn.turn_ordinal, turn.objective,
                       turn.status, turn.model, turn.profile,
                       feedback.verdict AS feedback_verdict,
                       feedback.note AS feedback_note,
                       feedback.resource_version AS feedback_resource_version,
                       feedback.actor AS feedback_actor,
                       feedback.updated_at AS feedback_updated_at
                FROM turn_context_plans AS plan
                JOIN agent_turns AS turn ON turn.turn_id = plan.turn_id
                JOIN turn_visibility AS visibility
                  ON visibility.scope_key = turn.scope_key
                LEFT JOIN turn_context_feedback AS feedback
                  ON feedback.turn_id = plan.turn_id
                WHERE turn.turn_ordinal >= visibility.min_turn_ordinal
                  AND (? = 0 OR turn.turn_id = ?)
                  AND (? = 0 OR feedback.verdict = 'off_topic' OR EXISTS (
                      SELECT 1 FROM turn_reaction_votes AS rv
                      WHERE rv.turn_id = turn.turn_id AND rv.vote = -1))
                ORDER BY plan.created_at DESC, plan.turn_id DESC
                LIMIT ?
                """,
                (int(turn_id is not None), int(turn_id or 0), int(negative_only), bounded),
            ).fetchall()
        votes = self.reaction_totals([int(row["turn_id"]) for row in rows])
        return [
            {
                "turn_id": int(row["turn_id"]),
                "votes": votes.get(int(row["turn_id"]), {"asker": None, "others_up": 0, "others_down": 0}),
                "turn_handle": f"t#{int(row['turn_ordinal'])}",
                "scope_key": str(row["scope_key"]),
                "current_message_id": int(row["current_message_id"]),
                "current_principal_id": (
                    int(row["current_principal_id"])
                    if row["current_principal_id"] is not None
                    else None
                ),
                "focus_message_id": (
                    int(row["focus_message_id"])
                    if row["focus_message_id"] is not None
                    else None
                ),
                "confidence": float(row["confidence"]),
                "reason_codes": _json_list(row["reason_codes_json"]),
                "related_message_ids": _json_list(
                    row["related_message_ids_json"]
                ),
                "candidates": _json_list(row["candidates_json"]),
                "recall_route": _json_object(row["recall_route_json"]),
                "adaptive_budget": _json_object(row["adaptive_budget_json"]),
                "evidence_guard": _json_object(row["evidence_guard_json"]),
                "topic_id": (
                    int(row["topic_id"]) if row["topic_id"] is not None else None
                ),
                "topic_message_ids": _json_list(
                    row["topic_message_ids_json"]
                ),
                "topic_query": str(row["topic_query"]),
                "recall_candidates": _json_list(
                    row["recall_candidates_json"]
                ),
                "resolver_version": str(row["resolver_version"]),
                "context_hash": str(row["context_hash"]),
                "objective": str(row["objective"]),
                "status": str(row["status"]),
                "model": str(row["model"]),
                "profile": str(row["profile"]),
                "feedback": (
                    {
                        "verdict": str(row["feedback_verdict"]),
                        "note": str(row["feedback_note"]),
                        "resource_version": int(
                            row["feedback_resource_version"]
                        ),
                        "actor": str(row["feedback_actor"]),
                        "updated_at": int(row["feedback_updated_at"]),
                    }
                    if row["feedback_verdict"] is not None
                    else None
                ),
                "created_at": int(row["created_at"]),
            }
            for row in rows
        ]

    def set_context_feedback(
        self,
        turn_id: int,
        *,
        verdict: str,
        note: str,
        actor: str,
        resource_version: int,
        updated_at: int | None = None,
    ) -> dict[str, object]:
        normalized_verdict = str(verdict).strip().casefold()
        if normalized_verdict not in {"correct", "off_topic"}:
            raise ValueError("context feedback verdict is invalid")
        normalized_actor = _safe_text(" ".join(str(actor).split()), 160)
        if not normalized_actor:
            normalized_actor = "admin-console"
        normalized_note = _safe_text(str(note), 1000)
        now = int(updated_at or time.time())
        with self._transaction() as cursor:
            visible = cursor.execute(
                """
                SELECT 1
                FROM turn_context_plans AS plan
                JOIN agent_turns AS turn ON turn.turn_id = plan.turn_id
                JOIN turn_visibility AS visibility
                  ON visibility.scope_key = turn.scope_key
                WHERE plan.turn_id = ?
                  AND turn.turn_ordinal >= visibility.min_turn_ordinal
                """,
                (int(turn_id),),
            ).fetchone()
            if visible is None:
                raise ValueError("context plan is not visible")
            cursor.execute(
                """
                INSERT INTO turn_context_feedback (
                    turn_id, verdict, note, resource_version, actor,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(turn_id) DO UPDATE SET
                    verdict = excluded.verdict,
                    note = excluded.note,
                    resource_version = excluded.resource_version,
                    actor = excluded.actor,
                    updated_at = excluded.updated_at
                """,
                (
                    int(turn_id),
                    normalized_verdict,
                    normalized_note,
                    int(resource_version),
                    normalized_actor,
                    now,
                    now,
                ),
            )
        return {
            "turn_id": int(turn_id),
            "verdict": normalized_verdict,
            "note": normalized_note,
            "resource_version": int(resource_version),
            "actor": normalized_actor,
            "updated_at": now,
        }

    def recent_trace_summaries(self, *, limit: int = 50) -> list[dict[str, object]]:
        """Return recent operational trace metadata without conversation content."""
        bounded = min(max(int(limit), 1), 200)
        now = int(time.time())
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT turn.turn_id, turn.turn_ordinal, turn.status,
                       turn.provider, turn.model, turn.profile,
                       turn.started_at, turn.finished_at,
                       turn.tool_call_count, turn.input_tokens,
                       turn.output_tokens, turn.total_tokens,
                       archive.payload AS archive_payload,
                       archive.expires_at AS archive_expires_at
                FROM agent_turns AS turn
                LEFT JOIN turn_archives AS archive
                  ON archive.turn_id = turn.turn_id
                WHERE turn.status != 'running'
                ORDER BY turn.started_at DESC, turn.turn_id DESC
                LIMIT ?
                """,
                (bounded,),
            ).fetchall()
            turn_ids = [int(row["turn_id"]) for row in rows]
            events = []
            if turn_ids:
                placeholders = ", ".join("?" for _ in turn_ids)
                events = self._connection.execute(
                    f"""
                    SELECT turn_id, tool_name, state
                    FROM turn_journal_events
                    WHERE event_kind = 'tool'
                      AND state != 'started'
                      AND turn_id IN ({placeholders})
                    ORDER BY event_id ASC
                    """,
                    turn_ids,
                ).fetchall()

        tools_by_turn: dict[int, set[str]] = {}
        failures_by_turn: dict[int, int] = {}
        for event in events:
            turn_id = int(event["turn_id"])
            tool_name = str(event["tool_name"] or "").strip()
            if tool_name:
                tools_by_turn.setdefault(turn_id, set()).add(tool_name)
            if str(event["state"]) in {"failed", "rejected", "outcome-unknown"}:
                failures_by_turn[turn_id] = failures_by_turn.get(turn_id, 0) + 1

        summaries: list[dict[str, object]] = []
        for row in rows:
            turn_id = int(row["turn_id"])
            payload = _decode_trace_archive(
                row["archive_payload"],
                int(row["archive_expires_at"] or 0),
                now,
            )
            routes = _safe_model_routes(payload.get("model_routes"))
            routing = _safe_model_routing(payload.get("model_routing"))
            requested = routing[0] if routing else {}
            actual = routing[-1] if routing else {}
            started_at = int(row["started_at"])
            finished_at = (
                int(row["finished_at"])
                if row["finished_at"] is not None
                else None
            )
            summaries.append(
                {
                    "trace_id": _safe_trace_id(payload.get("trace_id")),
                    "turn_id": turn_id,
                    "turn_handle": f"t#{int(row['turn_ordinal'])}",
                    "status": str(row["status"]),
                    "provider": str(row["provider"]),
                    "model": str(row["model"]),
                    "profile": str(row["profile"]),
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "duration_seconds": (
                        max(finished_at - started_at, 0)
                        if finished_at is not None
                        else None
                    ),
                    "tool_call_count": int(row["tool_call_count"]),
                    "tool_failures": failures_by_turn.get(turn_id, 0),
                    "tools": sorted(tools_by_turn.get(turn_id, set())),
                    "input_tokens": int(row["input_tokens"]),
                    "output_tokens": int(row["output_tokens"]),
                    "total_tokens": int(row["total_tokens"]),
                    "model_routes": routes,
                    "model_routing": routing,
                    "requested_profile": requested.get("requested_profile", routes[0]["profile"] if routes else str(row["profile"])),
                    "requested_model": requested.get("requested_model", routes[0]["model"] if routes else str(row["model"])),
                    "actual_profile": actual.get("actual_profile", routes[-1]["profile"] if routes else str(row["profile"])),
                    "actual_model": actual.get("actual_model", routes[-1]["model"] if routes else str(row["model"])),
                    "routing_reason": next((item["reason"] for item in routing if item["fallback"] or not item["actual_profile"]), ""),
                    "fallback": any(item["fallback"] for item in routing) if routing else len(routes) > 1,
                }
            )
        return summaries

    def record_model_note(
        self,
        turn_id: int,
        loop_sequence: int,
        content: str,
    ) -> None:
        content = _safe_text(content, min(self.event_max_chars, 2000))
        if not content:
            return
        self._insert_event(
            turn_id=turn_id,
            loop_sequence=loop_sequence,
            event_kind="model_note",
            state="succeeded",
            detail=content,
        )

    def record_tool_started(
        self,
        turn_id: int,
        loop_sequence: int,
        tool_name: str,
        arguments: dict[str, Any],
        effect_labels: list[str] | tuple[str, ...],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._insert_event(
            turn_id=turn_id,
            loop_sequence=loop_sequence,
            event_kind="tool",
            state="started",
            tool_name=tool_name,
            input_json=_safe_json(arguments, self.event_max_chars),
            detail=_safe_json(metadata or {}, min(self.event_max_chars, 2000)),
            effect_labels=effect_labels,
            increment_tool_count=True,
        )

    def record_tool_finished(
        self,
        turn_id: int,
        loop_sequence: int,
        tool_name: str,
        state: ToolState,
        result: str,
        effect_labels: list[str] | tuple[str, ...],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if state == "started":
            raise ValueError("finished tool event cannot use started state")
        self._insert_event(
            turn_id=turn_id,
            loop_sequence=loop_sequence,
            event_kind="tool",
            state=state,
            tool_name=tool_name,
            result_json=_safe_result(result, self.event_max_chars),
            detail=_safe_json(metadata or {}, min(self.event_max_chars, 2000)),
            effect_labels=effect_labels,
        )

    def record_tool_rejected(
        self,
        turn_id: int,
        loop_sequence: int,
        tool_name: str,
        arguments: dict[str, Any],
        result: str,
        effect_labels: list[str] | tuple[str, ...],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._insert_event(
            turn_id=turn_id,
            loop_sequence=loop_sequence,
            event_kind="tool",
            state="rejected",
            tool_name=tool_name,
            input_json=_safe_json(arguments, self.event_max_chars),
            result_json=_safe_result(result, self.event_max_chars),
            detail=_safe_json(metadata or {}, min(self.event_max_chars, 2000)),
            effect_labels=effect_labels,
            increment_tool_count=True,
        )

    def record_send_started(self, turn_id: int, attempt: int) -> None:
        self._insert_event(
            turn_id=turn_id,
            loop_sequence=SEND_LOOP_SEQUENCE_BASE + max(int(attempt), 1),
            event_kind="tool",
            state="started",
            tool_name="reply_send",
            input_json=_safe_json(
                {"attempt": max(int(attempt), 1)},
                self.event_max_chars,
            ),
            effect_labels=("send:conversation",),
        )
        self._refresh_digest(turn_id)

    def record_send_finished(
        self,
        turn_id: int,
        attempt: int,
        state: Literal["committed", "failed", "outcome-unknown"],
        result: dict[str, Any],
    ) -> None:
        self._insert_event(
            turn_id=turn_id,
            loop_sequence=SEND_LOOP_SEQUENCE_BASE + max(int(attempt), 1),
            event_kind="tool",
            state=state,
            tool_name="reply_send",
            result_json=_safe_json(result, self.event_max_chars),
            effect_labels=("send:conversation",),
        )
        self._refresh_digest(turn_id)

    def finish_turn(
        self,
        turn_id: int,
        *,
        status: TurnStatus,
        final_text: str = "",
        trace_payload: dict[str, Any] | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        finished_at: int | None = None,
        transaction_cursor: StoreCursor | None = None,
    ) -> TurnRecord | None:
        if status == "running":
            raise ValueError("finish_turn requires a terminal status")
        now = int(finished_at or time.time())
        with (nullcontext(transaction_cursor) if transaction_cursor is not None else self._transaction()) as cursor:
            cursor.execute(
                """
                UPDATE agent_turns
                SET status = ?, final_text = ?, finished_at = ?,
                    input_tokens = ?, output_tokens = ?, total_tokens = ?
                WHERE turn_id = ? AND status = 'running'
                """,
                (
                    status,
                    _safe_text(final_text, 4000),
                    now,
                    max(int(input_tokens), 0),
                    max(int(output_tokens), 0),
                    max(int(total_tokens), 0),
                    int(turn_id),
                ),
            )
            row = self._turn_row(cursor, int(turn_id))
            if row is None:
                return None
            self._upsert_digest(cursor, row, now)
            if trace_payload and not _contains_secret(trace_payload):
                self._store_archive(cursor, row, trace_payload, now)
            self._cleanup_archives(cursor, str(row["scope_key"]), now)
            return self._row_to_turn(row)

    def mark_running_turns_crashed(self) -> int:
        now = int(time.time())
        with self._transaction() as cursor:
            rows = cursor.execute(
                """SELECT turn_id FROM agent_turns WHERE status = 'running'
                   AND NOT EXISTS (SELECT 1 FROM qq_official_turn_executions x
                                   WHERE x.journal_turn_id=agent_turns.turn_id)"""
            ).fetchall()
            cursor.execute(
                """
                UPDATE agent_turns
                SET status = 'crashed', finished_at = ?
                WHERE status = 'running'
                  AND NOT EXISTS (SELECT 1 FROM qq_official_turn_executions x
                                  WHERE x.journal_turn_id=agent_turns.turn_id)
                """,
                (now,),
            )
            for row in rows:
                turn_row = self._turn_row(cursor, int(row["turn_id"]))
                if turn_row is not None:
                    self._upsert_digest(cursor, turn_row, now)
            return len(rows)

    def mark_started_effects_unknown(self) -> int:
        now = int(time.time())
        result = _safe_json(
            {
                "ok": False,
                "error": "工具执行状态未知：服务在完成事件写入前中断。",
            },
            self.event_max_chars,
        )
        with self._transaction() as cursor:
            rows = cursor.execute(
                """
                SELECT e.*
                FROM turn_journal_events AS e
                WHERE e.event_kind = 'tool'
                  AND e.state = 'started'
                  AND NOT EXISTS (SELECT 1 FROM qq_official_turn_executions x
                                  WHERE x.journal_turn_id=e.turn_id)
                  AND NOT EXISTS (
                      SELECT 1 FROM turn_journal_events AS done
                      WHERE done.turn_id = e.turn_id
                        AND done.node_id = e.node_id
                        AND done.event_kind = 'tool'
                        AND done.state != 'started'
                  )
                ORDER BY e.event_id ASC
                """
            ).fetchall()
            for row in rows:
                cursor.execute(
                    """
                    INSERT INTO turn_journal_events (
                        turn_id, loop_sequence, node_id, event_kind, state,
                        tool_name, input_json, result_json, detail,
                        effect_labels_json, occurred_at
                    ) VALUES (?, ?, ?, 'tool', 'outcome-unknown', ?, '', ?, '', ?, ?)
                    """,
                    (
                        int(row["turn_id"]),
                        int(row["loop_sequence"]),
                        str(row["node_id"]),
                        str(row["tool_name"]),
                        result,
                        str(row["effect_labels_json"]),
                        now,
                    ),
                )
            for turn_id in {int(row["turn_id"]) for row in rows}:
                turn_row = self._turn_row(cursor, turn_id)
                if turn_row is not None:
                    self._upsert_digest(cursor, turn_row, now)
            return len(rows)

    def get_visible_turn(
        self,
        scope: ConversationScope,
        turn_ordinal: int,
    ) -> TurnRecord | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT t.*
                FROM agent_turns AS t
                JOIN turn_visibility AS v ON v.scope_key = t.scope_key
                WHERE t.scope_key = ? AND t.turn_ordinal = ?
                  AND t.turn_ordinal >= v.min_turn_ordinal
                """,
                (scope.key, int(turn_ordinal)),
            ).fetchone()
        return self._row_to_turn(row) if row is not None else None

    def get_turn_by_id(self, turn_id: int) -> TurnRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM agent_turns WHERE turn_id = ?",
                (int(turn_id),),
            ).fetchone()
        return self._row_to_turn(row) if row is not None else None

    def usage_summary(
        self,
        scope: ConversationScope,
        *,
        since_timestamp: int = 0,
    ) -> dict[str, int]:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT
                    COUNT(*) AS turns,
                    COALESCE(SUM(t.input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(t.output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(t.total_tokens), 0) AS total_tokens
                FROM agent_turns AS t
                JOIN turn_visibility AS v ON v.scope_key = t.scope_key
                WHERE t.scope_key = ?
                  AND t.turn_ordinal >= v.min_turn_ordinal
                  AND t.status != 'running'
                  AND t.started_at >= ?
                """,
                (scope.key, max(int(since_timestamp), 0)),
            ).fetchone()
        return {
            "turns": int(row["turns"] if row is not None else 0),
            "input_tokens": int(row["input_tokens"] if row is not None else 0),
            "output_tokens": int(row["output_tokens"] if row is not None else 0),
            "total_tokens": int(row["total_tokens"] if row is not None else 0),
        }

    def events_for_turn(self, turn_id: int) -> list[TurnEventRecord]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM turn_journal_events
                WHERE turn_id = ? ORDER BY event_id ASC
                """,
                (int(turn_id),),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def render_recent_turns(
        self,
        scope: ConversationScope,
        *,
        hours: int = 24,
        limit: int = 5,
        exclude_turn_id: int | None = None,
    ) -> str:
        limit = min(max(int(limit), 1), 20)
        cutoff = int(time.time()) - max(int(hours), 1) * 3600
        parameters: list[Any] = [scope.key, cutoff]
        exclusion = ""
        if exclude_turn_id is not None:
            exclusion = "AND t.turn_id != ?"
            parameters.append(int(exclude_turn_id))
        parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT t.*, plan.current_principal_id AS context_principal_id
                FROM agent_turns AS t
                JOIN turn_visibility AS v ON v.scope_key = t.scope_key
                LEFT JOIN turn_context_plans AS plan ON plan.turn_id = t.turn_id
                WHERE t.scope_key = ?
                  AND t.started_at >= ?
                  AND t.turn_ordinal >= v.min_turn_ordinal
                  AND t.tool_call_count > 0
                  AND t.status != 'running'
                  {exclusion}
                ORDER BY t.turn_ordinal DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        if not rows:
            return ""
        lines = [
            "[recent turns - 当前群共享工作记录，需要细节时调用 "
            "context_expand；只能归属给标注的发起人，未标注时发起人未知]"
        ]
        lines.extend(
            self._render_standing_line(
                self._row_to_turn(row),
                principal_id=(
                    int(row["context_principal_id"])
                    if row["context_principal_id"] is not None
                    else None
                ),
            )
            for row in rows
        )
        return "\n".join(lines)

    def render_turn(
        self,
        scope: ConversationScope,
        turn_ordinal: int,
        *,
        max_chars: int = 10000,
    ) -> str | None:
        turn = self.get_visible_turn(scope, turn_ordinal)
        if turn is None:
            return None
        events = self.events_for_turn(turn.turn_id)
        status = _status_label(turn.status)
        finished = (
            datetime.fromtimestamp(turn.finished_at).strftime("%m-%d %H:%M")
            if turn.finished_at
            else "尚未结束"
        )
        lines = [
            f"[{turn.handle} 规范工作记录]",
            f"状态: {status}; 开始: {datetime.fromtimestamp(turn.started_at).strftime('%m-%d %H:%M')}; 结束: {finished}",
            f"目标: {turn.objective or '未记录'}",
            f"模型: {turn.model or '未知'}; 工具调用: {turn.tool_call_count}",
        ]
        for event in events:
            if event.event_kind == "model_note":
                lines.append(f"- 模型说明: {event.detail}")
                continue
            if event.state == "started":
                arguments = _summarize_json(event.input_json, 600)
                labels = ",".join(event.effect_labels) or "unspecified"
                metadata = _summarize_json(event.detail, 400) if event.detail else ""
                lines.append(
                    f"- {event.node_id} START {event.tool_name}({arguments}) [{labels}]"
                    + (f" {metadata}" if metadata and metadata != "{}" else "")
                )
            elif event.state == "rejected":
                arguments = _summarize_json(event.input_json, 600)
                result = _summarize_json(event.result_json, 900)
                lines.append(
                    f"- {event.node_id} REJECTED {event.tool_name}({arguments}): {result}"
                )
            else:
                result = _summarize_json(event.result_json, 900)
                lines.append(
                    f"  -> {event.state.upper()} {event.tool_name}: {result}"
                )
        if turn.final_text:
            lines.append(f"最终回复: {turn.final_text}")
        rendered = "\n".join(lines)
        return rendered[: max(int(max_chars), 1000)]

    def replay_steps(
        self,
        scope: ConversationScope,
        turn_ordinal: int,
    ) -> tuple[dict[str, Any], ...]:
        """Return an audit replay; it never re-executes recorded side effects."""
        turn = self.get_visible_turn(scope, turn_ordinal)
        if turn is None:
            return ()
        steps: list[dict[str, Any]] = []
        for event in self.events_for_turn(turn.turn_id):
            steps.append(
                {
                    "node_id": event.node_id,
                    "sequence": event.loop_sequence,
                    "kind": event.event_kind,
                    "state": event.state,
                    "tool_name": event.tool_name,
                    "arguments": _json_object(event.input_json),
                    "result": _json_object(event.result_json),
                    "metadata": _json_object(event.detail),
                    "effect_labels": list(event.effect_labels),
                    "occurred_at": event.occurred_at,
                }
            )
        return tuple(steps)

    def link_send(
        self,
        turn_id: int,
        canonical_message_id: int,
        *,
        node_id: str = "final",
        chunk_index: int = 0,
        transaction_cursor: StoreCursor | None = None,
        strict_association: bool = False,
    ) -> None:
        """Link a receipt in the caller's transaction, optionally without reassignment.

        Strict callers may replay the exact association, but cannot overwrite a
        prior turn/node/chunk link. The external cursor remains caller-owned.
        """
        if strict_association and (
            type(turn_id) is not int or turn_id <= 0
            or type(canonical_message_id) is not int or canonical_message_id <= 0
            or not isinstance(node_id, str) or not node_id
            or type(chunk_index) is not int or chunk_index < 0
        ):
            raise ValueError("invalid strict send association")
        with (nullcontext(transaction_cursor) if transaction_cursor is not None else self._transaction()) as cursor:
            if strict_association:
                cursor.execute(
                    """INSERT INTO turn_send_links
                        (canonical_message_id,turn_id,node_id,chunk_index,created_at)
                        VALUES (?,?,?,?,?) ON CONFLICT(canonical_message_id) DO NOTHING""",
                    (canonical_message_id, turn_id, node_id, chunk_index, int(time.time())),
                )
                row = cursor.execute(
                    """SELECT turn_id,node_id,chunk_index FROM turn_send_links
                        WHERE canonical_message_id=? FOR UPDATE""", (canonical_message_id,),
                ).fetchone()
                if row is None or (row["turn_id"], row["node_id"], row["chunk_index"]) != (turn_id, node_id, chunk_index):
                    raise ValueError("send receipt already has a different association")
                return
            cursor.execute(
                """
                INSERT INTO turn_send_links (
                    canonical_message_id, turn_id, node_id,
                    chunk_index, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(canonical_message_id) DO UPDATE SET
                    turn_id = excluded.turn_id,
                    node_id = excluded.node_id,
                    chunk_index = excluded.chunk_index
                """,
                (
                    int(canonical_message_id),
                    int(turn_id),
                    node_id,
                    max(int(chunk_index), 0),
                    int(time.time()),
                ),
            )

    def find_turn_for_reply(
        self,
        scope: ConversationScope,
        canonical_message_id: int,
    ) -> TurnRecord | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT t.*
                FROM turn_send_links AS l
                JOIN agent_turns AS t ON t.turn_id = l.turn_id
                JOIN turn_visibility AS v ON v.scope_key = t.scope_key
                WHERE l.canonical_message_id = ?
                  AND t.scope_key = ?
                  AND t.turn_ordinal >= v.min_turn_ordinal
                """,
                (int(canonical_message_id), scope.key),
            ).fetchone()
        return self._row_to_turn(row) if row is not None else None

    def add_fork_edge(
        self,
        from_turn_id: int,
        to_turn_id: int,
        *,
        created_by_principal_id: int | None,
    ) -> bool:
        if int(from_turn_id) == int(to_turn_id):
            return False
        with self._transaction() as cursor:
            rows = cursor.execute(
                """
                SELECT turn_id, scope_key FROM agent_turns
                WHERE turn_id IN (?, ?)
                """,
                (int(from_turn_id), int(to_turn_id)),
            ).fetchall()
            scopes = {str(row["scope_key"]) for row in rows}
            if len(rows) != 2 or len(scopes) != 1:
                return False
            cursor.execute(
                """
                INSERT INTO turn_edges (
                    from_turn_id, to_turn_id, kind,
                    created_by_principal_id, created_at
                ) VALUES (?, ?, 'fork-from', ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    int(from_turn_id),
                    int(to_turn_id),
                    created_by_principal_id,
                    int(time.time()),
                ),
            )
            return cursor.rowcount > 0

    def hide_history(self, scope: ConversationScope) -> int:
        with self._transaction() as cursor:
            self._ensure_visibility(cursor, scope.key)
            visibility = cursor.execute(
                """
                SELECT min_turn_ordinal FROM turn_visibility
                WHERE scope_key = ?
                """,
                (scope.key,),
            ).fetchone()
            minimum = int(visibility[0])
            aggregate = cursor.execute(
                """
                SELECT COUNT(*) AS count, MAX(turn_ordinal) AS maximum
                FROM agent_turns
                WHERE scope_key = ? AND turn_ordinal >= ?
                """,
                (scope.key, minimum),
            ).fetchone()
            count = int(aggregate["count"] or 0)
            maximum = int(aggregate["maximum"] or (minimum - 1))
            cursor.execute(
                """
                UPDATE turn_visibility
                SET min_turn_ordinal = ?, cleared_at = ?
                WHERE scope_key = ?
                """,
                (maximum + 1, int(time.time()), scope.key),
            )
            return count

    def archive_payload(self, turn_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT payload, expires_at FROM turn_archives
                WHERE turn_id = ?
                """,
                (int(turn_id),),
            ).fetchone()
        if row is None or int(row["expires_at"]) <= int(time.time()):
            return None
        try:
            decoded = zlib.decompress(bytes(row["payload"])).decode("utf-8")
            payload = json.loads(decoded)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def digest_for_turn(self, turn_id: int) -> str:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT skeleton, approach FROM turn_digests
                WHERE turn_id = ?
                """,
                (int(turn_id),),
            ).fetchone()
        if row is None:
            with self._transaction() as cursor:
                turn_row = self._turn_row(cursor, int(turn_id))
                if turn_row is not None and str(turn_row["status"]) != "running":
                    self._upsert_digest(cursor, turn_row, int(time.time()))
                    row = cursor.execute(
                        """
                        SELECT skeleton, approach FROM turn_digests
                        WHERE turn_id = ?
                        """,
                        (int(turn_id),),
                    ).fetchone()
        if row is None:
            return ""
        return "\n".join(
            item for item in (str(row["skeleton"]), str(row["approach"])) if item
        )

    def _refresh_digest(self, turn_id: int) -> None:
        with self._transaction() as cursor:
            turn_row = self._turn_row(cursor, int(turn_id))
            if turn_row is not None:
                self._upsert_digest(cursor, turn_row, int(time.time()))

    def fork_parent(
        self,
        scope: ConversationScope,
        turn_id: int,
    ) -> TurnRecord | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT parent.*
                FROM turn_edges AS edge
                JOIN agent_turns AS child ON child.turn_id = edge.from_turn_id
                JOIN agent_turns AS parent ON parent.turn_id = edge.to_turn_id
                JOIN turn_visibility AS visibility
                  ON visibility.scope_key = parent.scope_key
                WHERE edge.from_turn_id = ? AND edge.kind = 'fork-from'
                  AND child.scope_key = ? AND parent.scope_key = ?
                  AND parent.turn_ordinal >= visibility.min_turn_ordinal
                ORDER BY edge.edge_id ASC
                LIMIT 1
                """,
                (int(turn_id), scope.key, scope.key),
            ).fetchone()
        return self._row_to_turn(row) if row is not None else None

    def build_replay(
        self,
        scope: ConversationScope,
        turn_ordinal: int,
        *,
        current_model: str,
        prompt_version: str,
        tool_catalog_version: str,
        current_provider: str = "",
        current_profile: str = "",
        max_chars: int = 40000,
        max_segments: int = 3,
    ) -> ReplayBundle:
        target = self.get_visible_turn(scope, turn_ordinal)
        if target is None:
            return ReplayBundle(
                "digest", (), "", "target-not-visible", (), ()
            )
        chain = self._fork_chain(scope, target, limit=20)
        admitted: list[tuple[TurnRecord, list[dict[str, Any]]]] = []
        digest_turns: list[TurnRecord] = []
        reason = "valid"
        used_chars = 0
        max_chars = max(int(max_chars), 1000)
        max_segments = min(max(int(max_segments), 1), 10)
        for index, turn in enumerate(chain):
            if index >= max_segments:
                digest_turns.extend(chain[index:])
                reason = "segment-budget"
                break
            invalid_reason = self._replay_invalid_reason(
                turn,
                current_model=current_model,
                current_provider=current_provider,
                current_profile=current_profile,
                prompt_version=prompt_version,
                tool_catalog_version=tool_catalog_version,
            )
            if invalid_reason:
                digest_turns.extend(chain[index:])
                reason = invalid_reason
                break
            payload = self.archive_payload(turn.turn_id)
            messages = _replay_messages(payload)
            if not messages:
                digest_turns.extend(chain[index:])
                reason = "archive-missing"
                break
            size = len(
                json.dumps(
                    messages,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                )
            )
            if used_chars + size > max_chars:
                digest_turns.extend(chain[index:])
                reason = "chain-budget"
                break
            admitted.append((turn, messages))
            used_chars += size

        digest_prefix = "\n\n".join(
            digest
            for turn in reversed(digest_turns)
            if (digest := self.digest_for_turn(turn.turn_id))
        )
        replay_messages: list[dict[str, Any]] = []
        for _turn, messages in reversed(admitted):
            replay_messages.extend(messages)
        covered_ids = self._covered_message_ids(
            [turn.turn_id for turn, _messages in admitted]
        )
        return ReplayBundle(
            mode="verbatim" if replay_messages else "digest",
            messages=tuple(replay_messages),
            digest_prefix=digest_prefix,
            reason=reason,
            turn_ordinals=tuple(turn.turn_ordinal for turn, _messages in admitted),
            covered_canonical_message_ids=covered_ids,
        )

    def _insert_event(
        self,
        *,
        turn_id: int,
        loop_sequence: int,
        event_kind: str,
        state: str,
        tool_name: str = "",
        input_json: str = "",
        result_json: str = "",
        detail: str = "",
        effect_labels: list[str] | tuple[str, ...] = (),
        increment_tool_count: bool = False,
    ) -> None:
        labels = sorted({str(label) for label in effect_labels if str(label)})
        node_id = (
            f"turn:{int(turn_id)}:{int(loop_sequence)}"
            if event_kind == "tool"
            else f"turn:{int(turn_id)}:note:{int(loop_sequence)}"
        )
        with self._transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO turn_journal_events (
                    turn_id, loop_sequence, node_id, event_kind, state,
                    tool_name, input_json, result_json, detail,
                    effect_labels_json, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(turn_id),
                    int(loop_sequence),
                    node_id,
                    event_kind,
                    state,
                    tool_name,
                    input_json,
                    result_json,
                    detail,
                    json.dumps(labels, separators=(",", ":")),
                    int(time.time()),
                ),
            )
            if increment_tool_count:
                cursor.execute(
                    """
                    UPDATE agent_turns
                    SET tool_call_count = tool_call_count + 1
                    WHERE turn_id = ?
                    """,
                    (int(turn_id),),
                )

    def _store_archive(
        self,
        cursor: StoreCursor,
        turn_row: StoreRow,
        trace_payload: dict[str, Any],
        now: int,
    ) -> None:
        if self.archive_ttl_seconds <= 0 or self.archive_max_per_scope <= 0:
            return
        encoded = json.dumps(
            trace_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        compressed = zlib.compress(encoded, level=6)
        if len(compressed) > self.archive_max_bytes:
            return
        cursor.execute(
            """
            INSERT INTO turn_archives (
                turn_id, provider, model, payload, byte_count,
                created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(turn_id) DO UPDATE SET
                provider = excluded.provider,
                model = excluded.model,
                payload = excluded.payload,
                byte_count = excluded.byte_count,
                created_at = excluded.created_at,
                expires_at = excluded.expires_at
            """,
            (
                int(turn_row["turn_id"]),
                str(turn_row["provider"]),
                str(turn_row["model"]),
                compressed,
                len(compressed),
                now,
                now + self.archive_ttl_seconds,
            ),
        )

    def _upsert_digest(
        self,
        cursor: StoreCursor,
        turn_row: StoreRow,
        now: int,
    ) -> None:
        events = cursor.execute(
            """
            SELECT * FROM turn_journal_events
            WHERE turn_id = ? ORDER BY event_id ASC
            """,
            (int(turn_row["turn_id"]),),
        ).fetchall()
        tool_lines = []
        notes = []
        for event in events:
            if str(event["event_kind"]) == "model_note":
                detail = _first_line(str(event["detail"]), 300)
                if detail:
                    notes.append(detail)
                continue
            state = str(event["state"])
            if state == "started":
                arguments = _summarize_json(str(event["input_json"]), 300)
                tool_lines.append(
                    f"{str(event['tool_name'])} START {arguments}"
                )
            else:
                result = _summarize_json(str(event["result_json"]), 500)
                tool_lines.append(
                    f"{str(event['tool_name'])} {state.upper()} {result}"
                )
        objective = _first_line(str(turn_row["objective"]), 500)
        final_text = _first_line(str(turn_row["final_text"]), 700)
        skeleton_lines = [
            f"t#{int(turn_row['turn_ordinal'])} {str(turn_row['status']).upper()}: {objective}"
        ]
        skeleton_lines.extend(f"- {line}" for line in tool_lines[:80])
        if final_text:
            skeleton_lines.append(f"- final: {final_text}")
        approach = "\n".join(f"- note: {note}" for note in notes[:12])
        cursor.execute(
            """
            INSERT INTO turn_digests (
                turn_id, skeleton, approach, created_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(turn_id) DO UPDATE SET
                skeleton = excluded.skeleton,
                approach = excluded.approach,
                created_at = excluded.created_at
            """,
            (
                int(turn_row["turn_id"]),
                "\n".join(skeleton_lines)[:12000],
                approach[:4000],
                now,
            ),
        )

    def _fork_chain(
        self,
        scope: ConversationScope,
        target: TurnRecord,
        *,
        limit: int,
    ) -> list[TurnRecord]:
        chain = [target]
        seen = {target.turn_id}
        current = target
        for _index in range(max(int(limit) - 1, 0)):
            with self._lock:
                row = self._connection.execute(
                    """
                    SELECT parent.*
                    FROM turn_edges AS edge
                    JOIN agent_turns AS parent
                      ON parent.turn_id = edge.to_turn_id
                    JOIN turn_visibility AS visibility
                      ON visibility.scope_key = parent.scope_key
                    WHERE edge.from_turn_id = ? AND edge.kind = 'fork-from'
                      AND parent.scope_key = ?
                      AND parent.turn_ordinal >= visibility.min_turn_ordinal
                    ORDER BY edge.edge_id ASC
                    LIMIT 1
                    """,
                    (current.turn_id, scope.key),
                ).fetchone()
            if row is None:
                break
            parent = self._row_to_turn(row)
            if parent.turn_id in seen:
                break
            chain.append(parent)
            seen.add(parent.turn_id)
            current = parent
        return chain

    def _replay_invalid_reason(
        self,
        turn: TurnRecord,
        *,
        current_model: str,
        current_provider: str,
        current_profile: str,
        prompt_version: str,
        tool_catalog_version: str,
    ) -> str:
        if turn.status != "succeeded":
            return "turn-not-succeeded"
        if turn.tool_call_count <= 0:
            return "chat-only"
        if current_provider and turn.provider != current_provider:
            return "provider-changed"
        if current_profile and turn.profile != current_profile:
            return "profile-changed"
        if turn.model != current_model:
            return "model-changed"
        if not turn.prompt_version or turn.prompt_version != prompt_version:
            return "prompt-version-changed"
        if (
            not turn.tool_catalog_version
            or turn.tool_catalog_version != tool_catalog_version
        ):
            return "tool-catalog-changed"
        if self.archive_payload(turn.turn_id) is None:
            return "archive-missing"
        return ""

    def _covered_message_ids(
        self,
        turn_ids: list[int],
    ) -> tuple[int, ...]:
        if not turn_ids:
            return ()
        placeholders = ",".join("?" for _ in turn_ids)
        with self._lock:
            trigger_rows = self._connection.execute(
                f"""
                SELECT trigger_canonical_message_id FROM agent_turns
                WHERE turn_id IN ({placeholders})
                  AND trigger_canonical_message_id IS NOT NULL
                """,
                turn_ids,
            ).fetchall()
            send_rows = self._connection.execute(
                f"""
                SELECT canonical_message_id FROM turn_send_links
                WHERE turn_id IN ({placeholders})
                """,
                turn_ids,
            ).fetchall()
        return tuple(
            sorted(
                {
                    *(int(row[0]) for row in trigger_rows),
                    *(int(row[0]) for row in send_rows),
                }
            )
        )

    def _cleanup_archives(
        self,
        cursor: StoreCursor,
        scope_key: str,
        now: int,
    ) -> None:
        cursor.execute("DELETE FROM turn_archives WHERE expires_at <= ?", (now,))
        cursor.execute(
            """
            DELETE FROM turn_archives
            WHERE turn_id IN (
                SELECT a.turn_id
                FROM turn_archives AS a
                JOIN agent_turns AS t ON t.turn_id = a.turn_id
                WHERE t.scope_key = ?
                ORDER BY a.created_at DESC, a.turn_id DESC
                OFFSET ?
            )
            """,
            (scope_key, self.archive_max_per_scope),
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

    @staticmethod
    def _ensure_visibility(cursor: StoreCursor, scope_key: str) -> None:
        cursor.execute(
            """
            INSERT INTO turn_visibility(scope_key, min_turn_ordinal)
            VALUES (?, 1)
            ON CONFLICT (scope_key) DO NOTHING
            """,
            (scope_key,),
        )

    @staticmethod
    def _turn_row(
        cursor: StoreCursor,
        turn_id: int,
    ) -> StoreRow | None:
        return cursor.execute(
            "SELECT * FROM agent_turns WHERE turn_id = ?",
            (int(turn_id),),
        ).fetchone()

    @staticmethod
    def _row_to_turn(row: StoreRow) -> TurnRecord:
        return TurnRecord(
            turn_id=int(row["turn_id"]),
            scope_key=str(row["scope_key"]),
            turn_ordinal=int(row["turn_ordinal"]),
            trigger_canonical_message_id=(
                int(row["trigger_canonical_message_id"])
                if row["trigger_canonical_message_id"] is not None
                else None
            ),
            status=str(row["status"]),
            provider=str(row["provider"]),
            model=str(row["model"]),
            profile=str(row["profile"]),
            objective=str(row["objective"]),
            final_text=str(row["final_text"]),
            started_at=int(row["started_at"]),
            finished_at=(
                int(row["finished_at"]) if row["finished_at"] is not None else None
            ),
            tool_call_count=int(row["tool_call_count"]),
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            total_tokens=int(row["total_tokens"]),
            prompt_version=str(row["prompt_version"]),
            tool_catalog_version=str(row["tool_catalog_version"]),
        )

    @staticmethod
    def _row_to_event(row: StoreRow) -> TurnEventRecord:
        try:
            raw_labels = json.loads(str(row["effect_labels_json"]))
        except json.JSONDecodeError:
            raw_labels = []
        labels = tuple(
            str(label) for label in raw_labels if isinstance(label, str)
        )
        return TurnEventRecord(
            event_id=int(row["event_id"]),
            turn_id=int(row["turn_id"]),
            loop_sequence=int(row["loop_sequence"]),
            node_id=str(row["node_id"]),
            event_kind=str(row["event_kind"]),
            state=str(row["state"]),
            tool_name=str(row["tool_name"]),
            input_json=str(row["input_json"]),
            result_json=str(row["result_json"]),
            detail=str(row["detail"]),
            effect_labels=labels,
            occurred_at=int(row["occurred_at"]),
        )

    @staticmethod
    def _render_standing_line(
        turn: TurnRecord,
        *,
        principal_id: int | None = None,
    ) -> str:
        stamp = datetime.fromtimestamp(turn.started_at).strftime("%H:%M")
        status = {
            "succeeded": "OK",
            "silence": "SILENT",
            "aborted": "ABORTED",
            "crashed": "CRASHED",
        }.get(turn.status, turn.status.upper())
        summary = _first_line(turn.objective, 80)
        final = _first_line(turn.final_text, 80)
        suffix = f" -> {final}" if final else ""
        actor = (
            f" [发起人 mention#{principal_id}]"
            if principal_id is not None
            else " [发起人未知]"
        )
        return (
            f"{turn.handle} {stamp} {status}{actor} \"{summary}\""
            f" · {turn.tool_call_count} tools{suffix}"
        )


def tool_catalog_fingerprint(tools: list[dict[str, Any]]) -> str:
    from .tool_policy import policy_manifest_for_tools

    encoded = json.dumps(
        {"tools": tools, "policies": policy_manifest_for_tools(tools)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def tool_effect_labels(tool_name: str) -> tuple[str, ...]:
    from .tool_policy import policy_for_tool

    return policy_for_tool(tool_name).side_effects


def _safe_json(value: Any, max_chars: int) -> str:
    sanitized = _sanitize_value(value)
    encoded = json.dumps(
        sanitized,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return encoded[:max_chars]


def _json_list(value: Any) -> list[Any]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _safe_result(value: str, max_chars: int) -> str:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return _safe_text(str(value), max_chars)
    return _safe_json(parsed, max_chars)


def _sanitize_value(value: Any, key: str = "", depth: int = 0) -> Any:
    if _secret_key(key):
        return "[REDACTED]"
    if depth >= 8:
        return "[TRUNCATED_DEPTH]"
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize_value(
                item,
                str(item_key),
                depth + 1,
            )
            for item_key, item in list(value.items())[:100]
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return _safe_text(value, 2000)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return _safe_text(str(value), 500)


def _safe_text(value: str, max_chars: int) -> str:
    text = " ".join(str(value).split())
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{10,}\b", "[REDACTED]", text)
    text = re.sub(
        r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}",
        "Bearer [REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)((?:api[_ -]?key|access[_ -]?token|auth[_ -]?token|"
        r"authorization|cookie|password|secret|密码|验证码)"
        r"\s*[:=：]\s*)(?:bearer\s+)?\S+",
        r"\1[REDACTED]",
        text,
    )
    return text[: max(int(max_chars), 1)]


def _secret_key(key: str) -> bool:
    folded = key.casefold().replace("-", "_").replace(" ", "_")
    compact = folded.replace("_", "")
    if any(
        marker in folded
        for marker in (
            "password",
            "secret",
            "api_key",
            "authorization",
            "cookie",
            "验证码",
            "密码",
        )
    ):
        return True
    return compact == "apikey" or compact == "token" or compact.endswith("token")


def _contains_secret(value: Any, depth: int = 0) -> bool:
    if depth >= 12:
        return True
    if isinstance(value, dict):
        for key, item in value.items():
            if _secret_key(str(key)) and item is not None and item != "":
                return True
            if _contains_secret(item, depth + 1):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_secret(item, depth + 1) for item in value)
    if not isinstance(value, str):
        return False
    if re.search(r"\bsk-[A-Za-z0-9_-]{10,}\b", value):
        return True
    if re.search(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}", value):
        return True
    if re.search(
        r"(?i)(?:api[_ -]?key|access[_ -]?token|auth[_ -]?token|"
        r"authorization|cookie|password|secret|密码|验证码)"
        r"\s*[:=：]\s*(?:bearer\s+)?\S+",
        value,
    ):
        return True
    if value.lstrip().startswith(("{", "[")):
        try:
            nested = json.loads(value)
        except json.JSONDecodeError:
            return False
        if nested != value:
            return _contains_secret(nested, depth + 1)
    return False


def _summarize_json(value: str, max_chars: int) -> str:
    if not value:
        return "{}"
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return _first_line(value, max_chars)
    if isinstance(parsed, dict):
        parts = []
        for key, item in list(parsed.items())[:12]:
            if isinstance(item, (dict, list)):
                rendered = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            else:
                rendered = str(item)
            parts.append(f"{key}={_first_line(rendered, 160)}")
        return _first_line(", ".join(parts), max_chars)
    return _first_line(str(parsed), max_chars)


def _first_line(value: str, max_chars: int) -> str:
    return " ".join(str(value).split())[:max_chars]


def _status_label(status: str) -> str:
    return {
        "running": "运行中",
        "succeeded": "已完成",
        "silence": "无可见回复",
        "aborted": "已取消",
        "crashed": "异常中断",
    }.get(status, status)


def _decode_trace_archive(
    encoded: Any,
    expires_at: int,
    now: int,
) -> dict[str, Any]:
    if encoded is None or expires_at <= now:
        return {}
    try:
        decoded = zlib.decompress(bytes(encoded)).decode("utf-8")
        payload = json.loads(decoded)
    except (OSError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _safe_trace_id(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if re.fullmatch(r"[0-9a-f]{32}", normalized) else ""


def _safe_model_routing(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        decision = {key: _safe_text(str(raw.get(key) or ""), 200) for key in (
            "requested_profile", "requested_model", "actual_profile", "actual_model", "reason_code", "reason",
        )}
        decision["fallback"] = raw.get("fallback") is True
        outcomes = raw.get("outcomes")
        decision["outcomes"] = [
            {key: _safe_text(str(item.get(key) or ""), 200) for key in ("profile", "status", "reason_code", "reason")}
            for item in outcomes[:32] if isinstance(item, dict)
        ] if isinstance(outcomes, list) else []
        result.append(decision)
    return result


def _safe_model_routes(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    routes: list[dict[str, str]] = []
    for raw_route in value[:8]:
        if not isinstance(raw_route, dict):
            continue
        route = {
            key: _safe_text(str(raw_route.get(key) or ""), 160)
            for key in ("provider", "profile", "model")
        }
        if any(route.values()):
            routes.append(route)
    return routes


def _replay_messages(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list):
        return []
    messages: list[dict[str, Any]] = []
    for raw in raw_messages:
        if not isinstance(raw, dict):
            return []
        role = raw.get("role")
        if role == "system":
            continue
        if role == "user":
            messages.append(
                {"role": "user", "content": raw.get("content") or ""}
            )
            continue
        if role == "assistant":
            message: dict[str, Any] = {
                "role": "assistant",
                "content": raw.get("content") or "",
            }
            tool_calls = raw.get("tool_calls")
            if tool_calls is not None:
                if not isinstance(tool_calls, list):
                    return []
                message["tool_calls"] = tool_calls
                reasoning = raw.get("reasoning_content")
                if reasoning is not None:
                    message["reasoning_content"] = reasoning
            messages.append(message)
            continue
        if role == "tool":
            tool_call_id = raw.get("tool_call_id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                return []
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": raw.get("content") or "",
                }
            )
            continue
        return []
    if not messages or messages[0].get("role") != "user":
        return []
    return messages
