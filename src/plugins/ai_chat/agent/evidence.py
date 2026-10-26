"""Immutable tool receipts scoped to one task revision, never model assertions."""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from typing import Any

from .control import assert_job_owned


EVIDENCE_SQL = """
CREATE TABLE IF NOT EXISTS subagent_evidence (
    evidence_id TEXT PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES subagent_tasks(task_id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    run_id INTEGER NOT NULL REFERENCES subagent_runs(run_id) ON DELETE CASCADE,
    call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    complete INTEGER NOT NULL,
    recorded_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_subagent_evidence_task
ON subagent_evidence(task_id, revision, run_id);
"""
MAX_EVIDENCE_BYTES = 256_000
_SECRET_KEY = re.compile(r"^(?:.*[_-])?(?:password|secret|token|api[_-]?key|authorization|cookie|otp|验证码|密码)$", re.I)
_SECRET_TEXT = re.compile(r"\bsk-[A-Za-z0-9_-]{10,}|(?i:Bearer\s+)[A-Za-z0-9._~+/=-]{8,}")


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): "[REDACTED]" if _SECRET_KEY.fullmatch(str(key)) else redact(item)
                for key, item in value.items() if key != "_task_evidence"}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _SECRET_TEXT.sub("[REDACTED]", value)
    return value


def decode_result(raw: str | dict) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {"content": str(raw)}
    return value if isinstance(value, dict) else {"content": value}


