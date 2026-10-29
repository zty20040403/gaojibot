"""Durable, host-owned continuations for asynchronous Fleet operations."""
from __future__ import annotations

import hashlib
import json
import re
import time
from contextvars import ContextVar

from .control import assert_job_owned
from .task_identity import make_owner, owner_fields


class ExternalPending(BaseException):
    """Suspend orchestration, without treating an unfinished job as a failure."""


active_external: ContextVar[ExternalCalls | None] = ContextVar("agent_external_calls", default=None)
active_official_external: ContextVar[object | None] = ContextVar('official_external_bridge', default=None)
SUBMISSIONS = {"/v1/ops/call", "/v1/operations/prepare", "/v1/jobs", "/v1/deployments/prepare"}
READ_PATH = re.compile(r"/v1/(operations|jobs|deployments)/[A-Za-z0-9_.-]+")
TERMINAL = {
    "succeeded",
    "completed",
    "failed",
    "cancelled",
    "expired",
    "needs_attention",
    "timed_out",
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def remote_record(value):
    record = value.get("operation")
    return record if isinstance(record, dict) else value


def remote_path(value):
    record = remote_record(value)
    for key, kind in (("deployment_id", "deployments"), ("operation_id", "operations"), ("job_id", "jobs")):
        identifier = record.get(key)
        if identifier and re.fullmatch(r"[A-Za-z0-9_.-]+", str(identifier)):
            return f"/v1/{kind}/{identifier}"
    return ""


class ExternalStoreMixin:
    def external_calls(self, task_id, *, revision=None, run_id=None):
        revision = revision or self.control(task_id)["revision"]
        clause = " AND run_id=?" if run_id is not None else ""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM subagent_external_calls WHERE task_id=? AND revision=?" + clause + " ORDER BY updated_at, run_id, call_id",
                (task_id, revision, *((run_id,) if run_id is not None else ())),
            ).fetchall()
        return [{**dict(row), "request": json.loads(row["request_json"]),
                 "response": json.loads(row["response_json"])} for row in rows]

    def begin_external(self, task_id, revision, run_id, call_id, request, *, official_once_path=None):
        assert_job_owned()
        if self.control(task_id)["revision"] != revision:
            raise ValueError("External request belongs to an obsolete task revision")
        with self._transaction() as cursor:
            owner = cursor.execute("SELECT run_id FROM subagent_runs WHERE task_id=? AND run_id=?", (task_id, run_id)).fetchone()
            if owner is None:
                raise PermissionError("External request is outside its task")
            if official_once_path is not None:
                bridge = active_official_external.get()
                if (bridge is None or bridge.store is not self
                        or request.get('method') != 'POST' or request.get('path') != official_once_path
                        or re.fullmatch(r'/v1/operations/op_[a-f0-9]{32}/approve', official_once_path) is None):
                    raise PermissionError('Official external operation scope is unavailable')
                # The current task row serializes different model call IDs and
                # parallel steps before ANY physical approval handoff.
                cursor.execute('SELECT task_id FROM subagent_tasks WHERE task_id=? FOR UPDATE', (task_id,))
                previous = cursor.execute("""SELECT run_id,call_id,request_json FROM subagent_external_calls
                    WHERE task_id=? AND revision=? AND request_json::jsonb->>'path'=?
                    AND request_json::jsonb->>'method'='POST' ORDER BY updated_at,run_id,call_id LIMIT 1""",
                    (task_id,revision,official_once_path)).fetchone()
                if previous is not None:
                    original = json.loads(previous['request_json'])
                    if any(original.get(k) != request.get(k) for k in
                           ('method','path','body','actor','origin','official_source')):
                        raise PermissionError('Official external operation contract changed')
                    return previous['run_id'], previous['call_id']
            cursor.execute("""INSERT INTO subagent_external_calls
                (task_id, revision, run_id, call_id, request_json, status, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?) ON CONFLICT DO NOTHING""",
                (task_id, revision, run_id, call_id, canonical(request), int(time.time())))
            row = cursor.execute("""SELECT request_json FROM subagent_external_calls
                WHERE task_id=? AND revision=? AND run_id=? AND call_id=?""",
                (task_id, revision, run_id, call_id)).fetchone()
            if row["request_json"] != canonical(request):
                raise ValueError("Provider reused a tool call id for a different external request")

    def finish_external(self, item, response, *, official_resume_ready=False):
        path = remote_path(response) or item.get("remote_path", "")
        state = str(remote_record(response).get("status") or "")
        # A handle without a recognized final state is not proof of completion.
        if official_resume_ready:
            bridge = active_official_external.get()
            if bridge is None or bridge.store is not self:
                raise PermissionError('Official external operations are unavailable')
            source = bridge.guard(item['task_id'])
            _, context, contract = bridge._record(source, response)
            if (state != 'awaiting_approval' or item['request']['path'] != '/v1/operations/prepare'
                    or not bridge.granted(context, contract)):
                raise PermissionError('Official external receipt is not ready')
        waiting = bool(path and state not in TERMINAL and not official_resume_ready)
        if self.control(item["task_id"])["revision"] == item["revision"]:
            request = item.get("request") or json.loads(item.get("request_json") or "{}")
            ref = self.record_evidence(item["task_id"], item["run_id"], request.get("tool_name", ""),
                request.get("tool_arguments", {}), response, call_id=item["call_id"], revision=item["revision"])
            response = {**response, "_task_evidence": ref}
        with self._transaction() as cursor:
            cursor.execute("""UPDATE subagent_external_calls SET status=?, remote_path=?, response_json=?, updated_at=?
                WHERE task_id=? AND revision=? AND run_id=? AND call_id=?""",
                ("pending" if waiting else "resolved", path, canonical(response), int(time.time()),
                 item["task_id"], item["revision"], item["run_id"], item["call_id"]))
        self._notify_changed(item["task_id"])

    def hydrate_external_session(self, task_id, run_id):
        """Repair a crash between the durable remote receipt and transcript commit."""
        task = self.get(task_id)
        if getattr(task, "endpoint", None) is not None and self.external_calls(task_id):
            bridge = active_official_external.get()
            if bridge is None or bridge.store is not self:
                raise PermissionError("Official external operations are unavailable")
            bridge.hydrate(task_id)
        identity = owner_fields(make_owner(task.requester_user_id,
            getattr(task, "endpoint", None), getattr(task, "requester_actor", None)))
        session = self.agent_session(task_id, run_id, scope_key=task.scope_key, **identity)
        calls = {c["call_id"]: c for c in self.external_calls(task_id, run_id=run_id)}
        if not session["messages"]:
            return session
        messages = []
        source = session["messages"]
        i = 0
        while i < len(source):
            message = source[i]
            messages.append(message)
            i += 1
            if message.get("role") != "assistant" or not message.get("tool_calls"):
                continue
            answers = {}
            while i < len(source) and source[i].get("role") == "tool":
                answers[source[i].get("tool_call_id")] = source[i]
                i += 1
            for call in message["tool_calls"]:
                key = call["id"]
                item = calls.get(key)
                if item and item["status"] == "resolved":
                    answers[key] = {"role": "tool", "tool_call_id": key, "content": canonical(item["response"])}
                # Calls not started before a crash must have a protocol-valid reply.
                messages.append(answers.get(key) or {"role": "tool", "tool_call_id": key,
                    "content": '{"ok":false,"error":"Process interrupted before this tool returned; do not assume it ran."}'})
        if messages != source:
            self.save_agent_session(task_id, run_id, messages, scope_key=task.scope_key,
                **identity, model_profile=session["model_profile"], expected_version=session["version"])
        return self.agent_session(task_id, run_id, scope_key=task.scope_key, **identity)


