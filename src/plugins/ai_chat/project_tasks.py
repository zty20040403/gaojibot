"""Host-only admission and immutable inputs for project Sub-Agent tasks.

The caller must resolve live membership and sandbox admission. This component
does not enqueue work or mount project volumes. Runtime wiring and accepted
artifact publication must be installed together before enabling this path.
"""

import hashlib
import json
import re
import time

from src.project_content import manifest_bytes, project_path
from .project_lifecycle import ProjectLifecycle
from .projects import ProjectConflict, require_project_io_idle


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class ProjectTaskStore:
    def __init__(self, projects, tasks):
        if projects.database is not tasks.database:
            raise ValueError("Project tasks require the same database transaction owner")
        self.projects, self.tasks = projects, tasks

    def _prior(self, cursor, access, request_key):
        rows = cursor.execute(
            """SELECT b.* FROM project_task_bindings b JOIN projects p USING(project_id)
            WHERE p.scope_key=? AND p.bot_native_user_id=? AND b.actor_principal_id=?
            AND b.request_key=? LIMIT 2""",
            (access.scope.key, access.scope.bot_native_user_id, access.principal_id, request_key),
        ).fetchall()
        if len(rows) > 1:
            raise ProjectConflict("Project task request has ambiguous ownership")
        return dict(rows[0]) if rows else None

    def _owned(self, cursor, access, task_id):
        binding = cursor.execute(
            """SELECT b.* FROM project_task_bindings b JOIN projects p USING(project_id)
            WHERE b.task_id=? AND p.scope_key=? FOR UPDATE OF b""",
            (task_id, access.scope.key),
        ).fetchone()
        if binding is None:
            raise PermissionError("Project task is not available in this conversation")
        project = self.projects._get(cursor, access, binding["project_id"])
        self.projects._editor(cursor, access, project)
        if (binding["actor_principal_id"] != access.principal_id
                or not project["bot_native_user_id"]
                or project["bot_native_user_id"] != access.scope.bot_native_user_id):
            raise PermissionError("Project task belongs to another execution identity")
        task = cursor.execute("SELECT * FROM subagent_tasks WHERE task_id=? FOR UPDATE", (task_id,)).fetchone()
        if (task is None or task["scope_key"] != access.scope.key
                or str(task["requester_user_id"]) != access.scope.actor_native_user_id
                or task["project_id"] != binding["project_id"]
                or task["project_base_version"] != binding["base_version"]):
            raise ProjectConflict("Project task binding changed")
        return project, dict(binding), task

    @staticmethod
    def _clean_baseline(cursor, project, baseline):
        if project["status"] not in {"stopped", "hibernated", "archived"}:
            raise ProjectConflict("Save and quiesce the project before starting its task")
        if project["workspace_generation"] == 0:
            return
        # A manifest alone cannot prove a live workspace was saved. Require the
        # latest exact-generation lifecycle's final verification and snapshot.
        run = cursor.execute(
            """SELECT * FROM project_lifecycle_runs WHERE project_id=?
            ORDER BY job_id DESC LIMIT 1""", (project["project_id"],),
        ).fetchone()
        if (run is None or run["status"] != "completed"
                or run["generation"] != project["workspace_generation"]
                or run["target_state"] != project["status"]):
            raise ProjectConflict("Project has no verified quiescent baseline")
        steps = cursor.execute(
            "SELECT * FROM project_lifecycle_steps WHERE job_id=? ORDER BY step_index", (run["job_id"],),
        ).fetchall()
        expected = run["plan"]["steps"]
        if (run["next_step"] != len(expected) or len(steps) != len(expected) or not steps
                or any(s["status"] != "succeeded" or s["operation"] != expected[i] for i, s in enumerate(steps))
                or steps[-1]["operation"] != "verify_" + project["status"]
                or steps[-1]["arguments"]["fence"] != project["lifecycle_fence"]
                or not any(s["operation"] == "snapshot_workspace" and s["evidence"].get("snapshot") == baseline for s in steps)):
            raise ProjectConflict("Saved version does not match the verified project workspace")

    def prepare(
        self, access, project_id, *, expected_version, base_version, request_key,
        packet, decision, dispatch, max_parallelism, max_steps, now=None,
    ):
        """Atomically save the task, recovery envelope, baseline and project lock.

        Replays use the original request identity before consulting current
        focus. No dispatcher or listener can observe a half-bound task.
        """
        if (not isinstance(request_key, str) or not re.fullmatch(r"[a-f0-9]{64}", request_key)
                or any(type(v) is not int or v <= 0 for v in (project_id, expected_version, base_version))):
            raise ValueError("Invalid project task admission")
        if (packet.scope_key != access.scope.key
                or str(packet.requester_user_id) != access.scope.actor_native_user_id
                or str(dispatch.get("bot_id", "")) != access.scope.bot_native_user_id):
            raise PermissionError("Task context does not match the project execution identity")
        intent = _digest({"project_id": project_id, "base_version": base_version,
            "packet": packet.as_payload(), "decision": decision.as_payload() if decision else None,
            "dispatch": dispatch, "max_parallelism": max_parallelism, "max_steps": max_steps})
        now = int(time.time()) if now is None else now
        with self.tasks._transaction() as cursor:
            self.projects._scope_lock(cursor, access)
            prior = self._prior(cursor, access, request_key)
            if prior:
                _, binding, task = self._owned(cursor, access, prior["task_id"])
                if binding["intent_sha256"] != intent:
                    raise ProjectConflict("Project task request identity was reused")
                return self.tasks._task_row(task)
            project = self.projects._get(cursor, access, project_id, edit=True, expected=expected_version)
            if not project["bot_native_user_id"]:
                raise PermissionError("Project requires a bot account binding")
            focus = self.projects._current(cursor, access, now)
            if focus is None or focus["project_id"] != project_id:
                raise ProjectConflict("Current project changed or expired")
            ProjectLifecycle._idle(cursor, project)
            if cursor.execute("SELECT 1 FROM project_turn_saves WHERE project_id=? AND state='pending' LIMIT 1",
                              (project_id,)).fetchone():
                raise ProjectConflict("Project has an unfinished automatic save")
            baseline = ProjectLifecycle._baseline(cursor, project_id)
            if baseline is None or baseline["version_no"] != base_version:
                raise ProjectConflict("Project task requires its latest saved version")
            self._clean_baseline(cursor, project, baseline)
            self._manifest(cursor, access, project_id, baseline)
            task_row = self._bind(cursor, access, project, baseline, request_key=request_key,
                intent=intent, packet=packet, decision=decision, dispatch=dispatch,
                max_parallelism=max_parallelism, max_steps=max_steps, now=now)
        self.tasks._notify_changed(task_row["task_id"])
        return self.tasks._task_row(task_row)

    def _bind(self, cursor, access, project, baseline, *, request_key, intent,
              packet, decision, dispatch, max_parallelism, max_steps, now):
        """Caller owns admission, the project row lock and the outer transaction."""
        project_id, base_version = project["project_id"], baseline["version_no"]
        task = self.tasks.prepare_task(packet=packet, decision=decision, dispatch=dispatch,
            max_parallelism=max_parallelism, max_steps=max_steps, transaction_cursor=cursor)
        task_row = cursor.execute(
            "UPDATE subagent_tasks SET project_id=?,project_base_version=? WHERE task_id=? RETURNING *",
            (project_id, base_version, task.task_id),
        ).fetchone()
        cursor.execute(
            """INSERT INTO project_task_bindings(task_id,project_id,actor_principal_id,request_key,
            intent_sha256,base_version,manifest_sha256,generation,fence,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (task.task_id, project_id, access.principal_id, request_key, intent, base_version,
             baseline["manifest_sha256"], project["workspace_generation"], project["lifecycle_fence"], now),
        )
        cursor.execute("UPDATE projects SET locked_task_id=?,version=version+1,updated_at=?,last_active_at=? WHERE project_id=?",
                           (task.task_id, now, now, project_id))
        self.projects._touch_focus(cursor, access, project_id, now)
        evidence = {"task_id": task.task_id, "project_id": project_id, **baseline}
        self.projects._event(cursor, project_id, access, "task_bound", evidence, now)
        self.tasks.append_event(task.task_id, "task.project_bound", evidence, transaction_cursor=cursor)
        self.tasks.append_checkpoint(task.task_id, "project_bound", evidence, transaction_cursor=cursor)
        return task_row

    def _manifest(self, cursor, access, project_id, baseline):
        project = self.projects._get(cursor, access, project_id)
        document = self.projects._manifest(cursor, access, project_id, baseline["version_no"])
        if hashlib.sha256(manifest_bytes(document, quota_bytes=project["quota_bytes"])).hexdigest() != baseline["manifest_sha256"]:
            raise ProjectConflict("Project baseline manifest changed")
        return document

    def _baseline(self, cursor, access, task_id):
        project, binding, task = self._owned(cursor, access, task_id)
        require_project_io_idle(cursor, project["project_id"])
        if (project["locked_task_id"] != task_id or project["lifecycle_job_id"] is not None
                or project["workspace_generation"] != binding["generation"]
                or project["lifecycle_fence"] != binding["fence"]
                or task["cancel_requested"] or task["status"] in {"completed", "partial", "failed", "cancelled"}):
            raise ProjectConflict("Project task is not active on its admitted workspace")
        document = self._manifest(cursor, access, binding["project_id"],
                                  {"version_no": binding["base_version"], "manifest_sha256": binding["manifest_sha256"]})
        return binding, document

    def baseline(self, access, task_id):
        """Private immutable input index, never an arbitrary workspace mount."""
        with self.tasks._transaction() as cursor:
            self.projects._scope_lock(cursor, access)
            binding, document = self._baseline(cursor, access, task_id)
            return {"project_id": binding["project_id"], "version_no": binding["base_version"],
                    "manifest_sha256": binding["manifest_sha256"], "manifest": document}

    def require_step(self, access, task_id, step_key):
        with self.tasks._transaction() as cursor:
            self.projects._scope_lock(cursor, access)
            _, _, task = self._owned(cursor, access, task_id)
            self._baseline(cursor, access, task_id)
            run = cursor.execute("SELECT 1 FROM subagent_runs WHERE task_id=? AND step_key=? AND status='running'",
                                 (task_id, step_key)).fetchone()
            if task["status"] != "running" or run is None:
                raise ProjectConflict("Project baseline import requires its running task step")
            return task["conversation_id"]

    def read_baseline_file(self, access, task_id, path):
        name = project_path(path)
        with self.tasks._transaction() as cursor:
            self.projects._scope_lock(cursor, access)
            _, document = self._baseline(cursor, access, task_id)
            item = next((item for item in document["files"] if item["path"] == name), None)
            if item is None:
                raise FileNotFoundError(name)
            content = self.projects.blobs.read(item["sha256"])
            if len(content) != item["size"]:
                raise ProjectConflict("Project baseline file size changed")
            return {**item, "content": content}