class EvidenceStoreMixin:
    def record_evidence(self, task_id: int, run_id: int, tool_name: str, arguments: dict,
                        raw: str | dict, *, call_id: str = "", revision: int | None = None,
                        now: int | None = None) -> dict:
        assert_job_owned()
        revision = self.control(task_id)["revision"] if revision is None else revision
        payload = canonical(redact(decode_result(raw)))
        payload_hash = hashlib.sha256(payload.encode()).hexdigest()
        arguments_json = canonical(redact(arguments))
        complete = len(payload.encode()) <= MAX_EVIDENCE_BYTES
        if not complete:
            payload = canonical({"error": "evidence_too_large", "preview": payload[:4000]})
        identity = canonical([task_id, revision, run_id, call_id, tool_name, arguments_json, payload_hash])
        evidence_id = "evidence#" + hashlib.sha256(identity.encode()).hexdigest()[:32]
        timestamp = int(time.time() if now is None else now)
        with self._transaction() as cursor:
            # Ownership and revision are checked inside the write transaction.
            lock = "" if self._legacy_sqlite else " FOR UPDATE"
            task = cursor.execute("SELECT task_id FROM subagent_tasks WHERE task_id=?" + lock, (task_id,)).fetchone()
            run = cursor.execute("SELECT task_id FROM subagent_runs WHERE run_id=?", (run_id,)).fetchone()
            control = cursor.execute("SELECT revision FROM subagent_controls WHERE task_id=?" + lock, (task_id,)).fetchone()
            current = int(control["revision"]) if control else 1
            if task is None or run is None or int(run["task_id"]) != task_id or revision != current:
                raise PermissionError("Evidence does not belong to this task revision")
            cursor.execute("""INSERT INTO subagent_evidence
                (evidence_id, task_id, revision, run_id, call_id, tool_name, arguments_json,
                 payload_json, payload_hash, complete, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
                (evidence_id, task_id, revision, run_id, call_id, tool_name, arguments_json,
                 payload, payload_hash, int(complete), timestamp))
            stored = cursor.execute("SELECT recorded_at FROM subagent_evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
        self._notify_changed(task_id)
        return {"ref": evidence_id, "sha256": payload_hash, "complete": complete,
                "recorded_at": int(stored["recorded_at"]), "revision": revision}

    def task_evidence(self, task_id: int, *, revision: int | None = None,
                      run_ids: set[int] | None = None) -> list[dict]:
        revision = self.control(task_id)["revision"] if revision is None else revision
        with self._lock:
            rows = self._connection.execute("""SELECT * FROM subagent_evidence
                WHERE task_id=? AND revision=? ORDER BY recorded_at, evidence_id""", (task_id, revision)).fetchall()
        return [{**dict(row), "complete": bool(row["complete"]),
                 "arguments": json.loads(row["arguments_json"]), "payload": json.loads(row["payload_json"])}
                for row in rows if run_ids is None or int(row["run_id"]) in run_ids]


def evidence_index(items: list[dict]) -> list[dict]:
    return [{"ref": item["evidence_id"], "tool": item["tool_name"], "run_id": item["run_id"],
             "recorded_at": item["recorded_at"], "complete": item["complete"],
             "arguments": item["arguments"]} for item in items]


READ_TASK_EVIDENCE = {"type": "function", "function": {
    "name": "read_task_evidence",
    "description": "读取本步骤或获准上游的宿主工具证据。使用工具返回的 evidence# 标识；不能读取其他任务、修订或未授权步骤。",
    "parameters": {"type": "object", "additionalProperties": False,
        "properties": {"ref": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}},
        "required": ["ref"]}}}


READ_TASK_EVIDENCE_BATCH = {"type": "function", "function": {
    "name": "read_task_evidence_batch",
    "description": "批量读取最多8条获准的任务证据。每项独立分页；next_offset非空时必须继续读取，不能把分页片段当成完整证据。",
    "parameters": {"type": "object", "additionalProperties": False,
        "properties": {"requests": {"type": "array", "minItems": 1, "maxItems": 8,
            "items": {"type": "object", "additionalProperties": False,
                "properties": {"ref": {"type": "string", "maxLength": 80},
                               "offset": {"type": "integer", "minimum": 0}},
                "required": ["ref"]}}}, "required": ["requests"]}}}


def read_evidence(items: list[dict], arguments: dict, *, max_chars: int = 12000,
                  measure: Callable[[str], int] = len) -> str:
    found = next((item for item in items if item["evidence_id"] == arguments.get("ref")), None)
    if found is None:
        return canonical({"ok": False, "error": "Evidence is outside the authorized task/step context"})
    offset = arguments.get("offset", 0)
    if type(offset) is not int or offset < 0:
        return canonical({"ok": False, "error": "Invalid offset"})
    body = canonical(found["payload"])
    def page(length):
        end = offset + length
        return canonical({"ok": True, "ref": found["evidence_id"], "sha256": found["payload_hash"],
            "complete": found["complete"], "recorded_at": found["recorded_at"],
            "content": body[offset:end], "next_offset": end if end < len(body) else None})
    # JSON escaping and the evidence envelope count toward the actual model input.
    low, high = 0, max(0, min(len(body) - offset, 12000))
    while low < high:
        middle = (low + high + 1) // 2
        if measure(page(middle)) <= max_chars:
            low = middle
        else:
            high = middle - 1
    if measure(page(low)) > max_chars or (low == 0 and offset < len(body)):
        return canonical({"ok": False, "error": "Evidence result budget exhausted"})
    return page(low)


def read_evidence_batch(items: list[dict], arguments: dict, *, max_chars: int = 12000,
                        measure: Callable[[str], int] = len) -> str:
    requests = arguments.get("requests")
    if (not isinstance(requests, list) or not 1 <= len(requests) <= 8
            or set(arguments) != {"requests"}):
        return canonical({"ok": False, "error": "Provide 1 to 8 evidence requests"})
    if any(not isinstance(item, dict) or not isinstance(item.get("ref"), str)
           or len(item["ref"]) > 80 or set(item) - {"ref", "offset"}
           or type(item.get("offset", 0)) is not int or item.get("offset", 0) < 0
           for item in requests):
        return canonical({"ok": False, "error": "Invalid evidence batch request"})
    if len({item["ref"] for item in requests}) != len(requests):
        return canonical({"ok": False, "error": "Use one page per reference in each batch"})
    result = {"ok": True, "results": []}
    for index, request in enumerate(requests):
        remaining = max_chars - measure(canonical(result)) - 1
        per_item = remaining // (len(requests) - index)
        result["results"].append(json.loads(read_evidence(items, request, max_chars=per_item, measure=measure)))
    raw = canonical(result)
    return raw if measure(raw) <= max_chars else canonical({"ok": False, "error": "Evidence result budget exhausted"})
