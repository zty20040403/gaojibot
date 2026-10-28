"""Durable browser sessions and action receipts inside a long-lived project IO grant.

The application commits a response before ACKing the runner. Neither a timeout
nor a completed click releases the workspace: only verified drain followed by
the existing whole-workspace IO evidence may do that. Raw typed input is not
stored here; an interrupted dispatch is recovered by receipt, never retyped.
"""

from dataclasses import dataclass
import hashlib
import json
import re
import time
import uuid

from src.sandbox_runtime.project_browser_worker import FRAME_LIMIT, arguments, binding
from src.sandbox_runtime.wire import encoded

from .project_io import ACTIVE_STATES, BROWSER_SESSION_TOOL, ProjectIOGrant
from .project_turn_saves import require_running_turn
from .projects import ProjectConflict


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def action_digest(method, args):
    return digest({"method": method, "args": arguments(method, args)})


def _hex(value, length):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{%d}" % length, value) is not None


@dataclass(frozen=True)
class ProjectBrowserCall:
    grant: ProjectIOGrant
    sequence: int


class ProjectBrowserStore:
    def __init__(self, io):
        self.io, self.projects = io, io.projects

    def _owned(self, cursor, grant):
        project, operation = self.io._owned(cursor, grant)
        if operation["tool_name"] != BROWSER_SESSION_TOOL or not operation["mutating"]:
            raise PermissionError("A browser session IO grant is required")
        session = cursor.execute(
            "SELECT * FROM project_browser_sessions WHERE operation_id=? FOR UPDATE",
            (grant.operation_id,),
        ).fetchone()
        return project, operation, dict(session) if session else None

    def _access(self, cursor, access, grant, *, effect=False):
        self.projects._scope_lock(cursor, access)
        self.projects._get(cursor, access, grant.project_id)
        project, operation, session = self._owned(cursor, grant)
        if (operation["actor_principal_id"] != access.principal_id
                or not project["bot_native_user_id"]
                or project["bot_native_user_id"] != access.scope.bot_native_user_id):
            raise PermissionError("Browser belongs to another actor or account")
        # Even a snapshot can reveal this editor's logged-in page or write profile
        # data. Group membership alone is not browser access.
        self.projects._editor(cursor, access, project)
        if effect:
            self._live(project, operation)
            require_running_turn(cursor, access, operation["journal_turn_id"])
        return project, operation, session

    @staticmethod
    def _live(project, operation):
        if (operation["state"] not in ACTIVE_STATES
                or project["status"] != "active"
                or project["sandbox_id"] != operation["sandbox_id"]
                or project["workspace_generation"] != operation["generation"]
                or project["lifecycle_fence"] != operation["fence"]
                or project["lifecycle_job_id"] is not None
                or project["locked_task_id"] is not None):
            raise ProjectConflict("Browser workspace was replaced, fenced or is not active")

    @staticmethod
    def _binding(project, operation, session):
        # The final lifecycle verification owns the next fence without mutating
        # the helper. The settled physical epoch is one behind the DB journal.
        return binding({"instance_id": session["instance_id"], "owner": project["scope_key"],
                        "project_id": project["project_id"], "generation": operation["generation"],
                        "fence": operation["fence"] - 1})

    def launch_request(self, access, grant):
        with self.projects._transaction() as cursor:
            project, operation, session = self._access(cursor, access, grant, effect=True)
            if (session is None or session["session_id"] is not None
                    or operation["state"] not in {"dispatched", "needs_attention"}):
                raise ProjectConflict("Browser launch is not awaiting dispatch")
            target = self._target(project, operation, session)
            target.pop("session_id")
            return {**target, "launch_id": session["launch_id"]}

    def _target(self, project, operation, session):
        identity = self._binding(project, operation, session)
        return {"expected_instance_id": identity.pop("instance_id"), **identity,
                "session_id": session["session_id"]}

    def bind(self, access, grant, instance_id, *, now=None):
        """Freeze runner identity and launch ID before dispatching the IO grant."""
        if not _hex(instance_id, 32):
            raise ValueError("Browser requires a runner instance identity")
        now = int(time.time()) if now is None else now
        with self.projects._transaction() as cursor:
            _, operation, session = self._access(cursor, access, grant)
            if session is not None:
                if session["instance_id"] != instance_id:
                    raise ProjectConflict("Browser runner instance changed")
                return session
            if operation["state"] != "reserved":
                raise ProjectConflict("Bind a browser before dispatching its session")
            self._access(cursor, access, grant, effect=True)
            return dict(cursor.execute(
                """INSERT INTO project_browser_sessions(operation_id,instance_id,launch_id,created_at,updated_at)
                VALUES (?,?,?,?,?) RETURNING *""",
                (grant.operation_id, instance_id, uuid.uuid4().hex, now, now),
            ).fetchone())

    def session(self, access, grant):
        with self.projects._transaction() as cursor:
            project, operation, session = self._access(cursor, access, grant)
            if session is None:
                raise ProjectConflict("Browser session has not been bound")
            return {**session, "target": self._target(project, operation, session),
                    "operation_state": operation["state"], "journal_turn_id": operation["journal_turn_id"]}

    def _remote(self, project, operation, session, record, *, sequence=None):
        expected = self._binding(project, operation, session)
        if (not isinstance(record, dict) or len(encoded(record)) > FRAME_LIMIT + 8192
                or type(record.get("schema")) is not int or record["schema"] != 1
                or record.get("binding") != expected
                or binding(record["binding"]) != expected
                or record.get("launch_id") != session["launch_id"]
                or not _hex(record.get("session_id"), 32)
                or not _hex(record.get("config_sha256"), 64)
                or record.get("state") not in {"starting", "ready", "needs_attention", "closing", "closed"}
                or type(record.get("sequence")) is not int
                or record["sequence"] != (session["sequence"] if sequence is None else sequence)
                or (record.get("worker_id") is not None and not _hex(record["worker_id"], 32))):
            raise ProjectConflict("Browser session evidence does not match its launch")
        runtime = record.get("runtime")
        if (not isinstance(runtime, dict) or set(runtime) != {"backend", "sandbox_id", "runtime_id"}
                or runtime["backend"] != project["backend"]
                or runtime["sandbox_id"] != operation["sandbox_id"]):
            raise ProjectConflict("Browser runtime does not match its project")
        runtime_id = runtime["runtime_id"]
        valid_id = _hex(runtime_id, 64) if runtime["backend"] == "oci" else False
        if runtime["backend"] == "vm" and isinstance(runtime_id, str):
            try:
                valid_id = str(uuid.UUID(runtime_id)) == runtime_id
            except ValueError:
                pass
        if not valid_id:
            raise ProjectConflict("Browser runtime needs an immutable process identity")
        for key in ("session_id", "worker_id", "runtime", "config_sha256"):
            if session[key] is not None and session[key] != record[key]:
                raise ProjectConflict("Browser session or process identity changed")
        if record["state"] == "ready" and record["worker_id"] is None:
            raise ProjectConflict("Browser worker has not completed its handshake")

    def opened(self, grant, record, *, now=None):
        """Persist a runner handshake, including uncertain launches for recovery."""
        now = int(time.time()) if now is None else now
        with self.projects._transaction() as cursor:
            project, operation, session = self._owned(cursor, grant)
            if (session is None or session["sequence"] != 0
                    or operation["state"] not in {"dispatched", "needs_attention"}
                    or session["state"] not in {"opening", "ready", "needs_attention"}):
                raise ProjectConflict("No browser launch is awaiting observation")
            self._remote(project, operation, session, record)
            if record.get("last") is not None:
                raise ProjectConflict("Browser performed an unadmitted action")
            state = "ready" if record["state"] == "ready" else "needs_attention"
            cursor.execute(
                """UPDATE project_browser_sessions SET session_id=?,worker_id=?,runtime=?::jsonb,
                config_sha256=?,state=?,updated_at=? WHERE operation_id=?""",
                (record["session_id"], record["worker_id"], json.dumps(record["runtime"]),
                 record["config_sha256"], state, now, grant.operation_id),
            )

    def require_ready(self, access, grant, record):
        with self.projects._transaction() as cursor:
            project, operation, session = self._access(cursor, access, grant, effect=True)
            if session is None or session["state"] != "ready":
                raise ProjectConflict("Browser is not ready for a new action")
            self._remote(project, operation, session, record)
            if record["state"] != "ready":
                raise ProjectConflict("Browser stopped or became uncertain; drain before another session")
            if session["sequence"]:
                row = cursor.execute(
                    "SELECT * FROM project_browser_calls WHERE operation_id=? AND sequence=?",
                    (grant.operation_id, session["sequence"]),
                ).fetchone()
                if row is None or row["state"] != "acknowledged":
                    raise ProjectConflict("Previous browser action is unpublished")
                self._last(record, row)
            elif record.get("last") is not None:
                raise ProjectConflict("Browser performed an unadmitted action")

    def _prior(self, cursor, access, request_key, method, arguments_sha256):
        rows = cursor.execute(
            """SELECT c.*,io.project_id,io.token FROM project_browser_calls c
            JOIN project_io io USING(operation_id) JOIN projects p USING(project_id)
            WHERE p.scope_key=? AND p.bot_native_user_id=? AND io.actor_principal_id=?
            AND c.request_key=? LIMIT 2""",
            (access.scope.key, access.scope.bot_native_user_id, access.principal_id, request_key),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1 or rows[0]["method"] != method or rows[0]["arguments_sha256"] != arguments_sha256:
            raise ProjectConflict("Browser action identity was reused")
        row = rows[0]
        call = ProjectBrowserCall(ProjectIOGrant(row["operation_id"], row["project_id"], row["token"]), row["sequence"])
        self._access(cursor, access, call.grant)
        return call

    def find(self, access, request_key, method, args):
        if not _hex(request_key, 64):
            raise ValueError("Invalid browser request identity")
        hashed = action_digest(method, args)
        with self.projects._transaction() as cursor:
            self.projects._scope_lock(cursor, access)
            return self._prior(cursor, access, request_key, method, hashed)

    def current(self, access, project_id, turn_id, native_message_id):
        """Reuse only this actor's browser in this exact, still-running turn."""
        with self.projects._transaction() as cursor:
            self.projects._scope_lock(cursor, access)
            self.projects._get(cursor, access, project_id)
            require_running_turn(cursor, access, turn_id, native_message_id)
            row = cursor.execute(
                """SELECT * FROM project_io WHERE project_id=? AND actor_principal_id=?
                AND tool_name=? AND state IN ('reserved','dispatched','needs_attention')""",
                (project_id, access.principal_id, BROWSER_SESSION_TOOL),
            ).fetchone()
            if row is None:
                return None
            grant = self.io._grant(row)
            self._access(cursor, access, grant, effect=True)
            if row["journal_turn_id"] != turn_id:
                raise ProjectConflict("Browser belongs to another turn; await its save and drain")
            return grant

    def admit(self, access, grant, request_key, method, args, *, now=None):
        if not _hex(request_key, 64):
            raise ValueError("Invalid browser request identity")
        hashed = action_digest(method, args)
        now = int(time.time()) if now is None else now
        with self.projects._transaction() as cursor:
            project, operation, session = self._access(cursor, access, grant)
            prior = self._prior(cursor, access, request_key, method, hashed)
            if prior is not None:
                if prior.grant != grant:
                    raise ProjectConflict("Browser action belongs to a different session")
                return prior
            self._access(cursor, access, grant, effect=True)
            focus = self.projects._current(cursor, access, now)
            # The initial action was already admitted with the session. A crash
            # before launch must not retarget it to a subsequently selected project.
            first_admitted = (session is not None and session["sequence"] == 0
                              and operation["request_key"] == request_key
                              and operation["arguments_sha256"] == hashed)
            if not first_admitted and (focus is None or focus["project_id"] != grant.project_id):
                raise ProjectConflict("Current project changed or expired")
            if (session is None or session["state"] != "ready"
                    or operation["state"] not in {"dispatched", "needs_attention"}
                    or session["sequence"] >= 2**53 - 1):
                raise ProjectConflict("Browser session is not ready for actions")
            if cursor.execute(
                "SELECT 1 FROM project_browser_calls WHERE operation_id=? AND state<>'acknowledged' LIMIT 1",
                (grant.operation_id,),
            ).fetchone():
                raise ProjectConflict("Reconcile the previous browser action before continuing")
            sequence = session["sequence"] + 1
            cursor.execute(
                """INSERT INTO project_browser_calls(operation_id,sequence,request_key,method,arguments_sha256,
                created_at,updated_at) VALUES (?,?,?,?,?,?,?)""",
                (grant.operation_id, sequence, request_key, method, hashed, now, now),
            )
            cursor.execute("UPDATE project_browser_sessions SET sequence=?,updated_at=? WHERE operation_id=?",
                           (sequence, now, grant.operation_id))
            self.projects._touch_focus(cursor, access, grant.project_id, now)
            cursor.execute("UPDATE projects SET last_active_at=? WHERE project_id=?", (now, project["project_id"]))
            return ProjectBrowserCall(grant, sequence)

    def _call(self, cursor, call):
        if not isinstance(call, ProjectBrowserCall) or type(call.sequence) is not int or call.sequence < 1:
            raise PermissionError("A host-owned browser call is required")
        project, operation, session = self._owned(cursor, call.grant)
        row = cursor.execute(
            "SELECT * FROM project_browser_calls WHERE operation_id=? AND sequence=? FOR UPDATE",
            (call.grant.operation_id, call.sequence),
        ).fetchone()
        if session is None or row is None:
            raise ProjectConflict("Browser call does not exist")
        return project, operation, session, dict(row)

    def dispatch(self, access, call, method, args, *, now=None):
        hashed = action_digest(method, args)
        now = int(time.time()) if now is None else now
        with self.projects._transaction() as cursor:
            self._access(cursor, access, call.grant, effect=True)
            project, operation, session, row = self._call(cursor, call)
            if (row["state"] != "reserved" or session["state"] != "ready"
                    or row["method"] != method or row["arguments_sha256"] != hashed
                    or session["sequence"] != call.sequence):
                raise ProjectConflict("Browser action cannot be dispatched again or changed")
            cursor.execute(
                "UPDATE project_browser_calls SET state='dispatched',updated_at=? WHERE operation_id=? AND sequence=?",
                (now, call.grant.operation_id, call.sequence),
            )
            return {**self._target(project, operation, session), "sequence": call.sequence,
                    "action": method, "arguments": arguments(method, args)}

    def observe(self, call, response, *, now=None):
        now = int(time.time()) if now is None else now
        with self.projects._transaction() as cursor:
            project, operation, session, row = self._call(cursor, call)
            expected = {"schema": 1, "binding": self._binding(project, operation, session),
                        "worker_id": session["worker_id"], "sequence": call.sequence,
                        "arguments_sha256": row["arguments_sha256"]}
            if (not isinstance(response, dict) or len(encoded(response)) > FRAME_LIMIT
                    or any(type(response.get(k)) is not type(v) or response[k] != v for k, v in expected.items())
                    or binding(response["binding"]) != expected["binding"]
                    or response.get("state") not in {"completed", "needs_attention"}
                    or set(response) != {*expected, "state", "result" if response["state"] == "completed" else "error"}
                    or (response["state"] == "needs_attention" and response["error"] != "browser_outcome_unknown")):
                raise ProjectConflict("Browser result does not match its action")
            if row["state"] in {"reserved", "aborted"}:
                raise ProjectConflict("Browser action was not dispatched")
            if row["response"] is not None:
                if row["response"] != response:
                    raise ProjectConflict("Browser response changed on replay")
                return
            if session["sequence"] != call.sequence or session["state"] not in {"ready", "needs_attention"}:
                raise ProjectConflict("Browser session is no longer accepting observations")
            state = "received" if response["state"] == "completed" else "needs_attention"
            cursor.execute(
                """UPDATE project_browser_calls SET state=?,response=?::jsonb,response_sha256=?,updated_at=?
                WHERE operation_id=? AND sequence=?""",
                (state, json.dumps(response, allow_nan=False), digest(response), now, call.grant.operation_id, call.sequence),
            )

    def receipt(self, access, call):
        with self.projects._transaction() as cursor:
            self._access(cursor, access, call.grant)
            _, _, _, row = self._call(cursor, call)
            return row

    def pending(self, access, grant):
        with self.projects._transaction() as cursor:
            self._access(cursor, access, grant)
            row = cursor.execute(
                """SELECT sequence FROM project_browser_calls WHERE operation_id=?
                AND state NOT IN ('acknowledged','aborted')""",
                (grant.operation_id,),
            ).fetchone()
            return ProjectBrowserCall(grant, row["sequence"]) if row else None

    def abandon_reserved(self, access, call, *, now=None):
        """A never-dispatched action may be abandoned only by draining its session."""
        now = int(time.time()) if now is None else now
        with self.projects._transaction() as cursor:
            self._access(cursor, access, call.grant)
            _, _, session, row = self._call(cursor, call)
            if row["state"] not in {"reserved", "aborted"} or session["sequence"] != call.sequence:
                raise ProjectConflict("Dispatched browser effects cannot be abandoned")
            cursor.execute(
                "UPDATE project_browser_calls SET state='aborted',updated_at=? WHERE operation_id=? AND sequence=?",
                (now, call.grant.operation_id, call.sequence),
            )
            if session["state"] != "closed":
                cursor.execute("UPDATE project_browser_sessions SET state='draining',updated_at=? WHERE operation_id=?",
                               (now, call.grant.operation_id))

    def ack_request(self, access, call):
        with self.projects._transaction() as cursor:
            self._access(cursor, access, call.grant)
            project, operation, session, row = self._call(cursor, call)
            if row["state"] not in {"received", "acknowledged"} or session["sequence"] != call.sequence:
                raise ProjectConflict("Commit a completed browser response before acknowledging it")
            return {**self._target(project, operation, session), "sequence": call.sequence,
                    "response_sha256": row["response_sha256"]}

    @staticmethod
    def _last(record, row):
        expected = {"arguments_sha256": row["arguments_sha256"], "method": row["method"],
                    "state": "completed", "acknowledged": True, "response": row["response"]}
        last = record.get("last")
        if (last != expected or not isinstance(last, dict)
                or last.get("acknowledged") is not True
                or row["state"] not in {"received", "acknowledged"}
                or row["response"] is None or digest(last["response"]) != row["response_sha256"]):
            raise ProjectConflict("Runner did not acknowledge the committed browser response")

    def acknowledged(self, call, record, *, now=None):
        now = int(time.time()) if now is None else now
        with self.projects._transaction() as cursor:
            project, operation, session, row = self._call(cursor, call)
            self._remote(project, operation, session, record)
            if record["sequence"] != call.sequence:
                raise ProjectConflict("Browser ACK belongs to another action")
            self._last(record, row)
            cursor.execute(
                "UPDATE project_browser_calls SET state='acknowledged',updated_at=? WHERE operation_id=? AND sequence=?",
                (now, call.grant.operation_id, call.sequence),
            )

    def start_drain(self, access, grant, *, now=None):
        now = int(time.time()) if now is None else now
        with self.projects._transaction() as cursor:
            project, operation, session = self._access(cursor, access, grant)
            self._live(project, operation)
            if session is None or session["session_id"] is None:
                raise ProjectConflict("Recover the browser launch before draining it")
            if cursor.execute(
                "SELECT 1 FROM project_browser_calls WHERE operation_id=? AND state NOT IN ('acknowledged','aborted') LIMIT 1",
                (grant.operation_id,),
            ).fetchone():
                raise ProjectConflict("Browser actions must be reconciled before drain")
            if session["state"] != "closed":
                cursor.execute("UPDATE project_browser_sessions SET state='draining',updated_at=? WHERE operation_id=?",
                               (now, grant.operation_id))
            return self._target(project, operation, session)

    def drained(self, grant, record, *, now=None):
        now = int(time.time()) if now is None else now
        with self.projects._transaction() as cursor:
            project, operation, session = self._owned(cursor, grant)
            if session is None or session["state"] not in {"draining", "closed"}:
                raise ProjectConflict("No browser shutdown intent to observe")
            row = cursor.execute(
                """SELECT * FROM project_browser_calls WHERE operation_id=? AND state<>'aborted'
                ORDER BY sequence DESC LIMIT 1""",
                (grant.operation_id,),
            ).fetchone()
            self._remote(project, operation, session, record, sequence=row["sequence"] if row else 0)
            if record["state"] != "closed":
                raise ProjectConflict("Runner has not confirmed browser shutdown")
            if row is not None:
                if row["state"] != "acknowledged":
                    raise ProjectConflict("Browser still has an unpublished response")
                self._last(record, row)
            elif record.get("last") is not None:
                raise ProjectConflict("Browser performed an unadmitted action")
            if session["state"] != "closed":
                cursor.execute(
                    "UPDATE project_browser_sessions SET state='closed',closed_evidence=?::jsonb,updated_at=? WHERE operation_id=?",
                    (json.dumps(record, allow_nan=False), now, grant.operation_id),
                )
