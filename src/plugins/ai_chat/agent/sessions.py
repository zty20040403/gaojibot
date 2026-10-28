from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from typing import Any
from .task_identity import make_owner, object_owner, require_same_owner


class AgentSessionStoreMixin:
    """A session belongs to one task step, not to a reusable role template."""

    def agent_session(self, task_id: int, run_id: int, *, scope_key: str, requester_user_id: int | None, endpoint=None, requester_actor=None) -> dict[str, Any]:
        expected = make_owner(requester_user_id, endpoint, requester_actor)
        task = self.get(task_id)
        if task is None or task.scope_key != scope_key:
            raise PermissionError("Agent session is outside the current task/owner scope")
        require_same_owner(object_owner(task), expected)
        with self._lock:
            owner = self._connection.execute(
                """SELECT r.run_id FROM subagent_runs r JOIN subagent_tasks t ON t.task_id = r.task_id
                   WHERE r.run_id = ? AND t.task_id = ? AND t.scope_key = ?""",
                (run_id, task_id, scope_key),
            ).fetchone()
            if owner is None:
                raise PermissionError("Agent session is outside the current task/owner scope")
            row = self._connection.execute(
                "SELECT version, transcript_json, model_profile FROM subagent_sessions WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return {"version": 0, "messages": [], "model_profile": ""}
        return {"version": int(row["version"]), "messages": json.loads(row["transcript_json"]),
                "model_profile": str(row["model_profile"])}

    def save_agent_session(
        self, task_id: int, run_id: int, messages: list[dict[str, Any]], *,
        scope_key: str, requester_user_id: int | None, model_profile: str, expected_version: int, endpoint=None, requester_actor=None,
    ) -> int:
        self.agent_session(task_id, run_id, scope_key=scope_key, requester_user_id=requester_user_id, endpoint=endpoint, requester_actor=requester_actor)
        if any(item.get("role") not in {"user", "assistant", "tool"} for item in messages):
            raise ValueError("Agent transcripts must not persist host system instructions")
        payload = json.dumps(messages, ensure_ascii=False)
        with self._transaction() as cursor:
            covered = int(cursor.execute("SELECT COALESCE(MAX(sequence),0) AS sequence FROM subagent_events WHERE run_id=?", (run_id,)).fetchone()["sequence"])
            if expected_version == 0:
                cursor.execute(
                    """INSERT INTO subagent_sessions (run_id, task_id, version, transcript_json, model_profile, updated_at, covered_sequence)
                       VALUES (?, ?, 1, ?, ?, ?, ?) ON CONFLICT(run_id) DO NOTHING""",
                    (run_id, task_id, payload, model_profile, int(time.time()), covered),
                )
            else:
                cursor.execute(
                    """UPDATE subagent_sessions SET version = version + 1, transcript_json = ?,
                       model_profile = ?, updated_at = ?, covered_sequence = ? WHERE run_id = ? AND task_id = ? AND version = ?""",
                    (payload, model_profile, int(time.time()), covered, run_id, task_id, expected_version),
                )
            if cursor.rowcount != 1:
                raise RuntimeError("Agent session version conflict; concurrent takeover is not allowed")
        return expected_version + 1


READ_AGENT_RESULT = {
    "type": "function", "function": {
        "name": "read_agent_result",
        "description": "分页读取此步骤直接依赖的上游完整结果。cluster_artifacts 列出当前上游交接的集群 artifact_id 及来源，可用于发布，无需重复上传；metadata 和 handoff 可核对原始回执。previous_evidence 是历史观测，不代表旧产物仍可交付。只允许当前任务已交接的 step_id，不可读其他会话。",
        "parameters": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "step_id": {"type": "string"},
                "section": {"type": "string", "enum": ["summary", "facts", "artifacts", "cluster_artifacts", "metadata", "citations", "warnings", "unresolved", "handoff", "previous_evidence", "findings", "completed", "authorization", "next_verification", "evidence_index"]},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["step_id", "section"],
        },
    },
}


def read_upstream_result(upstream, arguments) -> str:
    key, section = str(arguments.get("step_id", "")), str(arguments.get("section", ""))
    if key not in upstream or section not in {"summary", "facts", "artifacts", "cluster_artifacts", "metadata", "citations", "warnings", "unresolved", "handoff", "previous_evidence", "findings", "completed", "authorization", "next_verification", "evidence_index"}:
        return json.dumps({"ok": False, "error": "No authorized upstream result or section"})
    value = cluster_artifact_refs(upstream[key]) if section == "cluster_artifacts" else upstream[key].get(section, "" if section == "summary" else [])
    offset = max(int(arguments.get("offset", 0)), 0)
    limit = min(max(int(arguments.get("limit", 5)), 1), 20)
    if isinstance(value, str):
        limit *= 200
    elif not isinstance(value, list):
        value = [value]
    selected = value[offset:offset + limit]
    return json.dumps({"ok": True, "step_id": key, "section": section, "data": selected,
                       "total": len(value), "next_offset": offset + len(selected) if offset + len(selected) < len(value) else None}, ensure_ascii=False)


def cluster_artifact_refs(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Expose current handoff handles, without promoting historical evidence."""
    refs = []
    seen = set()
    for section in ("artifacts", "metadata", "handoff"):
        value = result.get(section)
        items = value if isinstance(value, list) else [value]
        for offset, item in enumerate(items):
            if isinstance(item, Mapping):
                artifact_id = item.get("artifact_id")
                ids = [artifact_id] if isinstance(artifact_id, str) and re.fullmatch(r"artifact_[a-f0-9]{32}", artifact_id) else []
            elif section == "handoff" and isinstance(item, str):
                # Older results put the upload receipt in handoff prose.
                ids = re.findall(r"(?<![A-Za-z0-9_])artifact_[a-f0-9]{32}(?![A-Za-z0-9_])", item)
            else:
                ids = []
            for artifact_id in ids:
                if artifact_id in seen:
                    continue
                seen.add(artifact_id)
                ref = {"artifact_id": artifact_id, "section": section, "offset": offset}
                if isinstance(item, Mapping) and item.get("name"):
                    ref["name"] = str(item["name"])[:200]
                refs.append(ref)
    return refs


def upstream_index(upstream) -> str:
    index = {}
    for key, result in upstream.items():
        refs = cluster_artifact_refs(result)
        index[key] = {
            "status": result.get("status", "partial"), "summary": str(result.get("summary", ""))[:160],
            "sections": {field: len(result.get(field) or []) for field in ("facts", "artifacts", "citations", "unresolved", "handoff", "previous_evidence", "findings", "completed", "authorization", "next_verification", "evidence_index")},
            "read_with": "read_agent_result", "step_id": key,
        }
        if refs:
            index[key]["cluster_artifacts"] = refs[:5]
            index[key]["sections"]["cluster_artifacts"] = len(refs)
    return json.dumps(index, ensure_ascii=False, separators=(",", ":"))
