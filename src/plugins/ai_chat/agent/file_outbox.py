"""Durable file manifests; retry only when no upload could have happened."""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from .control import assert_job_owned


class FileOutboxStoreMixin:
    def queue_file(self, task_id: int, artifact: dict, filename: str) -> dict:
        assert_job_owned()
        digest = str(artifact.get("snapshot", ""))
        if not re.fullmatch(r"[a-f0-9]{64}", digest) or not filename:
            raise ValueError("File outbox needs an immutable snapshot and delivery name")
        revision = self.control(task_id)["revision"]
        payload = {"artifact": artifact, "filename": filename, "size": artifact["size"],
                   "handle": artifact["handle"], "attempts": 0, "next_attempt_at": 0,
                   "ok": False, "state": "queued", "error": "等待文件投递"}
        if artifact.get("draft") is True:
            payload.update(draft=True, draft_reason=str(artifact.get("draft_reason") or "内容尚未全部验收通过"))
        with self._transaction() as cursor:
            cursor.execute("""INSERT INTO subagent_deliveries
                (task_id, revision, delivery_key, state, payload_json, updated_at)
                VALUES (?, ?, ?, 'queued', ?, ?) ON CONFLICT DO NOTHING""",
                (task_id, revision, digest, json.dumps(payload, ensure_ascii=False), int(time.time())))
            row = cursor.execute("""SELECT * FROM subagent_deliveries
                WHERE task_id=? AND revision=? AND delivery_key=?""", (task_id, revision, digest)).fetchone()
        self._notify_changed(task_id)
        return {"key": digest, "revision": revision, "state": row["state"], "payload": json.loads(row["payload_json"]),
                "updated_at": row["updated_at"]}

    def queued_file_tasks(self) -> list[int]:
        with self._lock:
            rows = self._connection.execute("""SELECT DISTINCT d.task_id FROM subagent_deliveries d
                JOIN subagent_controls c ON c.task_id=d.task_id AND c.revision=d.revision
                JOIN subagent_tasks t ON t.task_id=d.task_id
                WHERE d.state='queued' AND c.dispatch_json <> '{}' AND t.cancel_requested=? LIMIT 50""", (False,)).fetchall()
        return [int(row["task_id"]) for row in rows]

    def rejected_file_tasks(self) -> list[int]:
        with self._lock:
            rows = self._connection.execute("""SELECT DISTINCT d.task_id FROM subagent_deliveries d
                JOIN subagent_controls c ON c.task_id=d.task_id AND c.revision=d.revision
                WHERE d.state='rejected' AND c.dispatch_json <> '{}'
                AND NOT EXISTS (SELECT 1 FROM subagent_checkpoints p WHERE p.task_id=d.task_id
                    AND p.phase='file_rejected:' || CAST(d.revision AS TEXT) || ':' || d.delivery_key)
                LIMIT 50""").fetchall()
        return [int(row["task_id"]) for row in rows]

    def claim_file(self, task_id: int, delivery: dict) -> dict | None:
        assert_job_owned()
        now = int(time.time())
        with self._transaction() as cursor:
            lock = "" if self._legacy_sqlite else " FOR UPDATE"
            control = cursor.execute("SELECT revision FROM subagent_controls WHERE task_id=?" + lock, (task_id,)).fetchone()
            current_revision = int(control["revision"]) if control else 1
            task = cursor.execute("SELECT cancel_requested FROM subagent_tasks WHERE task_id=?", (task_id,)).fetchone()
            if current_revision != delivery["revision"] or task is None or task["cancel_requested"]:
                return None
            row = cursor.execute("""SELECT state, payload_json FROM subagent_deliveries
                WHERE task_id=? AND revision=? AND delivery_key=?""" + lock,
                (task_id, delivery["revision"], delivery["key"])).fetchone()
            if row is None or row["state"] != "queued":
                return None
            payload = json.loads(row["payload_json"])
            if payload.get("next_attempt_at", 0) > now:
                return None
            payload.pop("blocked_reason", None)
            payload.update(attempts=payload.get("attempts", 0) + 1, upload_started_at=now, state="sending",
                           not_sent=False, retryable=False, error="")
            cursor.execute("""UPDATE subagent_deliveries SET state='sending', payload_json=?, updated_at=?
                WHERE task_id=? AND revision=? AND delivery_key=? AND state='queued'""",
                (json.dumps(payload, ensure_ascii=False), now, task_id, delivery["revision"], delivery["key"]))
            return payload if cursor.rowcount == 1 else None

    def defer_file_preparation(self, task_id: int, delivery: dict, error: str) -> dict:
        payload = dict(delivery["payload"])
        failures = int(payload.get("preparation_failures", 0)) + 1
        state = "rejected" if failures >= 5 else "queued"
        payload.update(preparation_failures=failures, state=state, ok=False, not_sent=True,
                       error=error, next_attempt_at=int(time.time()) + 30)
        with self._transaction() as cursor:
            cursor.execute("""UPDATE subagent_deliveries SET state=?, payload_json=?, updated_at=?
                WHERE task_id=? AND revision=? AND delivery_key=? AND state='queued'""",
                (state, json.dumps(payload, ensure_ascii=False), int(time.time()), task_id, delivery["revision"], delivery["key"]))
        self._notify_changed(task_id)
        return payload

    def defer_file_availability(self, task_id: int, delivery: dict, error: str) -> dict:
        """An offline transport has not attempted upload or damaged the artifact."""
        assert_job_owned()
        now = int(time.time())
        with self._transaction() as cursor:
            lock = "" if self._legacy_sqlite else " FOR UPDATE"
            control = cursor.execute("SELECT revision FROM subagent_controls WHERE task_id=?" + lock, (task_id,)).fetchone()
            task = cursor.execute("SELECT cancel_requested FROM subagent_tasks WHERE task_id=?", (task_id,)).fetchone()
            row = cursor.execute("""SELECT state, payload_json FROM subagent_deliveries
                WHERE task_id=? AND revision=? AND delivery_key=?""" + lock,
                (task_id, delivery["revision"], delivery["key"])).fetchone()
            if row is None:
                raise ValueError("File delivery manifest disappeared")
            payload = json.loads(row["payload_json"])
            current_revision = int(control["revision"]) if control else 1
            if (row["state"] != "queued" or payload != delivery["payload"]
                    or current_revision != delivery["revision"] or not task or task["cancel_requested"]):
                return {**payload, "state": row["state"], "ok": row["state"] == "acknowledged"}
            payload.update(state="queued", ok=False, not_sent=True, retryable=True,
                           availability_checks=int(payload.get("availability_checks", 0)) + 1,
                           blocked_reason="transport_unavailable", error=error, next_attempt_at=now + 30)
            cursor.execute("""UPDATE subagent_deliveries SET payload_json=?, updated_at=?
                WHERE task_id=? AND revision=? AND delivery_key=? AND state='queued'""",
                (json.dumps(payload, ensure_ascii=False), now, task_id, delivery["revision"], delivery["key"]))
        self._notify_changed(task_id)
        return payload

    def settle_file_attempt(self, task_id: int, delivery: dict, claimed: dict, result: dict) -> dict:
        now = int(time.time())
        if result.get("ok"):
            state = "acknowledged"
        elif result.get("not_sent") is True and result.get("retryable") is True and claimed["attempts"] < 5:
            state = "queued"
        elif result.get("not_sent") is True:
            state = "rejected"
        else:
            state = "unknown"
        payload = {**claimed, **{key: result[key] for key in
            ("receipt", "error", "uploaded", "file_id", "not_sent", "retryable") if key in result},
            "ok": state == "acknowledged", "state": state,
            "next_attempt_at": now + min(15 * 2 ** claimed["attempts"], 300) if state == "queued" else 0}
        with self._transaction() as cursor:
            lock = "" if self._legacy_sqlite else " FOR UPDATE"
            row = cursor.execute("""SELECT state, payload_json FROM subagent_deliveries
                WHERE task_id=? AND revision=? AND delivery_key=?""" + lock,
                (task_id, delivery["revision"], delivery["key"])).fetchone()
            if row is None:
                raise ValueError("File delivery manifest disappeared")
            current = json.loads(row["payload_json"])
            same_attempt = all(current.get(key) == claimed.get(key) for key in ("attempts", "upload_started_at"))
            if row["state"] not in {"sending", "unknown"} or not same_attempt:
                return {**current, "state": row["state"], "ok": row["state"] == "acknowledged"}
            cursor.execute("""UPDATE subagent_deliveries SET state=?, payload_json=?, updated_at=?
                WHERE task_id=? AND revision=? AND delivery_key=? AND state IN ('sending','unknown')""",
                (state, json.dumps(payload, ensure_ascii=False), now, task_id, delivery["revision"], delivery["key"]))
        self._notify_changed(task_id)
        return payload


