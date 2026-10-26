"""Versioned task controls and a durable fence for external deliveries."""
from __future__ import annotations

import json
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class JobFence:
    job_id: int
    owner: str
    attempt: int
    check: Callable[[], bool]

    def assert_owned(self) -> None:
        if not self.check():
            raise LeaseLost("Sub-Agent worker no longer owns its task lease")


class LeaseLost(BaseException):
    """Must escape ordinary model/tool retry handlers."""


active_job_fence: ContextVar[JobFence | None] = ContextVar("subagent_job_fence", default=None)
active_task_id: ContextVar[int | None] = ContextVar("subagent_task_id", default=None)
active_model_policy: ContextVar[dict[str, Any]] = ContextVar("subagent_model_policy", default={})


def assert_job_owned() -> None:
    fence = active_job_fence.get()
    if fence:
        fence.assert_owned()


CONTROL_SQL = """
CREATE TABLE IF NOT EXISTS subagent_controls (
    task_id INTEGER PRIMARY KEY REFERENCES subagent_tasks(task_id) ON DELETE CASCADE,
    version INTEGER NOT NULL, revision INTEGER NOT NULL,
    policy_json TEXT NOT NULL, dispatch_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL, final_queued_revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS subagent_deliveries (
    task_id INTEGER NOT NULL REFERENCES subagent_tasks(task_id) ON DELETE CASCADE,
    revision INTEGER NOT NULL, delivery_key TEXT NOT NULL,
    state TEXT NOT NULL, payload_json TEXT NOT NULL, updated_at INTEGER NOT NULL,
    PRIMARY KEY (task_id, revision, delivery_key)
);
"""


