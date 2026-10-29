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
    lease_generation: int | None = None
    transaction_guard: Callable[[Any], None] | None = None

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


class TaskControlStoreMixin:
    def reconcile_routine_parents(self, *, limit: int = 50) -> int:
        """Settle abandoned children without inventing remote cancellation receipts."""
        timestamp = int(time.time())
        changed = []
        with self._transaction() as cursor:
            rows = cursor.execute("""SELECT t.task_id,r.status AS parent_status FROM routine_children c
                JOIN routine_runs r ON r.run_id=c.run_id JOIN subagent_tasks t ON t.task_id=c.task_id
                WHERE r.status IN ('failed','skipped','cancelled','enqueued','quiet')
                  AND t.status NOT IN ('completed','partial','failed','cancelled')
                ORDER BY r.run_id,t.task_id LIMIT ? FOR UPDATE OF r,t SKIP LOCKED""",
                (min(max(limit, 1), 100),)).fetchall()
            for row in rows:
                task_id = row["task_id"]
                cancelled = row["parent_status"] == "cancelled"
                status = "cancelled" if cancelled else "failed"
                error = "所属例行任务已取消" if cancelled else "所属例行任务已结束，子任务尚未完成"
                error += "；已提交的远程操作需另行核对，未删除产物或伪造取消回执。"
                cursor.execute("""UPDATE subagent_runs SET status=CASE WHEN status='pending' THEN 'skipped' ELSE ? END,
                    last_error=?,finished_at=? WHERE task_id=?
                    AND status IN ('pending','running','interrupted','waiting_external')""",
                    (status, error, timestamp, task_id))
                cursor.execute("""UPDATE subagent_tasks SET status=?,cancel_requested=?,last_error=?,
                    finished_at=?,updated_at=? WHERE task_id=?""",
                    (status, cancelled, error, timestamp, timestamp, task_id))
                self.append_checkpoint(task_id, "routine_parent_ended",
                    {"parent_status": row["parent_status"], "remote_outcome": "requires_receipt_check"},
                    now=timestamp, transaction_cursor=cursor)
                self.append_event(task_id, f"task.{status}", {"error": error},
                    now=timestamp, transaction_cursor=cursor)
                changed.append(task_id)
        for task_id in changed:
            self._notify_changed(task_id)
        return len(changed)

    def routine_parent(self, task_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute("""SELECT c.run_id, r.job_id, r.attempt_id, r.status,
                r.deadline_at FROM routine_children c JOIN routine_runs r ON r.run_id=c.run_id
                WHERE c.task_id=?""", (task_id,)).fetchone()
        return dict(row) if row is not None else None

    def require_routine_owner(self, task_id: int) -> None:
        parent = self.routine_parent(task_id)
        if parent is None:
            return
        fence = active_job_fence.get()
        if fence is None or fence.job_id != parent["job_id"]:
            raise PermissionError("This Sub-Agent belongs to a scheduled occurrence, not a standalone job")
        fence.assert_owned()
        with self._lock:
            owned = self._connection.execute("""SELECT 1 FROM routine_children c
                JOIN routine_runs r ON r.run_id=c.run_id JOIN agent_routines a ON a.routine_id=r.routine_id
                JOIN durable_jobs j ON j.job_id=r.job_id
                WHERE c.task_id=? AND r.status='running' AND r.deadline_at>?
                  AND a.enabled AND a.version=r.version AND j.job_id=?
                  AND j.status='running' AND j.lease_owner=? AND j.attempts=? AND j.lease_until>?""",
                (task_id, time.time(), fence.job_id, fence.owner, fence.attempt, time.time())).fetchone()
        if owned is None:
            raise LeaseLost("Routine child lost its owning execution lease")

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
        with self._transaction() as cursor:
            cursor.execute("""UPDATE subagent_tasks SET status='interrupted', finished_at=NULL WHERE task_id=?
                AND status NOT IN ('completed','partial','failed','cancelled')""", (task_id,))
            if cursor.rowcount != 1:
                return
            running = [row["run_id"] for row in cursor.execute("""SELECT run_id FROM subagent_runs
                WHERE task_id=? AND status IN ('running','interrupted','waiting_external')""", (task_id,)).fetchall()]
            cursor.execute("UPDATE subagent_runs SET status='interrupted' WHERE task_id=? AND status='running'", (task_id,))
            self.append_checkpoint(task_id, "process_interrupted", {"interrupted_runs": running, "reason": "worker_takeover"},
                                   transaction_cursor=cursor)

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
                lock = " FOR UPDATE"
                task = cursor.execute(
                    "SELECT status FROM subagent_tasks WHERE task_id=?" + lock,
                    (task_id,),
                ).fetchone()
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

    @staticmethod
    def _require_legacy_file_task(cursor, task_id):
        row = cursor.execute("SELECT account_key FROM subagent_tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None or row["account_key"] is not None:
            raise ValueError("legacy_file_route_unavailable")

    def artifact_delivery_request(self, task, run_id, artifact, filename, *, draft, draft_reason):
        from .artifact_delivery import ArtifactDeliveryRequest, ArtifactDeliveryError, canonical_artifact_filename
        assert_job_owned()
        with self._transaction() as cursor:
            current = cursor.execute("""SELECT t.*, c.revision FROM subagent_tasks t
                JOIN subagent_controls c ON c.task_id=t.task_id WHERE t.task_id=?""", (task.task_id,)).fetchone()
            row = cursor.execute("""SELECT a.metadata_json FROM subagent_artifacts a
                JOIN subagent_runs r ON r.run_id=a.run_id AND r.task_id=a.task_id
                WHERE a.task_id=? AND a.run_id=? AND a.handle=?
                AND r.status IN ('succeeded','partial')""", (task.task_id, run_id,
                f"snapshot:{artifact.get('snapshot')}:{artifact.get('handle')}")).fetchone()
            if (current is None or row is None or current["cancel_requested"]
                    or current["status"] not in {"running", "verifying", "planning"}
                    or current["endpoint_key"] != task.endpoint.key
                    or current["requester_actor_key"] != task.requester_actor.key):
                raise ArtifactDeliveryError("artifact_source_unavailable")
            recorded = json.loads(row["metadata_json"])
            if any(recorded.get(key) != artifact.get(key) for key in ("snapshot", "size", "name", "handle")):
                raise ArtifactDeliveryError("artifact_capture_mismatch")
            return ArtifactDeliveryRequest(task.task_id, run_id, int(current["revision"]),
                task.endpoint, task.requester_actor, recorded.get("snapshot"), recorded.get("size"),
                canonical_artifact_filename(filename, draft=draft), draft,
                draft_reason.encode("utf-8")[:1000].decode("utf-8", "ignore"))

    def projected_result(self, task_id):
        from .artifact_delivery import official_task, delivery_state
        task = self.get(task_id)
        if task is None:
            return None
        if not official_task(task):
            return task.result
        result = dict(task.result)
        result["deliveries"] = [row["payload"] for row in self.deliveries(task_id)]
        result["delivery_state"] = delivery_state(result["deliveries"],
            required=bool(task.plan.get("contract", {}).get("delivery_required")))
        if result["deliveries"]:
            from .outcomes import outcome_report
            validation = result.get("validation")
            acceptance = validation.get("acceptance") if isinstance(validation, dict) else None
            narrative = result.get("report_narrative")
            result["answer"] = outcome_report(acceptance if isinstance(acceptance, dict) else {},
                narrative if isinstance(narrative, str) else "", result["deliveries"])
        return result

    def begin_delivery(self, task_id: int, key: str, payload: dict[str, Any]) -> bool:
        revision = self.control(task_id)["revision"]
        with self._transaction() as cursor:
            self._require_legacy_file_task(cursor, task_id)
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
            self._require_legacy_file_task(cursor, task_id)
            lock = " FOR UPDATE"
            row = cursor.execute(
                """SELECT state, payload_json FROM subagent_deliveries
                WHERE task_id=? AND revision=? AND delivery_key=?"""
                + lock,
                (task_id, revision, key),
            ).fetchone()
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
        from .artifact_delivery import official_task, project_rows
        task = self.get(task_id)
        if task is not None and official_task(task):
            projection = getattr(self, "official_file_projection", None)
            revision = self.control(task_id)["revision"]
            if projection is None:
                from ..adapters.qq_official.file_projection import project_task_files
                with self._transaction() as cursor:
                    values = project_task_files(cursor, task, revision)
            else:
                values = projection(task, revision)
            return project_rows(task, revision, values)
        with self._lock:
            rows = self._connection.execute("SELECT d.* FROM subagent_deliveries d JOIN subagent_tasks t ON t.task_id=d.task_id WHERE d.task_id=? AND t.account_key IS NULL ORDER BY d.revision, d.updated_at", (task_id,)).fetchall()
        return [{"key": row["delivery_key"], "revision": row["revision"], "state": row["state"],
                 "payload": json.loads(row["payload_json"]), "updated_at": row["updated_at"]} for row in rows]

    def dispatchable_tasks(self) -> list[int]:
        with self._lock:
            rows = self._connection.execute("""SELECT t.task_id FROM subagent_tasks t JOIN subagent_controls c ON t.task_id=c.task_id
                WHERE t.account_key IS NULL AND t.status IN ('queued', 'interrupted', 'waiting_external') AND t.cancel_requested=?
                AND NOT EXISTS (SELECT 1 FROM routine_children r WHERE r.task_id=t.task_id)
                ORDER BY t.created_at LIMIT 100""", (False,)).fetchall()
        return [int(row["task_id"]) for row in rows]

    def finalizable_tasks(self) -> list[int]:
        with self._lock:
            rows = self._connection.execute("""SELECT t.task_id FROM subagent_tasks t
                JOIN subagent_controls c ON t.task_id=c.task_id
                WHERE t.account_key IS NULL AND t.status IN ('completed','partial','failed','cancelled')
                  AND c.dispatch_json <> '{}' AND c.final_queued_revision <> c.revision
                  AND NOT EXISTS (SELECT 1 FROM routine_children r WHERE r.task_id=t.task_id)
                ORDER BY t.finished_at, t.task_id LIMIT 100""").fetchall()
        return [int(row["task_id"]) for row in rows]

    def mark_final_queued(self, task_id, revision, *, transaction_cursor=None):
        from contextlib import nullcontext
        with (nullcontext(transaction_cursor) if transaction_cursor is not None else self._transaction()) as cursor:
            cursor.execute("UPDATE subagent_controls SET final_queued_revision=? WHERE task_id=? AND revision=?",
                (revision, task_id, revision))

    def uncertain_deliveries(self) -> list[int]:
        with self._lock:
            rows = self._connection.execute("SELECT DISTINCT d.task_id FROM subagent_deliveries d JOIN subagent_tasks t ON t.task_id=d.task_id WHERE t.account_key IS NULL AND d.state IN ('unknown','sending') AND d.updated_at<? LIMIT 10", (int(time.time()) - 60,)).fetchall()
        return [int(row["task_id"]) for row in rows]

    def unsettled_file_receipts(self) -> list[int]:
        with self._lock:
            rows = self._connection.execute("""SELECT t.task_id FROM subagent_tasks t
                JOIN subagent_controls c ON c.task_id=t.task_id
                WHERE t.account_key IS NULL AND t.status IN ('completed','partial','failed','cancelled') AND c.dispatch_json <> '{}'
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
            lock = " FOR UPDATE"
            row = cursor.execute(
                """SELECT t.* FROM subagent_tasks t JOIN subagent_controls c ON c.task_id=t.task_id
                WHERE t.task_id=? AND c.revision=? AND t.account_key IS NULL
                AND t.status IN ('completed','partial','failed','cancelled')"""
                + lock,
                (task_id, revision),
            ).fetchone()
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
