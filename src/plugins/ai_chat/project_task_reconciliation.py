"""Recover project task ownership; never discard task files or remote effects."""

import asyncio
import json
import logging
import time

from .agent.external import remote_path, remote_record
from .project_executor import _store
from .project_lifecycle import ProjectLifecycle
from .project_materialization import ProjectMaterializations
from .project_publication import ProjectPublications
from .project_tasks import ProjectTaskStore
from .projects import ProjectConflict, require_project_io_idle

logger = logging.getLogger(__name__)


def _require_settled_external_calls(cursor, task_id):
    for call in cursor.execute("SELECT status,remote_path,response_json FROM subagent_external_calls WHERE task_id=?",
                               (task_id,)).fetchall():
        response = json.loads(call["response_json"])
        record = remote_record(response)
        state = str(record.get("status") or "")
        # 'resolved' means observation ended, not necessarily remote execution.
        known_terminal = state in {"succeeded", "completed", "failed", "cancelled", "expired"}
        if (call["status"] != "resolved" or response.get("pending") or record.get("pending")
                or ((state or call["remote_path"] or remote_path(response)) and not known_terminal)):
            raise ProjectConflict("Project task has unconfirmed remote operations")


class ProjectTaskReleases:
    def __init__(self, bindings):
        self.bindings = bindings
        self.projects, self.tasks = bindings.projects, bindings.tasks

    def release_failed(self, access, task_id):
        """Detach a settled failed task, leaving its independent artifacts alone."""
        now = int(time.time())
        with self.tasks._transaction() as cursor:
            self.projects._scope_lock(cursor, access)
            control = cursor.execute("SELECT revision FROM subagent_controls WHERE task_id=? FOR UPDATE", (task_id,)).fetchone()
            project, binding, task = self.bindings._owned(cursor, access, task_id)
            prior = cursor.execute("SELECT * FROM project_task_releases WHERE task_id=?", (task_id,)).fetchone()
            if control is None:
                raise ProjectConflict("Project task has no revision")
            if prior is not None:
                if prior["revision"] != control["revision"]:
                    raise ProjectConflict("Released task revision changed")
                return dict(prior)
            if cursor.execute("SELECT 1 FROM project_task_publications WHERE task_id=?", (task_id,)).fetchone():
                raise ProjectConflict("Accepted project version must be materialized, not discarded")
            require_project_io_idle(cursor, project["project_id"])
            if (project["locked_task_id"] != task_id or project["lifecycle_job_id"] is not None
                    or project["workspace_generation"] != binding["generation"]
                    or project["lifecycle_fence"] != binding["fence"]
                    or task["status"] not in {"failed", "cancelled"}):
                raise ProjectConflict("Project task has not settled on its admitted workspace")
            baseline = {"version_no": binding["base_version"], "manifest_sha256": binding["manifest_sha256"]}
            if ProjectLifecycle._baseline(cursor, project["project_id"]) != baseline:
                raise ProjectConflict("Project version changed while the failed task owned it")
            self.bindings._clean_baseline(cursor, project, baseline)
            self.bindings._manifest(cursor, access, project["project_id"], baseline)
            if cursor.execute("""SELECT 1 FROM subagent_runs WHERE task_id=?
                AND status NOT IN ('succeeded','partial','failed','cancelled','skipped') LIMIT 1""", (task_id,)).fetchone():
                raise ProjectConflict("Project task still has unfinished steps")
            if cursor.execute("""SELECT 1 FROM durable_jobs WHERE kind='subagent.workflow'
                AND payload_json::jsonb->>'task_id'=? AND status='running' AND lease_until>? LIMIT 1""",
                (str(task_id), time.time())).fetchone():
                raise ProjectConflict("Project task worker still owns an execution lease")
            _require_settled_external_calls(cursor, task_id)
            if cursor.execute("""SELECT 1 FROM project_turn_saves WHERE project_id=? AND state='pending' LIMIT 1""",
                              (project["project_id"],)).fetchone():
                raise ProjectConflict("Project still has an unfinished automatic save")
            receipt = cursor.execute("""INSERT INTO project_task_releases(task_id,revision,task_status,created_at)
                VALUES (?,?,?,?) RETURNING *""", (task_id, control["revision"], task["status"], now)).fetchone()
            cursor.execute("UPDATE projects SET locked_task_id=NULL,version=version+1,updated_at=? WHERE project_id=?",
                           (now, project["project_id"]))
            evidence = {**dict(receipt), "project_id": project["project_id"], **baseline,
                        "project_version_changed": False, "task_artifacts_deleted": False}
            self.projects._event(cursor, project["project_id"], access, "task_released", evidence, now)
            self.tasks.append_checkpoint(task_id, "project_task_released", evidence, transaction_cursor=cursor)
            self.tasks.append_event(task_id, "task.project_released", evidence, transaction_cursor=cursor)
        self.tasks._notify_changed(task_id)
        return dict(receipt)

    def candidates(self, *, after=0, limit=20):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid project task reconciliation page")
        with self.tasks._transaction() as cursor:
            return [dict(row) for row in cursor.execute("""SELECT p.*,b.task_id,b.actor_principal_id,
                (v.task_id IS NOT NULL) AS published
                FROM project_task_bindings b JOIN projects p USING(project_id)
                JOIN subagent_tasks t USING(task_id)
                LEFT JOIN project_task_publications v USING(task_id)
                WHERE b.task_id>? AND p.locked_task_id=b.task_id AND p.lifecycle_job_id IS NULL
                AND (v.task_id IS NOT NULL OR t.status IN ('failed','cancelled'))
                AND NOT EXISTS (SELECT 1 FROM project_task_releases r WHERE r.task_id=b.task_id)
                ORDER BY b.task_id LIMIT ?""", (after, limit)).fetchall()]


class ProjectTaskReconciler:
    def __init__(self, runtime, tasks):
        self.runtime = runtime
        bindings = ProjectTaskStore(runtime.store, tasks)
        self.releases = ProjectTaskReleases(bindings)
        self.materializations = ProjectMaterializations(ProjectPublications(bindings), runtime.lifecycle)
        self._lock = asyncio.Lock()
        self._after = 0

    async def run_once(self):
        async with self._lock:
            rows = await _store(self.releases.candidates, after=self._after)
            for candidate in rows:
                try:
                    async with asyncio.timeout(30):
                        access = await self.runtime._execution_access(candidate, candidate["actor_principal_id"])
                        if candidate["published"]:
                            backend = self.runtime.backends[candidate["backend"]]
                            await _store(self.materializations.request, access, candidate["task_id"], preflight=backend.preflight)
                        else:
                            await _store(self.releases.release_failed, access, candidate["task_id"])
                except Exception as exc:
                    logger.info("Project task %s reconciliation deferred: %s", candidate["task_id"], type(exc).__name__)
            # Bounded scans must not let one revoked or uncertain task starve others.
            self._after = rows[-1]["task_id"] if len(rows) == 20 else 0
            return len(rows)

    async def run(self, stop):
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception as exc:
                logger.warning("Project task reconciliation scan failed: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=30)
            except TimeoutError:
                pass
