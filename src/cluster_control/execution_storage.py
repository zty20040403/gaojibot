from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Callable

from src.bot_storage import PostgresDatabase

from .execution_contracts import canonical_json, content_hash, new_handle, safe_artifact_name
from .scheduling import ResourceRequest, eligibility_reason, settle_reported_cost


def _decode(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return fallback


class ClusterExecutionStore:
    """Authoritative P3/P4 state. Remote effects happen only after commit."""

    def __init__(self, database: PostgresDatabase, artifact_root: Path) -> None:
        self.database = database
        self.artifact_root = artifact_root
        self.artifact_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @staticmethod
    def _event(cursor: Any, table: str, key_name: str, key: str, **values: Any) -> None:
        row = cursor.execute(
            f"SELECT COALESCE(MAX(sequence), 0) + 1 FROM {table} WHERE {key_name} = ?",
            (key,),
        ).fetchone()
        sequence = int(row[0]) if row else 1
        if table == "fleet_operation_events":
            cursor.execute(
                """INSERT INTO fleet_operation_events
                   (operation_id, sequence, event_type, status, actor_id, fence,
                    payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    key, sequence, values["event_type"], values["status"],
                    values.get("actor_id", "system"), values.get("fence", 0),
                    canonical_json(values.get("payload", {})), values["created_at"],
                ),
            )
        else:
            cursor.execute(
                """INSERT INTO fleet_job_events
                   (job_id, sequence, event_type, status, worker_id, fence,
                    payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    key, sequence, values["event_type"], values["status"],
                    values.get("worker_id", ""), values.get("fence", 0),
                    canonical_json(values.get("payload", {})), values["created_at"],
                ),
            )

    def prepare_operation(self, record: dict[str, Any]) -> dict[str, Any]:
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            # Serialize equal intents before checking the unique idempotency key.
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(?, 0))", (
                canonical_json([record["actor_id"], record["origin_scope"], record["idempotency_key"]]),
            ))
            existing = cursor.execute(
                """SELECT * FROM fleet_operations
                   WHERE actor_id = ? AND origin_scope = ? AND idempotency_key = ?""",
                (record["actor_id"], record["origin_scope"], record["idempotency_key"]),
            ).fetchone()
            if existing is not None:
                item = self._operation(dict(existing), cursor)
                if item["contract_hash"] != record["contract_hash"]:
                    raise ValueError("idempotency key already belongs to a different operation")
                return item
            cursor.execute(
                """INSERT INTO fleet_operations (
                   operation_id, task_ref, step_ref, actor_id, origin_scope, host_id,
                   resource_ref, operation, operation_version, arguments_json,
                   backend_ref, backend_binding_version, expected_state_json,
                   resource_version, policy_version, deadline_at, resource_budget_json,
                   idempotency_key, verification_json, compensation_json, contract_hash,
                   status, capability_status, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                             ?, ?, ?, ?, ?)""",
                (
                    record["operation_id"], record["task_ref"], record["step_ref"],
                    record["actor_id"], record["origin_scope"], record["host_id"],
                    record["resource_ref"], record["operation"], record["operation_version"],
                    canonical_json(record["arguments"]), record["backend_ref"],
                    record["backend_binding_version"], canonical_json(record["expected_state"]),
                    record["resource_version"], record["policy_version"], record["deadline_at"],
                    canonical_json(record["resource_budget"]), record["idempotency_key"],
                    canonical_json(record["verification"]), canonical_json(record["compensation"]),
                    record["contract_hash"], record["status"], record["capability_status"],
                    record["created_at"], record["created_at"],
                ),
            )
            self._event(
                cursor, "fleet_operation_events", "operation_id", record["operation_id"],
                event_type="prepared", status=record["status"], actor_id=record["actor_id"],
                payload={"capability_status": record["capability_status"]},
                created_at=record["created_at"],
            )
            connection.commit()
            return self.get_operation(record["operation_id"]) or {}
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def approve_operation(
        self, operation_id: str, *, actor_id: str, expected_hash: str,
        expected_version: int, expires_at: int,
        before_approve: Callable[[Any, dict[str, Any], int], None] | None = None,
    ) -> dict[str, Any]:
        now = int(time.time())
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            row = cursor.execute(
                "SELECT * FROM fleet_operations WHERE operation_id = ? FOR UPDATE",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise LookupError("operation not found")
            item = dict(row)
            if item["capability_status"] != "available":
                raise PermissionError("operation backend is not available")
            if item["status"] != "awaiting_approval":
                raise ValueError("operation is not awaiting approval")
            if item["contract_hash"] != expected_hash or int(item["resource_version"]) != expected_version:
                raise ValueError("operation changed; prepare and review it again")
            if int(item["deadline_at"]) <= now:
                raise ValueError("operation deadline expired")
            if before_approve is not None:
                before_approve(cursor, item, now)
            approval_id = new_handle("approval")
            cursor.execute(
                """INSERT INTO fleet_approvals
                   (approval_id, operation_id, actor_id, contract_hash, resource_version,
                    approved_at, expires_at, consumed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (approval_id, operation_id, actor_id, expected_hash, expected_version,
                 now, min(expires_at, int(item["deadline_at"])), None),
            )
            cursor.execute(
                """UPDATE fleet_operations SET approval_ref = ?, status = 'queued',
                   updated_at = ? WHERE operation_id = ?""",
                (approval_id, now, operation_id),
            )
            self._event(
                cursor, "fleet_operation_events", "operation_id", operation_id,
                event_type="approved", status="queued", actor_id=actor_id,
                payload={"approval_id": approval_id}, created_at=now,
            )
            connection.commit()
            return self.get_operation(operation_id) or {}
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def cancel_operation(
        self, operation_id: str, *, actor_id: str, origin_scope: str = ""
    ) -> dict[str, Any]:
        now = int(time.time())
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            row = cursor.execute(
                "SELECT status, fence FROM fleet_operations WHERE operation_id = ? FOR UPDATE",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise LookupError("operation not found")
            owner = cursor.execute(
                "SELECT actor_id, origin_scope FROM fleet_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if owner is None or (
                not actor_id.startswith("admin:")
                and (owner["actor_id"] != actor_id or owner["origin_scope"] != origin_scope)
            ):
                raise PermissionError("operation belongs to another scope")
            if row["status"] in {"succeeded", "failed", "cancelled"}:
                return self._operation_by_id(cursor, operation_id)
            status = "cancelled" if row["status"] in {"planned", "awaiting_approval", "queued"} else "cancelling"
            cursor.execute(
                "UPDATE fleet_operations SET status = ?, updated_at = ? WHERE operation_id = ?",
                (status, now, operation_id),
            )
            self._event(
                cursor, "fleet_operation_events", "operation_id", operation_id,
                event_type="cancel_requested", status=status, actor_id=actor_id,
                fence=int(row["fence"]), created_at=now,
            )
            connection.commit()
            return self.get_operation(operation_id) or {}
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def _operation_by_id(self, cursor: Any, operation_id: str) -> dict[str, Any]:
        row = cursor.execute(
            "SELECT * FROM fleet_operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        if row is None:
            raise LookupError("operation not found")
        return self._operation(dict(row), cursor)

    def _operation(self, item: dict[str, Any], cursor: Any) -> dict[str, Any]:
        for key in ("arguments_json", "expected_state_json", "resource_budget_json", "verification_json", "compensation_json", "result_json"):
            item[key.removesuffix("_json")] = _decode(item.pop(key, "{}"), {})
        events = cursor.execute(
            """SELECT sequence, event_type, status, actor_id, fence, payload_json, created_at
               FROM fleet_operation_events WHERE operation_id = ? ORDER BY sequence""",
            (item["operation_id"],),
        ).fetchall()
        item["events"] = [
            {**dict(event), "payload": _decode(dict(event).get("payload_json"), {})}
            for event in events
        ]
        for event in item["events"]:
            event.pop("payload_json", None)
        return item

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            row = cursor.execute(
                "SELECT * FROM fleet_operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            return self._operation(dict(row), cursor) if row else None
        finally:
            cursor.close()
            connection.close()

    def recent_operations(self, limit: int = 50) -> list[dict[str, Any]]:
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            rows = cursor.execute(
                "SELECT * FROM fleet_operations ORDER BY updated_at DESC LIMIT ?",
                (min(max(limit, 1), 200),),
            ).fetchall()
            return [self._operation(dict(row), cursor) for row in rows]
        finally:
            cursor.close()
            connection.close()

    def find_operation(self, actor: str, origin: str, key: str) -> dict[str, Any] | None:
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            row = cursor.execute("""SELECT * FROM fleet_operations
                WHERE actor_id=? AND origin_scope=? AND idempotency_key=?""", (actor, origin, key)).fetchone()
            return self._operation(dict(row), cursor) if row else None
        finally:
            cursor.close()
            connection.close()

    def operation_provenance(self, actor: str, origin: str, intent_key: str) -> dict[str, Any] | None:
        """Read historical authorization, without reauthorizing or asserting current health."""
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            # Approval and dispatch must come from the same read-only snapshot.
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            rows = cursor.execute("""SELECT * FROM fleet_operations WHERE backend_ref IN ('ops-management-v2','ssh-management-v1')
                AND operation IN ('maxops.execute','ssh.execute') AND idempotency_key=? AND actor_id=? AND origin_scope=? LIMIT 2""",
                (intent_key, actor, origin)).fetchall()
            if len(rows) != 1:
                return None
            item = self._operation(dict(rows[0]), cursor)
            approval = cursor.execute("""SELECT approval_id, operation_id, actor_id, contract_hash,
                resource_version, approved_at, consumed_at, expires_at FROM fleet_approvals
                WHERE approval_id=? AND operation_id=?""", (item["approval_ref"], item["operation_id"])).fetchone()
            approval = dict(approval) if approval else {}
            dispatch = next((event["created_at"] for event in item["events"] if event["event_type"] == "dispatching"), None)
            consumed = approval.get("consumed_at")
            verified = (content_hash({"actor": actor, "origin": origin, "arguments": item["arguments"]})
                == item["contract_hash"] == approval.get("contract_hash")
                and approval.get("resource_version") == item["resource_version"]
                and consumed is not None and dispatch is not None
                and item["created_at"] <= approval["approved_at"] <= consumed <= dispatch < approval["expires_at"])
            return {"source": "gaoji-control", "historical": True, "level": "authorized_dispatch",
                "authorized_before_dispatch": verified, "operation_id": item["operation_id"],
                "backend_job_id": item["backend_operation_id"], "actor_id": actor, "origin_scope": origin,
                "host_id": item["host_id"], "operation": item["arguments"].get("op"),
                "params_hash": hashlib.sha256(canonical_json(item["arguments"].get("params", {})).encode()).hexdigest(),
                "intent_key": intent_key, "contract_hash": item["contract_hash"],
                "approval": approval, "created_at": item["created_at"], "dispatched_at": dispatch,
                "recorded_status": item["status"], "status_recorded_at": item["updated_at"],
                "instruction": "Historical approval and dispatch only; not a new grant, current health check or proof of task completion."}
        finally:
            cursor.close()
            connection.close()

    def claim_managed_operation(self, owner: str) -> dict[str, Any] | None:
        now = int(time.time())
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            row = cursor.execute("""SELECT * FROM fleet_operations
                WHERE operation IN ('maxops.execute','ssh.execute') AND status IN ('queued','running','reconciling','cancelling')
                AND (lease_expires_at IS NULL OR lease_expires_at<=?)
                ORDER BY updated_at, operation_id LIMIT 1 FOR UPDATE SKIP LOCKED""", (now,)).fetchone()
            if row is None:
                return None
            item = dict(row)
            problem = ""
            if item["status"] == "queued":
                approval = cursor.execute("SELECT * FROM fleet_approvals WHERE approval_id=? FOR UPDATE",
                    (item["approval_ref"],)).fetchone()
                if not approval or approval["consumed_at"] is not None or approval["expires_at"] <= now:
                    problem = "approval_expired"
                elif approval["contract_hash"] != item["contract_hash"] or approval["resource_version"] != item["resource_version"]:
                    problem = "approval_changed"
                else:
                    cursor.execute("UPDATE fleet_approvals SET consumed_at=? WHERE approval_id=?", (now, item["approval_ref"]))
            elif not item["backend_operation_id"] and not (
                _decode(item["arguments_json"], {}).get("host_control", {}).get("version") == 1
            ):
                problem = "submission_outcome_unknown"
            if problem:
                cursor.execute("""UPDATE fleet_operations SET status='needs_attention', error_code=?,
                    lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE operation_id=?""",
                    (problem, now, item["operation_id"]))
                self._event(cursor, "fleet_operation_events", "operation_id", item["operation_id"],
                    event_type=problem, status="needs_attention", created_at=now)
                connection.commit()
                return None
            status = "cancelling" if item["status"] == "cancelling" else "running"
            cursor.execute("""UPDATE fleet_operations SET status=?, lease_owner=?, lease_expires_at=?,
                fence=fence+1, attempt=attempt+?, updated_at=? WHERE operation_id=?""",
                (status, owner, now+90, int(item["status"] == "queued"), now, item["operation_id"]))
            if item["status"] == "queued":
                self._event(cursor, "fleet_operation_events", "operation_id", item["operation_id"],
                    event_type="dispatching", status=status, fence=item["fence"]+1, created_at=now)
            connection.commit()
            return self.get_operation(item["operation_id"])
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def finish_managed_operation(self, operation_id: str, *, owner: str, fence: int,
                                 status: str, result: Any, backend_id: str | None, error: str) -> bool:
        now = int(time.time())
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            row = cursor.execute("SELECT * FROM fleet_operations WHERE operation_id=? FOR UPDATE", (operation_id,)).fetchone()
            if not row or row["lease_owner"] != owner or row["fence"] != fence or row["lease_expires_at"] <= now:
                return False
            if row["status"] == "cancelling" and status in {"running", "reconciling"}:
                status = "cancelling"
            cursor.execute("""UPDATE fleet_operations SET status=?, result_json=?, backend_operation_id=?,
                error_code=?, lease_owner=NULL, lease_expires_at=?, updated_at=? WHERE operation_id=?""",
                (status, canonical_json(result), backend_id, error, now+5, now, operation_id))
            old_phase = _decode(row["result_json"], {}).get("phase")
            phase = result.get("phase") if isinstance(result, dict) else None
            if row["status"] != status or row["backend_operation_id"] != backend_id or row["error_code"] != error or old_phase != phase:
                self._event(cursor, "fleet_operation_events", "operation_id", operation_id,
                    event_type="upstream_observed", status=status, fence=fence, created_at=now,
                    payload={"backend_operation_id": backend_id, "error_code": error, "phase": phase})
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def checkpoint_managed_operation(self, operation_id: str, *, owner: str, fence: int,
                                     result: dict[str, Any]) -> None:
        now = int(time.time())
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            row = cursor.execute("SELECT * FROM fleet_operations WHERE operation_id=? FOR UPDATE", (operation_id,)).fetchone()
            if (not row or row["lease_owner"] != owner or row["fence"] != fence
                    or row["lease_expires_at"] <= now or row["deadline_at"] <= now or row["status"] != "running"):
                raise PermissionError("Operation lease expired or was cancelled before submission")
            approval = cursor.execute("SELECT * FROM fleet_approvals WHERE approval_id=? FOR UPDATE",
                                      (row["approval_ref"],)).fetchone()
            if (not approval or approval["expires_at"] <= now or approval["consumed_at"] is None
                    or approval["contract_hash"] != row["contract_hash"]
                    or approval["resource_version"] != row["resource_version"]):
                raise PermissionError("Operation approval expired or changed before submission")
            cursor.execute("UPDATE fleet_operations SET result_json=?, updated_at=? WHERE operation_id=?",
                           (canonical_json(result), now, operation_id))
            self._event(cursor, "fleet_operation_events", "operation_id", operation_id,
                event_type="host_command_checkpoint", status="running", fence=fence,
                payload={"phase": result["phase"]}, created_at=now)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def upsert_worker(self, worker_id: str, payload: dict[str, Any], *, host_id: str) -> dict[str, Any]:
        now = int(time.time())
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                """INSERT INTO fleet_workers
                   (worker_id, host_id, boot_id, protocol_version, availability,
                    capabilities_json, runtime_json, capacity_json, public_base_url,
                    last_seen_at, resource_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(worker_id) DO UPDATE SET
                    host_id = EXCLUDED.host_id, boot_id = EXCLUDED.boot_id,
                    protocol_version = EXCLUDED.protocol_version,
                    availability = EXCLUDED.availability,
                    capabilities_json = EXCLUDED.capabilities_json,
                    runtime_json = EXCLUDED.runtime_json,
                    capacity_json = EXCLUDED.capacity_json,
                    public_base_url = EXCLUDED.public_base_url,
                    last_seen_at = EXCLUDED.last_seen_at,
                    resource_version = fleet_workers.resource_version + 1""",
                (
                    worker_id, host_id, payload["boot_id"], payload["protocol_version"],
                    payload["availability"], canonical_json(payload["capabilities"]),
                    canonical_json(payload["runtime"]), canonical_json(payload["capacity"]),
                    payload.get("public_base_url", ""), now,
                ),
            )
            connection.commit()
            return self.worker(worker_id) or {}
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    @staticmethod
    def _worker(item: dict[str, Any]) -> dict[str, Any]:
        for key, fallback in (("capabilities_json", []), ("runtime_json", {}), ("capacity_json", {})):
            item[key.removesuffix("_json")] = _decode(item.pop(key, None), fallback)
        item["fresh"] = int(time.time()) - int(item["last_seen_at"]) <= 45
        return item

    def worker(self, worker_id: str) -> dict[str, Any] | None:
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            row = cursor.execute("SELECT * FROM fleet_workers WHERE worker_id = ?", (worker_id,)).fetchone()
            return self._worker(dict(row)) if row else None
        finally:
            cursor.close()
            connection.close()

    def workers(self) -> list[dict[str, Any]]:
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            rows = cursor.execute("SELECT * FROM fleet_workers ORDER BY worker_id").fetchall()
            return [self._worker(dict(row)) for row in rows]
        finally:
            cursor.close()
            connection.close()

    def submit_job(self, record: dict[str, Any]) -> dict[str, Any]:
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            existing = cursor.execute(
                """SELECT * FROM fleet_worker_jobs
                   WHERE actor_id = ? AND origin_scope = ? AND idempotency_key = ?""",
                (record["actor_id"], record["origin_scope"], record["idempotency_key"]),
            ).fetchone()
            if existing:
                item = self._job(dict(existing), cursor)
                if (
                    item["kind"] != record["kind"]
                    or item["payload"] != record["payload"]
                    or item["constraints"] != record["constraints"]
                    or int(item["deadline_at"]) != int(record["deadline_at"])
                ):
                    raise ValueError("idempotency key already belongs to a different job")
                return item
            cursor.execute(
                """INSERT INTO fleet_worker_jobs
                   (job_id, actor_id, origin_scope, kind, payload_json, constraints_json,
                    idempotency_key, status, priority, deadline_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)""",
                (
                    record["job_id"], record["actor_id"], record["origin_scope"],
                    record["kind"], canonical_json(record["payload"]),
                    canonical_json(record["constraints"]), record["idempotency_key"],
                    int(record["constraints"].get("priority", 50)),
                    record["deadline_at"], record["created_at"], record["created_at"],
                ),
            )
            self._event(
                cursor, "fleet_job_events", "job_id", record["job_id"],
                event_type="submitted", status="queued", created_at=record["created_at"],
            )
            connection.commit()
            return self.get_job(record["job_id"]) or {}
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    @staticmethod
    def _queued_candidates(
        cursor: Any, *, now: int, worker_id: str, capabilities: set[str]
    ) -> Iterator[Any]:
        after: tuple[Any, ...] | None = None
        while True:
            boundary = ""
            parameters: list[Any] = [now, sorted(capabilities), worker_id, worker_id]
            if after is not None:
                boundary = "AND (-priority, deadline_at, created_at, job_id) > (?, ?, ?, ?)"
                parameters.extend(after)
            rows = cursor.execute(
                f"""SELECT * FROM fleet_worker_jobs
                    WHERE status = 'queued' AND deadline_at > ? AND kind = ANY(?)
                      AND COALESCE(NULLIF(constraints_json::jsonb ->> 'worker_id', ''), ?) = ?
                      {boundary}
                    ORDER BY priority DESC, deadline_at, created_at, job_id
                    FOR UPDATE SKIP LOCKED LIMIT 50""",
                parameters,
            ).fetchall()
            if not rows:
                return
            yield from rows
            last = rows[-1]
            after = (-int(last["priority"]), last["deadline_at"], last["created_at"], last["job_id"])

    def claim_job(
        self, worker_id: str, *, host: dict[str, Any] | None = None,
        lease_seconds: int = 60,
        owner_aliases: tuple[str, ...] = (),
    ) -> dict[str, Any] | None:
        now = int(time.time())
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            overdue = cursor.execute(
                """SELECT job_id, kind FROM fleet_worker_jobs
                   WHERE status = 'queued' AND deadline_at <= ? FOR UPDATE""",
                (now,),
            ).fetchall()
            for stale in overdue:
                cursor.execute(
                    """UPDATE fleet_worker_jobs SET status = 'failed',
                       error_code = 'deadline_expired', updated_at = ? WHERE job_id = ?""",
                    (now, stale["job_id"]),
                )
                if stale["kind"] == "preview.static":
                    cursor.execute(
                        """UPDATE fleet_previews SET state = 'failed',
                           health_status = 'deadline_expired', updated_at = ?
                           WHERE job_id = ?""",
                        (now, stale["job_id"]),
                    )
                self._event(
                    cursor, "fleet_job_events", "job_id", stale["job_id"],
                    event_type="deadline_expired", status="failed",
                    payload={"retryable": False}, created_at=now,
                )
            expired = cursor.execute(
                """SELECT job_id, kind, worker_id, fence, constraints_json,
                          grant_id, reserved_cost_microunits
                   FROM fleet_worker_jobs
                   WHERE status IN ('running','verifying') AND lease_expires_at <= ?
                   FOR UPDATE""",
                (now,),
            ).fetchall()
            for stale in expired:
                constraints = ResourceRequest.parse(
                    _decode(stale["constraints_json"], {})
                )
                checkpoint = cursor.execute(
                    """SELECT checkpoint_id, phase FROM fleet_job_checkpoints
                       WHERE job_id = ? ORDER BY sequence DESC LIMIT 1""",
                    (stale["job_id"],),
                ).fetchone()
                can_resume = bool(checkpoint and checkpoint["phase"] == "completed")
                can_retry = constraints.safe_rerun or can_resume
                if stale["grant_id"] and int(stale["reserved_cost_microunits"] or 0):
                    cursor.execute(
                        """UPDATE fleet_borrow_grants SET budget_reserved_microunits =
                           GREATEST(budget_reserved_microunits - ?, 0),
                           resource_version = resource_version + 1, updated_at = ?
                           WHERE grant_id = ?""",
                        (
                            int(stale["reserved_cost_microunits"]), now,
                            stale["grant_id"],
                        ),
                    )
                if stale["kind"] == "preview.static" or not can_retry:
                    cursor.execute(
                        """UPDATE fleet_worker_jobs SET status = 'failed',
                           error_code = 'outcome_unknown', lease_expires_at = NULL,
                           grant_id = NULL, reserved_cost_microunits = 0,
                           updated_at = ? WHERE job_id = ?""",
                        (now, stale["job_id"]),
                    )
                    cursor.execute(
                        """UPDATE fleet_previews SET state = 'failed',
                           health_status = 'unknown', updated_at = ? WHERE job_id = ?""",
                        (now, stale["job_id"]),
                    )
                    expired_status = "failed"
                else:
                    cursor.execute(
                        """UPDATE fleet_worker_jobs SET status = 'queued', worker_id = NULL,
                           lease_expires_at = NULL, grant_id = NULL,
                           reserved_cost_microunits = 0, resume_checkpoint_id = ?,
                           scheduler_reason = 'lease_expired_retry', updated_at = ?
                           WHERE job_id = ?""",
                        (
                            checkpoint["checkpoint_id"] if can_resume else None,
                            now, stale["job_id"],
                        ),
                    )
                    expired_status = "queued"
                cursor.execute(
                    """UPDATE fleet_reservations SET status = 'expired', released_at = ?
                       WHERE job_id = ? AND status = 'active'""",
                    (now, stale["job_id"]),
                )
                self._event(
                    cursor, "fleet_job_events", "job_id", stale["job_id"],
                    event_type="lease_expired", status=expired_status,
                    worker_id=str(stale["worker_id"] or ""), fence=int(stale["fence"]),
                    payload={"retryable": can_retry, "checkpoint": can_resume}, created_at=now,
                )
            worker = cursor.execute(
                "SELECT * FROM fleet_workers WHERE worker_id = ? FOR UPDATE", (worker_id,)
            ).fetchone()
            if worker is None or worker["availability"] != "available" or now - int(worker["last_seen_at"]) > 45:
                connection.commit()
                return None
            policy = cursor.execute(
                "SELECT * FROM fleet_worker_policies WHERE worker_id = ? FOR UPDATE",
                (worker_id,),
            ).fetchone()
            if policy is None or policy["desired_availability"] != "available":
                connection.commit()
                return None
            active_rows = cursor.execute(
                """SELECT r.cpu_millis, r.memory_bytes, r.gpu_slots, j.grant_id
                   FROM fleet_reservations r
                   JOIN fleet_worker_jobs j ON j.job_id = r.job_id
                   WHERE r.worker_id = ? AND r.status = 'active'
                     AND r.lease_expires_at > ?""",
                (worker_id, now),
            ).fetchall()
            active_totals = {"cpu_millis": 0, "memory_bytes": 0, "gpu_slots": 0}
            grant_usage: dict[str, dict[str, int]] = {}
            for reservation in active_rows:
                for field in active_totals:
                    active_totals[field] += int(reservation[field] or 0)
                grant_id = str(reservation["grant_id"] or "")
                if grant_id:
                    usage = grant_usage.setdefault(
                        grant_id,
                        {"cpu_millis": 0, "memory_bytes": 0, "gpu_slots": 0},
                    )
                    for field in usage:
                        usage[field] += int(reservation[field] or 0)
            capabilities = set(_decode(worker["capabilities_json"], []))
            capacity = _decode(worker["capacity_json"], {})
            rows = self._queued_candidates(
                cursor, now=now, worker_id=worker_id, capabilities=capabilities
            )
            chosen = None
            chosen_request: ResourceRequest | None = None
            chosen_grant = None
            host_policy = host or {"site": "", "gpu_compute": False}
            for row in rows:
                request = ResourceRequest.parse(_decode(row["constraints_json"], {}))
                external_borrow = (
                    str(row["actor_id"]) != str(policy["owner_actor_id"])
                    and str(row["actor_id"]) not in owner_aliases
                )
                grant = None
                grant_rows = cursor.execute(
                    """SELECT * FROM fleet_borrow_grants
                       WHERE worker_id = ? AND status = 'available'
                         AND valid_from <= ? AND valid_until > ?
                         AND (grantee_actor_id = ? OR grantee_actor_id = '*')
                         AND (origin_scope = ? OR origin_scope = '*')
                       ORDER BY valid_until, created_at FOR UPDATE""",
                    (worker_id, now, now, row["actor_id"], row["origin_scope"]),
                ).fetchall()
                for candidate in grant_rows:
                    reason = eligibility_reason(
                        request, worker=worker, host=host_policy, policy=policy,
                        grant=candidate, job_kind=str(row["kind"]), now=now,
                        external_borrow=external_borrow,
                        grant_usage=grant_usage.get(str(candidate["grant_id"])),
                    )
                    if not reason:
                        grant = candidate
                        break
                reason = eligibility_reason(
                    request, worker=worker, host=host_policy, policy=policy,
                    grant=grant, job_kind=str(row["kind"]), now=now,
                    external_borrow=external_borrow,
                    grant_usage=(
                        grant_usage.get(str(grant["grant_id"]))
                        if grant is not None
                        else None
                    ),
                )
                if not reason:
                    # A job that does not fit must not block smaller jobs behind it.
                    for field, limit_field in (
                        ("cpu_millis", "cpu_limit_millis"),
                        ("memory_bytes", "memory_limit_bytes"),
                        ("gpu_slots", "gpu_limit_slots"),
                    ):
                        required = active_totals[field] + getattr(request, field)
                        if required > int(capacity.get(field, 0)):
                            reason = f"worker_{field}_capacity"
                            break
                        if required > int(policy[limit_field]):
                            reason = f"owner_{field}_capacity"
                            break
                if reason:
                    cursor.execute(
                        "UPDATE fleet_worker_jobs SET scheduler_reason = ?, updated_at = ? WHERE job_id = ?",
                        (reason, now, row["job_id"]),
                    )
                    continue
                chosen = row
                chosen_request = request
                chosen_grant = grant
                break
            if chosen is None:
                connection.commit()
                return None
            assert chosen_request is not None
            cpu = chosen_request.cpu_millis
            memory = chosen_request.memory_bytes
            gpu = chosen_request.gpu_slots
            fence = int(chosen["fence"]) + 1
            lease_at = min(now + min(max(lease_seconds, 15), 300), int(chosen["deadline_at"]))
            grant_id = str(chosen_grant["grant_id"]) if chosen_grant else None
            reserved_cost = (
                chosen_request.max_cost_microunits
                if chosen_grant is not None
                else 0
            )
            if chosen_grant is not None and reserved_cost:
                cursor.execute(
                    """UPDATE fleet_borrow_grants
                       SET budget_reserved_microunits = budget_reserved_microunits + ?,
                           resource_version = resource_version + 1, updated_at = ?
                       WHERE grant_id = ?""",
                    (reserved_cost, now, grant_id),
                )
            cursor.execute(
                """UPDATE fleet_worker_jobs SET status = 'running', worker_id = ?,
                   attempt = attempt + 1, fence = ?, lease_expires_at = ?, grant_id = ?,
                   reserved_cost_microunits = ?, scheduler_reason = 'scheduled', updated_at = ?
                   WHERE job_id = ?""",
                (worker_id, fence, lease_at, grant_id, reserved_cost, now, chosen["job_id"]),
            )
            cursor.execute(
                """INSERT INTO fleet_reservations
                   (reservation_id, job_id, worker_id, cpu_millis, memory_bytes,
                    gpu_slots, status, fence, lease_expires_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                   ON CONFLICT(job_id) DO UPDATE SET
                    reservation_id = EXCLUDED.reservation_id,
                    worker_id = EXCLUDED.worker_id,
                    cpu_millis = EXCLUDED.cpu_millis,
                    memory_bytes = EXCLUDED.memory_bytes,
                    gpu_slots = EXCLUDED.gpu_slots,
                    status = 'active', fence = EXCLUDED.fence,
                    lease_expires_at = EXCLUDED.lease_expires_at,
                    created_at = EXCLUDED.created_at, released_at = NULL""",
                (new_handle("reservation"), chosen["job_id"], worker_id, cpu, memory,
                 gpu, fence, lease_at, now),
            )
            self._event(
                cursor, "fleet_job_events", "job_id", chosen["job_id"],
                event_type="claimed", status="running", worker_id=worker_id,
                fence=fence,
                payload={"lease_expires_at": lease_at, "grant_id": grant_id},
                created_at=now,
            )
            connection.commit()
            return self.get_job(chosen["job_id"])
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def renew_job(
        self, job_id: str, *, worker_id: str, fence: int, lease_seconds: int = 60
    ) -> dict[str, int | bool]:
        now = int(time.time())
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            row = cursor.execute(
                """SELECT status, worker_id, fence, deadline_at, lease_expires_at
                   FROM fleet_worker_jobs WHERE job_id = ? FOR UPDATE""",
                (job_id,),
            ).fetchone()
            if row is None:
                raise LookupError("job not found")
            if row["worker_id"] != worker_id or int(row["fence"]) != int(fence):
                raise PermissionError("stale worker lease")
            if row["status"] == "cancelling":
                connection.commit()
                return {
                    "lease_expires_at": int(row["lease_expires_at"] or now),
                    "cancel_requested": True,
                }
            if row["status"] not in {"running", "verifying"}:
                raise ValueError("job lease is no longer renewable")
            lease_at = min(now + min(max(lease_seconds, 15), 300), int(row["deadline_at"]))
            if lease_at <= now:
                raise ValueError("job deadline expired")
            cursor.execute(
                "UPDATE fleet_worker_jobs SET lease_expires_at = ?, updated_at = ? WHERE job_id = ?",
                (lease_at, now, job_id),
            )
            cursor.execute(
                """UPDATE fleet_reservations SET lease_expires_at = ?
                   WHERE job_id = ? AND status = 'active' AND fence = ?""",
                (lease_at, job_id, fence),
            )
            connection.commit()
            return {"lease_expires_at": lease_at, "cancel_requested": False}
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def complete_job(
        self, job_id: str, *, worker_id: str, fence: int, ok: bool,
        result: dict[str, Any], error_code: str = "",
    ) -> dict[str, Any]:
        now = int(time.time())
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            row = cursor.execute(
                "SELECT * FROM fleet_worker_jobs WHERE job_id = ? FOR UPDATE", (job_id,)
            ).fetchone()
            if row is None:
                raise LookupError("job not found")
            if row["worker_id"] != worker_id or int(row["fence"]) != int(fence):
                raise PermissionError("stale worker receipt")
            if row["status"] not in {"running", "verifying", "cancelling"}:
                return self._job(dict(row), cursor)
            cancelled = row["status"] == "cancelling"
            reserved_cost = int(row["reserved_cost_microunits"] or 0)
            request = ResourceRequest.parse(_decode(row["constraints_json"], {}))
            settlement = settle_reported_cost(
                request, result.get("cost_microunits")
            )
            cost_invalid = settlement.invalid_report or settlement.limit_exceeded
            status = (
                "cancelled"
                if cancelled
                else ("succeeded" if ok and not cost_invalid else "failed")
            )
            stored_result = {"cancelled": True} if cancelled else result
            stored_error = (
                "cancelled"
                if cancelled
                else (
                    "invalid_cost_report"
                    if settlement.invalid_report
                    else (
                        "cost_limit_exceeded"
                        if settlement.limit_exceeded
                        else error_code[:80]
                    )
                )
            )
            settled_cost = settlement.settled_microunits if row["grant_id"] else 0
            if row["grant_id"]:
                cursor.execute(
                    """UPDATE fleet_borrow_grants SET
                       budget_reserved_microunits = GREATEST(budget_reserved_microunits - ?, 0),
                       budget_spent_microunits = budget_spent_microunits + ?,
                       resource_version = resource_version + 1, updated_at = ?
                       WHERE grant_id = ?""",
                    (reserved_cost, settled_cost, now, row["grant_id"]),
                )
            cursor.execute(
                """UPDATE fleet_worker_jobs SET status = ?, result_json = ?, error_code = ?,
                   lease_expires_at = NULL, reserved_cost_microunits = 0,
                   settled_cost_microunits = settled_cost_microunits + ?,
                   updated_at = ? WHERE job_id = ?""",
                (
                    status, canonical_json(stored_result), stored_error,
                    settled_cost, now, job_id,
                ),
            )
            cursor.execute(
                """UPDATE fleet_reservations SET status = 'released', released_at = ?
                   WHERE job_id = ? AND status = 'active' AND fence = ?""",
                (now, job_id, fence),
            )
            self._event(
                cursor, "fleet_job_events", "job_id", job_id,
                event_type=(
                    "cancelled"
                    if cancelled
                    else ("completed" if status == "succeeded" else "failed")
                ),
                status=status, worker_id=worker_id, fence=fence,
                payload={
                    **stored_result,
                    "reported_cost_microunits": settlement.reported_microunits,
                    "settled_cost_microunits": settled_cost,
                    "cost_limit_exceeded": settlement.limit_exceeded,
                },
                created_at=now,
            )
            if row["kind"] == "preview.static":
                self._finish_preview(cursor, dict(row), result, status, now)
            connection.commit()
            return self.get_job(job_id) or {}
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def _job(self, item: dict[str, Any], cursor: Any) -> dict[str, Any]:
        for key in ("payload_json", "constraints_json", "result_json"):
            item[key.removesuffix("_json")] = _decode(item.pop(key, "{}"), {})
        events = cursor.execute(
            """SELECT sequence, event_type, status, worker_id, fence, payload_json, created_at
               FROM fleet_job_events WHERE job_id = ? ORDER BY sequence""",
            (item["job_id"],),
        ).fetchall()
        item["events"] = []
        for event in events:
            decoded = dict(event)
            decoded["payload"] = _decode(decoded.pop("payload_json", "{}"), {})
            item["events"].append(decoded)
        checkpoint = cursor.execute(
            """SELECT checkpoint_id, sequence, worker_id, fence, phase,
                      format_version, executor_version, state_json, state_hash, created_at
               FROM fleet_job_checkpoints WHERE job_id = ?
               ORDER BY sequence DESC LIMIT 1""",
            (item["job_id"],),
        ).fetchone()
        item["resume_checkpoint"] = None
        if checkpoint is not None:
            decoded_checkpoint = dict(checkpoint)
            decoded_checkpoint["state"] = _decode(
                decoded_checkpoint.pop("state_json", "{}"), {}
            )
            item["resume_checkpoint"] = decoded_checkpoint
        return item

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            row = cursor.execute("SELECT * FROM fleet_worker_jobs WHERE job_id = ?", (job_id,)).fetchone()
            return self._job(dict(row), cursor) if row else None
        finally:
            cursor.close()
            connection.close()

    def recent_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            rows = cursor.execute(
                "SELECT * FROM fleet_worker_jobs ORDER BY updated_at DESC LIMIT ?",
                (min(max(limit, 1), 200),),
            ).fetchall()
            return [self._job(dict(row), cursor) for row in rows]
        finally:
            cursor.close()
            connection.close()

    def cancel_job(self, job_id: str, *, actor_id: str, origin_scope: str) -> dict[str, Any]:
        now = int(time.time())
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            row = cursor.execute(
                "SELECT * FROM fleet_worker_jobs WHERE job_id = ? FOR UPDATE", (job_id,)
            ).fetchone()
            if row is None:
                raise LookupError("job not found")
            if not actor_id.startswith("admin:") and (
                row["actor_id"] != actor_id or row["origin_scope"] != origin_scope
            ):
                raise PermissionError("job belongs to another scope")
            status = "cancelled" if row["status"] == "queued" else "cancelling"
            if row["status"] in {"succeeded", "failed", "cancelled"}:
                return self._job(dict(row), cursor)
            cursor.execute(
                "UPDATE fleet_worker_jobs SET status = ?, updated_at = ? WHERE job_id = ?",
                (status, now, job_id),
            )
            if status == "cancelled":
                if row["grant_id"] and int(row["reserved_cost_microunits"] or 0):
                    cursor.execute(
                        """UPDATE fleet_borrow_grants SET budget_reserved_microunits =
                           GREATEST(budget_reserved_microunits - ?, 0),
                           resource_version = resource_version + 1, updated_at = ?
                           WHERE grant_id = ?""",
                        (int(row["reserved_cost_microunits"]), now, row["grant_id"]),
                    )
                cursor.execute(
                    "UPDATE fleet_reservations SET status = 'cancelled', released_at = ? WHERE job_id = ? AND status = 'active'",
                    (now, job_id),
                )
                cursor.execute(
                    """UPDATE fleet_worker_jobs SET grant_id = NULL,
                       reserved_cost_microunits = 0 WHERE job_id = ?""",
                    (job_id,),
                )
            if row["kind"] == "preview.static":
                cursor.execute(
                    """UPDATE fleet_previews SET state = 'disabled',
                       health_status = 'cancelled', updated_at = ? WHERE job_id = ?""",
                    (now, job_id),
                )
            self._event(
                cursor, "fleet_job_events", "job_id", job_id,
                event_type="cancel_requested", status=status,
                worker_id=str(row["worker_id"] or ""), fence=int(row["fence"]), created_at=now,
            )
            connection.commit()
            return self.get_job(job_id) or {}
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def save_checkpoint(
        self, job_id: str, *, worker_id: str, fence: int, phase: str,
        format_version: int, executor_version: str, state: dict[str, Any],
    ) -> dict[str, Any]:
        if phase not in {"started", "progress", "completed"}:
            raise ValueError("invalid checkpoint phase")
        encoded = canonical_json(state)
        if len(encoded.encode("utf-8")) > 64_000:
            raise ValueError("checkpoint state exceeds 64 KiB")
        state_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        now = int(time.time())
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            row = cursor.execute(
                """SELECT status, worker_id, fence, constraints_json
                   FROM fleet_worker_jobs WHERE job_id = ? FOR UPDATE""",
                (job_id,),
            ).fetchone()
            if row is None:
                raise LookupError("job not found")
            if (
                row["status"] not in {"running", "verifying"}
                or row["worker_id"] != worker_id
                or int(row["fence"]) != int(fence)
            ):
                raise PermissionError("stale worker checkpoint")
            request = ResourceRequest.parse(_decode(row["constraints_json"], {}))
            if not request.checkpointable:
                raise PermissionError("job does not permit checkpoints")
            if format_version != 1 or executor_version != request.executor_version:
                raise ValueError("checkpoint format or executor version is incompatible")
            previous = cursor.execute(
                """SELECT checkpoint_id, sequence, phase, state_hash
                   FROM fleet_job_checkpoints WHERE job_id = ?
                   ORDER BY sequence DESC LIMIT 1""",
                (job_id,),
            ).fetchone()
            if previous and previous["phase"] == "completed":
                if previous["state_hash"] != state_hash:
                    raise ValueError("completed checkpoint is immutable")
                connection.commit()
                return self.checkpoint(str(previous["checkpoint_id"])) or {}
            checkpoint_id = new_handle("checkpoint")
            sequence = int(previous["sequence"]) + 1 if previous else 1
            cursor.execute(
                """INSERT INTO fleet_job_checkpoints
                   (checkpoint_id, job_id, sequence, worker_id, fence, phase,
                    format_version, executor_version, state_json, state_hash, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    checkpoint_id, job_id, sequence, worker_id, fence, phase,
                    format_version, executor_version, encoded, state_hash, now,
                ),
            )
            cursor.execute(
                "UPDATE fleet_worker_jobs SET resume_checkpoint_id = ?, updated_at = ? WHERE job_id = ?",
                (checkpoint_id, now, job_id),
            )
            self._event(
                cursor, "fleet_job_events", "job_id", job_id,
                event_type="checkpoint_saved", status=str(row["status"]),
                worker_id=worker_id, fence=fence,
                payload={"checkpoint_id": checkpoint_id, "phase": phase},
                created_at=now,
            )
            connection.commit()
            return self.checkpoint(checkpoint_id) or {}
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def checkpoint(self, checkpoint_id: str) -> dict[str, Any] | None:
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            row = cursor.execute(
                "SELECT * FROM fleet_job_checkpoints WHERE checkpoint_id = ?",
                (checkpoint_id,),
            ).fetchone()
            if row is None:
                return None
            item = dict(row)
            item["state"] = _decode(item.pop("state_json", "{}"), {})
            return item
        finally:
            cursor.close()
            connection.close()

    def checkpoints(self, job_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            rows = cursor.execute(
                """SELECT * FROM fleet_job_checkpoints WHERE job_id = ?
                   ORDER BY sequence DESC LIMIT ?""",
                (job_id, min(max(limit, 1), 200)),
            ).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                item["state"] = _decode(item.pop("state_json", "{}"), {})
                items.append(item)
            return items
        finally:
            cursor.close()
            connection.close()

    def store_artifact(
        self, *, actor_id: str, origin_scope: str, name: str, media_type: str,
        content_base64: str, job_id: str | None = None,
    ) -> dict[str, Any]:
        name = safe_artifact_name(name)
        try:
            content = base64.b64decode(content_base64, validate=True)
        except ValueError as exc:
            raise ValueError("artifact content is not valid base64") from exc
        if not content or len(content) > 25 * 1024 * 1024:
            raise ValueError("artifact must be between 1 byte and 25 MiB")
        digest = hashlib.sha256(content).hexdigest()
        artifact_id = new_handle("artifact")
        target_dir = self.artifact_root / digest[:2]
        target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = target_dir / digest
        created_blob = False
        if not target.exists():
            with tempfile.NamedTemporaryFile(dir=target_dir, delete=False) as output:
                output.write(content)
                temp_name = output.name
            os.chmod(temp_name, 0o400)
            os.replace(temp_name, target)
            created_blob = True
        now = int(time.time())
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                """INSERT INTO fleet_artifacts
                   (artifact_id, job_id, actor_id, origin_scope, name, media_type,
                    size_bytes, sha256, storage_ref, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'staged', ?)""",
                (artifact_id, job_id, actor_id, origin_scope, name, media_type[:120],
                 len(content), digest, str(target), now),
            )
            connection.commit()
            return self.artifact(artifact_id) or {}
        except Exception:
            connection.rollback()
            if created_blob:
                target.unlink(missing_ok=True)
            raise
        finally:
            cursor.close()
            connection.close()

    def artifact(self, artifact_id: str) -> dict[str, Any] | None:
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            row = cursor.execute("SELECT * FROM fleet_artifacts WHERE artifact_id = ?", (artifact_id,)).fetchone()
            return dict(row) if row else None
        finally:
            cursor.close()
            connection.close()

    def artifact_bytes(self, artifact_id: str) -> tuple[dict[str, Any], bytes]:
        item = self.artifact(artifact_id)
        if item is None:
            raise LookupError("artifact not found")
        path = Path(str(item["storage_ref"]))
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise LookupError("artifact content unavailable") from exc
        if hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise ValueError("artifact checksum mismatch")
        return item, content

    def artifact_bytes_for_worker(
        self, artifact_id: str, *, job_id: str, worker_id: str, fence: int
    ) -> tuple[dict[str, Any], bytes]:
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            row = cursor.execute(
                """SELECT payload_json, status, worker_id, fence
                   FROM fleet_worker_jobs WHERE job_id = ?""",
                (job_id,),
            ).fetchone()
            if row is None:
                raise LookupError("job not found")
            payload = _decode(row["payload_json"], {})
            if (
                row["status"] not in {"running", "verifying"}
                or row["worker_id"] != worker_id
                or int(row["fence"]) != int(fence)
                or payload.get("artifact_id") != artifact_id
            ):
                raise PermissionError("artifact is not assigned to this worker lease")
        finally:
            cursor.close()
            connection.close()
        return self.artifact_bytes(artifact_id)

    def validate_artifact(self, artifact_id: str, *, ok: bool) -> None:
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "UPDATE fleet_artifacts SET status = ?, validated_at = ? WHERE artifact_id = ?",
                ("validated" if ok else "rejected", int(time.time()), artifact_id),
            )
            connection.commit()
        finally:
            cursor.close()
            connection.close()

    def create_preview(self, record: dict[str, Any]) -> dict[str, Any]:
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                """INSERT INTO fleet_previews
                   (preview_id, job_id, artifact_id, actor_id, origin_scope, route,
                    state, expires_at, cleanup_policy, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)""",
                (record["preview_id"], record["job_id"], record["artifact_id"],
                 record["actor_id"], record["origin_scope"], record["route"],
                 record["expires_at"], record["cleanup_policy"],
                 record["created_at"], record["created_at"]),
            )
            connection.commit()
            return self.preview(record["preview_id"]) or {}
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def _finish_preview(
        self, cursor: Any, job: dict[str, Any], result: dict[str, Any],
        status: str, now: int,
    ) -> None:
        payload = _decode(job["payload_json"], {})
        preview_id = str(payload.get("preview_id") or "")
        if not preview_id:
            return
        cursor.execute(
            """UPDATE fleet_previews SET worker_id = ?, public_url = ?, state = ?,
               health_status = ?, last_checked_at = ?, updated_at = ? WHERE preview_id = ?""",
            (job.get("worker_id"), str(result.get("public_url") or "") if status == "succeeded" else "",
             "active" if status == "succeeded" else ("disabled" if status == "cancelled" else "failed"),
             "healthy" if status == "succeeded" else status,
             now, now, preview_id),
        )

    def preview(self, preview_id: str) -> dict[str, Any] | None:
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            row = cursor.execute("SELECT * FROM fleet_previews WHERE preview_id = ?", (preview_id,)).fetchone()
            return dict(row) if row else None
        finally:
            cursor.close()
            connection.close()

    def previews(self, limit: int = 50) -> list[dict[str, Any]]:
        now = int(time.time())
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                """UPDATE fleet_previews SET state = 'expired', health_status = 'expired',
                   updated_at = ? WHERE state = 'active' AND expires_at <= ?""",
                (now, now),
            )
            rows = cursor.execute(
                "SELECT * FROM fleet_previews ORDER BY updated_at DESC LIMIT ?",
                (min(max(limit, 1), 200),),
            ).fetchall()
            connection.commit()
            return [dict(row) for row in rows]
        finally:
            cursor.close()
            connection.close()

    def reservations(self, limit: int = 100) -> list[dict[str, Any]]:
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            rows = cursor.execute(
                "SELECT * FROM fleet_reservations ORDER BY created_at DESC LIMIT ?",
                (min(max(limit, 1), 200),),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            cursor.close()
            connection.close()