class TaskControlStoreMixin:
    def revision_checkpoints(self, task_id: int) -> list[dict]:
        # Revision ancestry must not be truncated by the console's checkpoint page limit.
        with self._lock:
            rows = self._connection.execute("""SELECT sequence, state_json FROM subagent_checkpoints
                WHERE task_id=? AND phase='revision_requested' ORDER BY sequence""", (task_id,)).fetchall()
        return [{"sequence": int(row["sequence"]), "state": json.loads(row["state_json"])} for row in rows]

    def current_revision_adaptive_repair_count(self, task_id: int) -> int:
        # Revision changes and their checkpoint boundary are persisted atomically.
        with self._lock:
            row = self._connection.execute("""SELECT COUNT(*) AS repair_count
                FROM subagent_checkpoints WHERE task_id=? AND phase='adaptive_repair_planned'
                AND sequence > COALESCE((SELECT MAX(sequence) FROM subagent_checkpoints
                    WHERE task_id=? AND phase='revision_requested'), 0)""", (task_id, task_id)).fetchone()
        return int(row["repair_count"])

    def latest_run_checkpoint(self, task_id: int, run_id: int, phase: str) -> dict[str, Any] | None:
        """Return the latest checkpoint state for this run/phase in the current revision."""
        with self._lock:
            row = self._connection.execute("""SELECT state_json FROM subagent_checkpoints
                WHERE task_id=? AND run_id=? AND phase=?
                AND sequence > COALESCE((SELECT MAX(sequence) FROM subagent_checkpoints
                    WHERE task_id=? AND phase='revision_requested'), 0)
                ORDER BY sequence DESC LIMIT 1""", (task_id, run_id, phase, task_id)).fetchone()
        return json.loads(row["state_json"]) if row is not None else None

    def run_resume_safe(self, run_id: int) -> bool:
        with self._lock:
            revisions = self._connection.execute("""SELECT e.sequence, e.payload_json, r.step_key
                FROM subagent_events e JOIN subagent_runs r ON r.task_id=e.task_id
                WHERE r.run_id=? AND e.event_type='task.revised' ORDER BY e.sequence DESC""", (run_id,)).fetchall()
            # An explicit revision is a new execution attempt for selected steps only.
            boundary = next((int(row["sequence"]) for row in revisions
                if row["step_key"] in json.loads(row["payload_json"]).get("steps", [])), 0)
            events = self._connection.execute("SELECT sequence, event_type, payload_json FROM subagent_events WHERE run_id=? AND sequence>? AND event_type IN ('agent.tool_started','agent.tool_finished') ORDER BY sequence", (run_id, boundary)).fetchall()
            session = self._connection.execute("SELECT transcript_json, covered_sequence FROM subagent_sessions WHERE run_id=?", (run_id,)).fetchone()
            durable = self._connection.execute("""SELECT e.call_id, e.request_json, e.response_json FROM subagent_external_calls e
                JOIN subagent_controls c ON c.task_id=e.task_id AND c.revision=e.revision
                WHERE e.run_id=? AND e.status='resolved'""", (run_id,)).fetchall()
        reconciled = {row["call_id"]: json.loads(row["request_json"]) for row in durable}
        for row in durable:
            response = json.loads(row["response_json"])
            record = response.get("operation") if isinstance(response.get("operation"), dict) else response
            if record.get("status") == "needs_attention":
                return False
        acknowledged = {m.get("tool_call_id") for m in json.loads(session["transcript_json"]) if m.get("role") == "tool"} if session else set()
        pending = {}
        for event in events:
            value = json.loads(event["payload_json"])
            known = reconciled.get(value.get("call_id"))
            if known and known.get("tool_name") == value.get("tool_name") and (
                event["event_type"] == "agent.tool_finished" or known.get("tool_arguments") == value.get("arguments")):
                continue
            if value.get("idempotency") in {"pure", "idempotent"}:
                continue
            if not session or int(event["sequence"]) > int(session["covered_sequence"]):
                return False
            call_id = value.get("call_id")
            if not call_id:
                return False
            if event["event_type"] == "agent.tool_started":
                pending[call_id] = value
            elif value.get("state") in {"succeeded", "committed"} and call_id in acknowledged:
                pending.pop(call_id, None)
            else:
                return False
        return not pending

    def interrupt_task(self, task_id: int) -> None:
        running = [r.run_id for r in self.runs(task_id) if r.status in {"running", "interrupted", "waiting_external"}]
        with self._transaction() as cursor:
            cursor.execute("UPDATE subagent_runs SET status='interrupted' WHERE task_id=? AND status='running'", (task_id,))
            cursor.execute("UPDATE subagent_tasks SET status='interrupted', finished_at=NULL WHERE task_id=?", (task_id,))
        self.append_checkpoint(task_id, "process_interrupted", {"interrupted_runs": running, "reason": "worker_takeover"})

    def control(self, task_id: int) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute("SELECT * FROM subagent_controls WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            return {"version": 0, "revision": 1, "policy": {"mode": "auto"}, "dispatch": {}}
        return {"version": int(row["version"]), "revision": int(row["revision"]),
                "policy": json.loads(row["policy_json"]), "dispatch": json.loads(row["dispatch_json"])}

    def update_control(self, task_id: int, *, expected_version: int, policy=None, dispatch=None, revision=None) -> dict[str, Any]:
        current = self.control(task_id)
        if current["version"] != expected_version:
            raise ValueError("Task version changed; refresh before editing")
        value = {**current, "version": expected_version + 1}
        for key, update in (("policy", policy), ("dispatch", dispatch), ("revision", revision)):
            if update is not None:
                value[key] = update
        with self._transaction() as cursor:
            if policy is not None:
                lock = "" if self._legacy_sqlite else " FOR UPDATE"
                task = cursor.execute("SELECT status FROM subagent_tasks WHERE task_id=?" + lock, (task_id,)).fetchone()
                if task is None or task["status"] in {"running", "planning", "verifying", "cancelling", "revising", "waiting_external"}:
                    raise ValueError("Task started before the model update; wait until it stops")
            if expected_version == 0:
                cursor.execute("""INSERT INTO subagent_controls
                    (task_id, version, revision, policy_json, dispatch_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(task_id) DO NOTHING""",
                    (task_id, value["version"], value["revision"], json.dumps(value["policy"]), json.dumps(value["dispatch"]), int(time.time())))
            else:
                cursor.execute("""UPDATE subagent_controls SET version=?, revision=?, policy_json=?, dispatch_json=?, updated_at=?
                    WHERE task_id=? AND version=?""",
                    (value["version"], value["revision"], json.dumps(value["policy"]), json.dumps(value["dispatch"]), int(time.time()), task_id, expected_version))
            if cursor.rowcount != 1:
                raise ValueError("Task version changed; refresh before editing")
            if expected_version == 0 and dispatch:
                cursor.execute("UPDATE subagent_tasks SET status='queued' WHERE task_id=? AND status='received'", (task_id,))
        self._notify_changed(task_id)
        return value

    def begin_delivery(self, task_id: int, key: str, payload: dict[str, Any]) -> bool:
        revision = self.control(task_id)["revision"]
        with self._transaction() as cursor:
            cursor.execute("""INSERT INTO subagent_deliveries
                (task_id, revision, delivery_key, state, payload_json, updated_at)
                VALUES (?, ?, ?, 'sending', ?, ?) ON CONFLICT DO NOTHING""",
                (task_id, revision, key, json.dumps(payload, ensure_ascii=False), int(time.time())))
            return cursor.rowcount == 1

    def finish_delivery(self, task_id: int, key: str, state: str, payload: dict[str, Any], *,
                        revision: int | None = None, expected_payload: dict | None = None) -> bool:
        if state not in {"acknowledged", "unknown", "rejected"}:
            raise ValueError("Invalid delivery state")
        if revision is None:
            revision = self.control(task_id)["revision"]
        with self._transaction() as cursor:
            lock = "" if self._legacy_sqlite else " FOR UPDATE"
            row = cursor.execute("""SELECT state, payload_json FROM subagent_deliveries
                WHERE task_id=? AND revision=? AND delivery_key=?""" + lock, (task_id, revision, key)).fetchone()
            if row is None or row["state"] not in {"sending", "unknown"}:
                return False
            if expected_payload is not None and json.loads(row["payload_json"]) != expected_payload:
                return False
            payload = {**payload, "state": state, "ok": state == "acknowledged"}
            cursor.execute("""UPDATE subagent_deliveries SET state=?, payload_json=?, updated_at=?
                WHERE task_id=? AND revision=? AND delivery_key=? AND state IN ('sending','unknown')
                AND payload_json=?""",
                (state, json.dumps(payload, ensure_ascii=False), int(time.time()), task_id, revision, key, row["payload_json"]))
            changed = cursor.rowcount == 1
        if changed:
            self._notify_changed(task_id)
        return changed

    def deliveries(self, task_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute("SELECT * FROM subagent_deliveries WHERE task_id=? ORDER BY revision, updated_at", (task_id,)).fetchall()
        return [{"key": row["delivery_key"], "revision": row["revision"], "state": row["state"],
                 "payload": json.loads(row["payload_json"]), "updated_at": row["updated_at"]} for row in rows]

    def dispatchable_tasks(self) -> list[int]:
        with self._lock:
            rows = self._connection.execute("""SELECT t.task_id FROM subagent_tasks t JOIN subagent_controls c ON t.task_id=c.task_id
                WHERE t.status IN ('queued', 'interrupted', 'waiting_external') AND t.cancel_requested=?
                ORDER BY t.created_at LIMIT 100""", (False,)).fetchall()
        return [int(row["task_id"]) for row in rows]

    def finalizable_tasks(self) -> list[int]:
        with self._lock:
            rows = self._connection.execute("""SELECT t.task_id FROM subagent_tasks t
                JOIN subagent_controls c ON t.task_id=c.task_id
                WHERE t.status IN ('completed','partial','failed','cancelled')
                  AND c.dispatch_json <> '{}' AND c.final_queued_revision <> c.revision
                ORDER BY t.finished_at, t.task_id LIMIT 100""").fetchall()
        return [int(row["task_id"]) for row in rows]

    def mark_final_queued(self, task_id, revision):
        with self._transaction() as cursor:
            cursor.execute("UPDATE subagent_controls SET final_queued_revision=? WHERE task_id=? AND revision=?",
                (revision, task_id, revision))

    def uncertain_deliveries(self) -> list[int]:
        with self._lock:
            rows = self._connection.execute("SELECT DISTINCT task_id FROM subagent_deliveries WHERE state IN ('unknown','sending') AND updated_at<? LIMIT 10", (int(time.time()) - 60,)).fetchall()
        return [int(row["task_id"]) for row in rows]

    def unsettled_file_receipts(self) -> list[int]:
        with self._lock:
            rows = self._connection.execute("""SELECT t.task_id FROM subagent_tasks t
                JOIN subagent_controls c ON c.task_id=t.task_id
                WHERE t.status IN ('completed','partial','failed','cancelled') AND c.dispatch_json <> '{}'
                  AND EXISTS (SELECT 1 FROM subagent_deliveries d WHERE d.task_id=t.task_id
                    AND d.revision=c.revision AND d.state='acknowledged')
                  AND NOT EXISTS (SELECT 1 FROM subagent_deliveries d WHERE d.task_id=t.task_id
                    AND d.revision=c.revision AND d.state <> 'acknowledged')
                  AND NOT EXISTS (SELECT 1 FROM subagent_checkpoints p WHERE p.task_id=t.task_id
                    AND p.phase='artifact_receipts_settled:' || CAST(c.revision AS TEXT))
                ORDER BY t.updated_at LIMIT 100""").fetchall()
        return [int(row["task_id"]) for row in rows]

    def sync_file_receipt_summary(self, task_id: int, revision: int) -> dict[str, Any] | None:
        from .outcomes import acceptance_blocks_completion, outcome_report

        with self._transaction() as cursor:
            lock = "" if self._legacy_sqlite else " FOR UPDATE"
            row = cursor.execute("""SELECT t.* FROM subagent_tasks t JOIN subagent_controls c ON c.task_id=t.task_id
                WHERE t.task_id=? AND c.revision=?
                AND t.status IN ('completed','partial','failed','cancelled')""" + lock, (task_id, revision)).fetchone()
            if row is None:
                return None
            deliveries = cursor.execute("SELECT * FROM subagent_deliveries WHERE task_id=? AND revision=? ORDER BY delivery_key",
                (task_id, revision)).fetchall()
            if not deliveries or any(d["state"] != "acknowledged" for d in deliveries):
                return None
            result = json.loads(row["result_json"])
            receipts = []
            for delivery in deliveries:
                payload = json.loads(delivery["payload_json"])
                receipt = payload.get("receipt") or {}
                payload.update(ok=True, state="acknowledged", error="", receipt={
                    **receipt, "ok": True, "error": "",
                    "file_id": payload.get("file_id") or receipt.get("file_id")})
                cursor.execute("""UPDATE subagent_deliveries SET payload_json=?
                    WHERE task_id=? AND revision=? AND delivery_key=?""",
                    (json.dumps(payload, ensure_ascii=False), task_id, revision, delivery["delivery_key"]))
                receipts.append(payload)
            by_file = {(d.get("filename"), d.get("handle")): d for d in receipts}
            previous = result.get("deliveries", [])
            updated = [by_file.get((item.get("filename"), item.get("handle")), item) for item in previous]
            all_confirmed = bool(updated) and all(item.get("ok") is True for item in updated)
            result["deliveries"] = updated
            result["delivery_state"] = "acknowledged" if all_confirmed else "failed_or_unknown"
            notice_needed = result.get("file_receipts", {}).get("notice_needed", False) or any(
                old.get("ok") is not True and new.get("ok") is True for old, new in zip(previous, updated))
            result["file_receipts"] = {"revision": revision, "confirmed": receipts,
                "notice_needed": bool(notice_needed)}
            status = row["status"]
            acceptance = result.get("validation", {}).get("acceptance")
            if isinstance(acceptance, dict) and acceptance.get("task_outcome"):
                result["answer"] = outcome_report(acceptance, str(result.get("report_narrative") or ""), updated)
            if (status == "partial" and all_confirmed and result.get("execution_state") == "succeeded"
                    and not any(item.get("draft") for item in updated)
                    and isinstance(acceptance, dict) and acceptance.get("status") == "passed"
                    and not acceptance_blocks_completion(acceptance)):
                status = "completed"
            cursor.execute("UPDATE subagent_tasks SET result_json=?, status=?, updated_at=? WHERE task_id=?",
                (json.dumps(result, ensure_ascii=False), status, int(time.time()), task_id))
        self._notify_changed(task_id)
        return result["file_receipts"]