async def attempt_file(store: Any, task_id: int, delivery: dict, *, prepare, send, readiness=None) -> dict:
    if delivery["state"] != "queued":
        return {**delivery["payload"], "state": delivery["state"], "ok": delivery["state"] == "acknowledged"}
    if delivery["payload"].get("next_attempt_at", 0) > time.time():
        return delivery["payload"]
    if readiness is not None:
        blocker = await readiness()
        if blocker is not None:
            return store.defer_file_availability(task_id, delivery, blocker)
    # Read and verify bytes before marking an upload as possibly sent.
    try:
        content = await prepare(delivery["payload"]["artifact"])
    except (OSError, ValueError) as exc:
        return store.defer_file_preparation(task_id, delivery, f"文件准备失败，尚未上传：{exc}")
    claim = store.claim_file(task_id, delivery)
    if claim is None:
        return {**delivery["payload"], "ok": False, "state": "pending", "error": "另一个投递进程正在处理，或任务已修订/取消"}
    try:
        raw = await send(content, claim["filename"])
        result = raw if isinstance(raw, dict) else json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("Invalid upload receipt")
    except asyncio.CancelledError:
        # Keep sending: after takeover, reconcile rather than blindly resend.
        raise
    except Exception as exc:
        result = {"ok": False, "error": f"上传回执不明确：{type(exc).__name__}"}
    return store.settle_file_attempt(task_id, delivery, claim, result)