class ExternalCalls:
    def __init__(self, store, task_id, run_id):
        self.store, self.task_id, self.run_id = store, task_id, run_id
        self.revision = store.control(task_id)["revision"]
        self.call_id = ""
        self.tool_name = ""
        self.arguments = {}

    def pause(self):
        if any(c["status"] == "pending" for c in self.store.external_calls(self.task_id, run_id=self.run_id)):
            raise ExternalPending()

    async def request(self, method, path, body, *, actor, origin, perform):
        task = self.store.get(self.task_id)
        if getattr(task, "endpoint", None) is not None:
            bridge = active_official_external.get()
            if bridge is None or bridge.store is not self.store:
                raise PermissionError("Official external operations are unavailable")
            return await bridge.request(self, method, path, body, actor=actor, origin=origin, perform=perform)
        if not self.call_id or not ((method == "POST" and path in SUBMISSIONS)
                                   or (method == "GET" and READ_PATH.fullmatch(path))):
            return await perform(body)
        actual_user = self.store.revision_actor(self.task_id)
        self.store.require_execution_actor(self.task_id, actual_user)
        if actor != f"qq:{actual_user}" or origin != task.scope_key:
            raise PermissionError("External call does not match the task owner")
        body = dict(body or {}) if body is not None else None
        if method == "POST":
            # The host, not the model, owns the replay identity.
            identity = canonical([self.task_id, self.revision, self.run_id, self.call_id])
            body["idempotency_key"] = "subagent:" + hashlib.sha256(identity.encode()).hexdigest()
        request = {"method": method, "path": path, "body": body, "actor": actor, "origin": origin,
                   "tool_name": self.tool_name, "tool_arguments": self.arguments}
        self.store.begin_external(self.task_id, self.revision, self.run_id, self.call_id, request)
        item = next(c for c in self.store.external_calls(self.task_id, run_id=self.run_id) if c["call_id"] == self.call_id)
        if item["status"] == "resolved" or item["remote_path"]:
            return item["response"]
        try:
            response = await perform(body)
        except Exception as exc:
            if getattr(exc, "retryable", False) or isinstance(exc, (TimeoutError, OSError)):
                # Keep the intent: replay uses the same upstream idempotency key.
                return {"ok": False, "pending": True, "message": "Remote receipt unavailable; background reconciliation will continue."}
            self.store.finish_external(item, {"ok": False, "error": str(exc)[:1000]})
            raise
        self.store.finish_external(item, response)
        return response


