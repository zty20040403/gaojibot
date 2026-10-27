"""Render published links from scoped remote receipts, never model-written URLs."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
import time
from urllib.parse import urlsplit

from .external import canonical


def preview_delivery_note(store, task, revision: int, *, now: int | None = None) -> str:
    timestamp = int(time.time()) if now is None else now
    links: dict[str, tuple[int, int]] = {}
    run_ids = {run.run_id for run in store.runs(task.task_id)}
    sources = list(store.external_calls(task.task_id, revision=revision))
    checkpoints = {item["state"].get("revision"): item["state"]
                   for item in store.revision_checkpoints(task.task_id)}
    inherited = set(run_ids)
    for current in range(revision, 1, -1):
        inherited &= {item["run_id"] for item in checkpoints.get(current, {}).get("previous_runs", [])}
        if not inherited:
            break
        sources.extend(item for item in store.external_calls(task.task_id, revision=current - 1)
                       if item["run_id"] in inherited)
    for item in sources:
        request, record = item["request"], item["response"]
        body = request.get("body") or {}
        if not isinstance(body, dict) or not isinstance(record, dict):
            continue
        expected_key = "subagent:" + hashlib.sha256(
            canonical([task.task_id, item["revision"], item["run_id"], item["call_id"]]).encode()
        ).hexdigest()
        if (item["task_id"] != task.task_id or item["status"] != "resolved" or item["run_id"] not in run_ids
                or request.get("method") != "POST" or request.get("path") != "/v1/jobs"
                or request.get("actor") != f"qq:{store.revision_actor(task.task_id, item['revision'])}" or request.get("origin") != task.scope_key
                or body.get("kind") != "preview.static" or body.get("idempotency_key") != expected_key
                or record.get("kind") != "preview.static" or record.get("status") != "succeeded"
                or record.get("actor_id") != request["actor"] or record.get("origin_scope") != task.scope_key
                or record.get("idempotency_key") != expected_key):
            continue
        payload = record.get("payload") or {}
        result = record.get("result") or {}
        requested_payload = body.get("payload") or {}
        if not all(isinstance(value, dict) for value in (payload, result, requested_payload)):
            continue
        preview_id = str(payload.get("preview_id") or "")
        url = str(result.get("public_url") or "")
        try:
            parsed = urlsplit(url)
            valid_url = (parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username
                         and not parsed.password and not parsed.query and not parsed.fragment
                         and parsed.path.endswith(f"/previews/{preview_id}/")
                         and not any(char.isspace() or ord(char) < 32 for char in url))
        except ValueError:
            valid_url = False
        if (not valid_url or re.fullmatch(r"preview_[a-f0-9]{32}", preview_id) is None
                or payload.get("artifact_id") != requested_payload.get("artifact_id")
                or type(payload.get("expires_at")) is not int
                or not 0 < payload["expires_at"] <= timestamp + 604800):
            continue
        links.setdefault(url, (payload["expires_at"], item["revision"]))
    if not links:
        return ""
    lines = ["网页发布回执（不代表任务其他验收项已通过）："]
    for url, (expires_at, source_revision) in links.items():
        expiry = datetime.fromtimestamp(expires_at, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        if expires_at <= timestamp:
            lines.append(f"第 {source_revision} 版的已发布预览已于 {expiry} 过期，需要重新发布。")
        else:
            lines.append(f"第 {source_revision} 版：{url}\n有效期至 {expiry}；仅能访问该 Worker 网络的人可打开，勿转发私密内容。")
    return "\n".join(lines)


def routine_preview_delivery_note(store, run, children) -> str:
    notes = []
    for child in children:
        task = store.get(child["task_id"])
        parent = store.routine_parent(child["task_id"])
        if (task is None or parent is None or parent["run_id"] != run.run_id
                or task.scope_key != run.spec.scope_key
                or str(task.requester_user_id) != run.spec.creator_id):
            raise PermissionError("Preview child does not belong to this scheduled occurrence")
        note = preview_delivery_note(store, task, store.control(task.task_id)["revision"])
        if note:
            notes.append(f"{task.handle}\n{note}")
    return "\n\n".join(notes)
