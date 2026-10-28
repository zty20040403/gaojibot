"""Durable, host-only admission for existing project sandbox tools.

This store never runs commands. A runtime adapter must persist dispatch intent,
call the existing sandbox executor, and provide observed quiescence before
releasing a reservation. There is deliberately no time-based lock expiry.
"""

from dataclasses import dataclass
import json
import re
import time
import uuid

from .projects import ProjectConflict, require_project_io_idle
from .project_turn_saves import require_running_turn


READ_TOOLS = frozenset(
    {"sandbox_read_file", "send_file_from_sandbox", "send_image_from_sandbox"}
)
WRITE_TOOLS = frozenset({"sandbox_exec", "sandbox_write_file"})
BROWSER_SESSION_TOOL = "project_browser_session"
MUTATING_TOOLS = WRITE_TOOLS | {BROWSER_SESSION_TOOL}
ACTIVE_STATES = frozenset({"reserved", "dispatched", "needs_attention"})


@dataclass(frozen=True)
class ProjectIOGrant:
    operation_id: int
    project_id: int
    token: str


class ProjectIOStore:
    def __init__(self, projects, usage):
        if usage.projects is not projects:
            raise ValueError("Project accounting must use the same transaction owner")
        self.projects, self.usage = projects, usage

    @staticmethod
    def _grant(row):
        return ProjectIOGrant(row["operation_id"], row["project_id"], row["token"])

    def _prior(self, cursor, access, request_key, tool_name, arguments_sha256):
        rows = cursor.execute(
            """SELECT io.* FROM project_io io JOIN projects p USING(project_id)
            WHERE p.scope_key=? AND p.bot_native_user_id=?
            AND io.actor_principal_id=? AND io.request_key=? LIMIT 2""",
            (access.scope.key, access.scope.bot_native_user_id, access.principal_id, request_key),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1 or rows[0]["tool_name"] != tool_name or rows[0]["arguments_sha256"] != arguments_sha256:
            raise ProjectConflict("Project operation identity was reused")
        self.projects._get(cursor, access, rows[0]["project_id"])
        return rows[0]

    def find(self, access, request_key, tool_name, arguments_sha256):
        """Resolve a replay before consulting mutable current-project focus."""
        with self.projects._transaction() as cursor:
            self.projects._scope_lock(cursor, access)
            prior = self._prior(cursor, access, request_key, tool_name, arguments_sha256)
            return self._grant(prior) if prior else None

    def pending(self, access, project_id):
        with self.projects._transaction() as cursor:
            self.projects._scope_lock(cursor, access)
            self.projects._get(cursor, access, project_id)
            row = cursor.execute(
                """SELECT * FROM project_io WHERE project_id=? AND actor_principal_id=?
                AND state IN ('dispatched','needs_attention')""",
                (project_id, access.principal_id),
            ).fetchone()
            return self._grant(row) if row else None

    def status(self, access, project_id):
        with self.projects._transaction() as cursor:
            self.projects._scope_lock(cursor, access)
            self.projects._get(cursor, access, project_id)
            row = cursor.execute(
                """SELECT operation_id,tool_name,state,updated_at,result IS NOT NULL AS result_recorded
                FROM project_io WHERE project_id=? AND state IN ('reserved','dispatched','needs_attention')""",
                (project_id,),
            ).fetchone()
            return dict(row) if row else None

    def admit(
        self,
        access,
        project_id,
        tool_name,
        *,
        request_key,
        arguments_sha256,
        expected_version,
        expected_bytes=0,
        installing_dependencies=False,
        journal_turn_id=None,
        native_message_id=None,
        now=None,
    ):
        if (
            not isinstance(tool_name, str)
            or tool_name not in READ_TOOLS | MUTATING_TOOLS
            or any(
                not isinstance(v, str) or not re.fullmatch(r"[a-f0-9]{64}", v)
                for v in (request_key, arguments_sha256)
            )
            or type(expected_bytes) is not int
            or expected_bytes < 0
            or type(expected_version) is not int
            or expected_version <= 0
            or type(installing_dependencies) is not bool
            or (tool_name in READ_TOOLS and (expected_bytes or installing_dependencies))
            or (tool_name != "sandbox_exec" and installing_dependencies)
        ):
            raise ValueError("Invalid project tool admission")
        now = int(time.time()) if now is None else now
        mutating = tool_name in MUTATING_TOOLS
        with self.projects._transaction() as cursor:
            self.projects._scope_lock(cursor, access)
            project = self.projects._get(cursor, access, project_id)
            if (
                not project["bot_native_user_id"]
                or project["bot_native_user_id"] != access.scope.bot_native_user_id
            ):
                raise PermissionError("Project requires an account binding")
            prior = self._prior(cursor, access, request_key, tool_name, arguments_sha256)
            if prior:
                if prior["project_id"] != project_id:
                    raise ProjectConflict("Project operation is bound to another workspace")
                return self._grant(prior)
            if cursor.execute(
                "SELECT 1 FROM project_io WHERE project_id=? AND request_key=?",
                (project_id, request_key),
            ).fetchone():
                raise ProjectConflict("Project operation belongs to another actor")
            if mutating:
                self.projects._editor(cursor, access, project)
            if mutating or journal_turn_id is not None:
                if native_message_id is None:
                    raise ProjectConflict("Project turn requires its trigger message")
                require_running_turn(cursor, access, journal_turn_id, native_message_id)
            require_project_io_idle(cursor, project_id)
            focus = self.projects._current(cursor, access, now)
            if focus is None or focus["project_id"] != project_id:
                raise ProjectConflict("Current project changed or expired")
            if (
                project["version"] != expected_version
                or project["status"] != "active"
                or not project["sandbox_id"]
                or project["workspace_generation"] <= 0
                or project["lifecycle_job_id"] is not None
                or project["locked_task_id"] is not None
            ):
                raise ProjectConflict("Project workspace is not ready for tools")
            # Shell commands have unknown write size. Reserve all remaining
            # project capacity at the scope level, not a made-up byte estimate.
            reserved = (
                max(0, project["quota_bytes"] - project["used_bytes"])
                if tool_name in {"sandbox_exec", BROWSER_SESSION_TOOL}
                else expected_bytes
            )
            if mutating:
                self.usage._require(
                    cursor,
                    access,
                    project,
                    expected_bytes=expected_bytes,
                    installing_dependencies=installing_dependencies,
                    scope_reservation=reserved,
                    now=now,
                )
            row = cursor.execute(
                """INSERT INTO project_io(project_id,actor_principal_id,request_key,token,
                tool_name,arguments_sha256,sandbox_id,generation,fence,mutating,reserved_bytes,created_at,updated_at,journal_turn_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING *""",
                (
                    project_id,
                    access.principal_id,
                    request_key,
                    uuid.uuid4().hex,
                    tool_name,
                    arguments_sha256,
                    project["sandbox_id"],
                    project["workspace_generation"],
                    project["lifecycle_fence"],
                    mutating,
                    reserved,
                    now,
                    now,
                    journal_turn_id,
                ),
            ).fetchone()
            if mutating:
                cursor.execute(
                    """INSERT INTO project_turn_saves(project_id,turn_id,actor_principal_id,created_at,updated_at)
                    VALUES (?,?,?,?,?) ON CONFLICT(project_id,turn_id) DO UPDATE
                    SET state='pending',job_id=NULL,error_code='',next_attempt_at=0,
                    updated_at=EXCLUDED.updated_at WHERE project_turn_saves.state<>'pending'""",
                    (project_id, journal_turn_id, access.principal_id, now, now),
                )
            self.projects._touch_focus(cursor, access, project_id, now)
            cursor.execute(
                "UPDATE projects SET last_active_at=? WHERE project_id=?",
                (now, project_id),
            )
            self.projects._event(
                cursor,
                project_id,
                access,
                "tool_reserved",
                {"operation_id": row["operation_id"], "tool": tool_name},
                now,
            )
            return self._grant(row)

    def _owned(self, cursor, grant):
        if not isinstance(grant, ProjectIOGrant):
            raise PermissionError("A host-owned project operation grant is required")
        scope = cursor.execute(
            "SELECT scope_key FROM projects WHERE project_id=?", (grant.project_id,)
        ).fetchone()
        if scope is None:
            raise PermissionError("Project operation grant does not match")
        # State changes and usage invalidation are atomic with peer admission:
        # scope first, then project, then operation.
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(?,0))",
            ("project-scope:" + scope["scope_key"],),
        )
        project = cursor.execute(
            "SELECT * FROM projects WHERE project_id=? FOR UPDATE", (grant.project_id,)
        ).fetchone()
        row = cursor.execute(
            "SELECT * FROM project_io WHERE operation_id=? AND project_id=? FOR UPDATE",
            (grant.operation_id, grant.project_id),
        ).fetchone()
        if project is None or row is None or row["token"] != grant.token:
            raise PermissionError("Project operation grant does not match")
        return dict(project), dict(row)

    def dispatch(self, access, grant, *, now=None):
        """Return the frozen physical target exactly once, before starting I/O."""
        now = int(time.time()) if now is None else now
        with self.projects._transaction() as cursor:
            self.projects._scope_lock(cursor, access)
            project = self.projects._get(cursor, access, grant.project_id)
            if (
                not project["bot_native_user_id"]
                or project["bot_native_user_id"] != access.scope.bot_native_user_id
            ):
                raise PermissionError("Project account binding changed")
            _, row = self._owned(cursor, grant)
            if row["actor_principal_id"] != access.principal_id:
                raise PermissionError("Project operation belongs to another actor")
            if row["mutating"]:
                self.projects._editor(cursor, access, project)
            if row["journal_turn_id"] is not None:
                require_running_turn(cursor, access, row["journal_turn_id"])
            if (
                row["state"] != "reserved"
                or project["status"] != "active"
                or project["lifecycle_job_id"] is not None
                or project["locked_task_id"] is not None
                or project["sandbox_id"] != row["sandbox_id"]
                or project["workspace_generation"] != row["generation"]
                or project["lifecycle_fence"] != row["fence"]
            ):
                raise ProjectConflict(
                    "Project operation cannot be dispatched again or to a changed workspace"
                )
            if row["tool_name"] == BROWSER_SESSION_TOOL and not cursor.execute(
                "SELECT 1 FROM project_browser_sessions WHERE operation_id=? AND state='opening'",
                (grant.operation_id,),
            ).fetchone():
                raise ProjectConflict("Bind the browser launch before dispatch")
            cursor.execute(
                "UPDATE project_io SET state='dispatched',updated_at=? WHERE operation_id=?",
                (now, grant.operation_id),
            )
            return {
                "owner": project["scope_key"],
                "sandbox_id": row["sandbox_id"],
                "project_id": grant.project_id,
                "generation": row["generation"],
                "fence": row["fence"],
            }

    def uncertain(self, grant, *, now=None):
        now = int(time.time()) if now is None else now
        with self.projects._transaction() as cursor:
            _, row = self._owned(cursor, grant)
            if row["state"] not in {"dispatched", "needs_attention"}:
                raise ProjectConflict(
                    "Only dispatched operations have uncertain effects"
                )
            cursor.execute(
                "UPDATE project_io SET state='needs_attention',updated_at=? WHERE operation_id=?",
                (now, grant.operation_id),
            )

    def abort(self, grant, *, now=None, drain_job=None):
        """Safe only before dispatch intent; never cancels an already running command."""
        now = int(time.time()) if now is None else now
        with self.projects._transaction() as cursor:
            project, row = self._owned(cursor, grant)
            self._drain_guard(cursor, project, row, grant, drain_job)
            if row["state"] not in {"reserved", "aborted"}:
                raise ProjectConflict(
                    "Dispatched effects need reconciliation, not automatic unlock"
                )
            cursor.execute(
                "UPDATE project_io SET state='aborted',updated_at=? WHERE operation_id=?",
                (now, grant.operation_id),
            )

    @staticmethod
    def _validate_result(result):
        if (
            not isinstance(result, dict)
            or len(json.dumps(result, allow_nan=False).encode()) > 512 * 1024
        ):
            raise ValueError("Project tool receipt exceeds its limit")

    @staticmethod
    def _drain_guard(cursor, project, row, grant, job):
        if job is not None or (row["tool_name"] == BROWSER_SESSION_TOOL and project["lifecycle_job_id"] is not None):
            from .project_browser_drain import require_drain
            require_drain(cursor, project, grant, job)

    def observe_result(self, grant, result, *, now=None, drain_job=None):
        """Persist a returned result without claiming all processes have stopped."""
        self._validate_result(result)
        now = int(time.time()) if now is None else now
        with self.projects._transaction() as cursor:
            project, row = self._owned(cursor, grant)
            self._drain_guard(cursor, project, row, grant, drain_job)
            if row["state"] not in {"dispatched", "needs_attention", "completed"}:
                raise ProjectConflict("No dispatched operation to observe")
            if row["result"] is not None and row["result"] != result:
                raise ProjectConflict("Project result changed on replay")
            cursor.execute(
                "UPDATE project_io SET result=?::jsonb,updated_at=? WHERE operation_id=?",
                (json.dumps(result), now, grant.operation_id),
            )

    def complete(self, grant, result, evidence, *, now=None, drain_job=None):
        """Only the runtime adapter may supply an actual, quiescent helper receipt."""
        self._validate_result(result)
        now = int(time.time()) if now is None else now
        with self.projects._transaction() as cursor:
            project, row = self._owned(cursor, grant)
            self._drain_guard(cursor, project, row, grant, drain_job)
            if row["tool_name"] == BROWSER_SESSION_TOOL:
                browser = cursor.execute(
                    "SELECT state FROM project_browser_sessions WHERE operation_id=?",
                    (grant.operation_id,),
                ).fetchone()
                if browser is None or browser["state"] != "closed":
                    raise ProjectConflict("Browser shutdown has not been durably observed")
                if cursor.execute(
                    """SELECT 1 FROM project_browser_calls WHERE operation_id=?
                    AND state NOT IN ('acknowledged','aborted') LIMIT 1""",
                    (grant.operation_id,),
                ).fetchone():
                    raise ProjectConflict("Browser still has unpublished actions")
            if row["result"] is not None and row["result"] != result:
                raise ProjectConflict("Project result changed on replay")
            expected = {
                "sandbox_id": row["sandbox_id"],
                "project_id": grant.project_id,
                "generation": row["generation"],
                "fence": row["fence"],
                "pending": False,
                "quiescent": True,
            }
            if (
                not isinstance(evidence, dict)
                or evidence != expected
                or any(type(evidence[k]) is not type(v) for k, v in expected.items())
            ):
                raise ProjectConflict(
                    "Project completion lacks matching quiescent evidence"
                )
            if row["state"] == "completed":
                if row["result"] != result or row["evidence"] != evidence:
                    raise ProjectConflict("Project receipt changed on replay")
                return
            if (
                row["state"] not in {"dispatched", "needs_attention"}
                or project["sandbox_id"] != row["sandbox_id"]
                or project["workspace_generation"] != row["generation"]
                or project["lifecycle_fence"] != row["fence"]
            ):
                raise ProjectConflict(
                    "Cannot settle an undispatched or replaced workspace"
                )
            cursor.execute(
                """UPDATE project_io SET state='completed',result=?::jsonb,evidence=?::jsonb,updated_at=?
                           WHERE operation_id=?""",
                (json.dumps(result), json.dumps(evidence), now, grant.operation_id),
            )
            cursor.execute(
                "UPDATE projects SET last_active_at=?,updated_at=?,version=version+? WHERE project_id=?",
                (now, now, int(row["mutating"]), grant.project_id),
            )
            if row["mutating"]:
                cursor.execute(
                    "UPDATE project_usage SET complete=FALSE,error='tool_write_requires_sample' WHERE project_id=?",
                    (grant.project_id,),
                )

    def receipt(self, access, grant, *, drain_job=None):
        with self.projects._transaction() as cursor:
            self.projects._scope_lock(cursor, access)
            project = self.projects._get(cursor, access, grant.project_id)
            if (
                not project["bot_native_user_id"]
                or project["bot_native_user_id"] != access.scope.bot_native_user_id
            ):
                raise PermissionError("Project account binding changed")
            _, row = self._owned(cursor, grant)
            if drain_job is not None:
                from .project_browser_drain import require_drain
                require_drain(cursor, project, grant, drain_job, access=access)
                self.projects._editor(cursor, access, project)
            elif row["actor_principal_id"] != access.principal_id:
                raise PermissionError("Project operation belongs to another actor")
            return {
                "operation_id": row["operation_id"],
                "state": row["state"],
                "tool": row["tool_name"],
                "result": row["result"],
                "updated_at": row["updated_at"],
            }