async def poll_external(store, task, client):
    """Called only by the task lease holder; never runs an LLM."""
    if getattr(task, "endpoint", None) is not None:
        bridge = active_official_external.get()
        if bridge is None or bridge.store is not store:
            raise PermissionError("Official external operations are unavailable")
        return await bridge.poll(task, client)
    from ..server_task_authorization import server_task
    control = store.control(task.task_id)
    actual_user = store.revision_actor(task.task_id)
    store.require_execution_actor(task.task_id, actual_user)
    scope = {"kind": "task", "id": task.task_id, "revision": control["revision"],
             "scope": task.scope_key, "qq_id": str(actual_user),
             "bot_id": str(control["dispatch"]["bot_id"]), "objective": task.objective}
    token = server_task.set(scope)
    try:
        for item in store.external_calls(task.task_id):
            if item["status"] != "pending":
                continue
            assert_job_owned()
            from .collaboration_runtime import guard_collaboration
            await guard_collaboration()
            current = store.get(task.task_id)
            if current.cancel_requested or current.finished_at is not None or store.control(task.task_id)["revision"] != control["revision"]:
                return False
            request = item["request"]
            if request["actor"] != f"qq:{actual_user}" or request["origin"] != task.scope_key:
                raise PermissionError("Stored external request has an invalid owner")
            try:
                if item["remote_path"]:
                    response = await client._request("GET", item["remote_path"], actor=request["actor"], origin=request["origin"])
                else:
                    response = await client._request(request["method"], request["path"], request["body"], actor=request["actor"], origin=request["origin"])
                store.finish_external(item, response)
            except Exception as exc:
                if not getattr(exc, "retryable", False) and not isinstance(exc, (TimeoutError, OSError)):
                    store.finish_external(item, {"ok": False, "error": str(exc)[:1000], "status": "needs_attention",
                        "remote_path": item["remote_path"], "message": "Observation failed; remote execution outcome is not confirmed."})
        return not any(c["status"] == "pending" for c in store.external_calls(task.task_id))
    finally:
        server_task.reset(token)
