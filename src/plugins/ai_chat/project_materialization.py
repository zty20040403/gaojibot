"""Narrow task-lock authority for restoring an already accepted project version."""

import time

from .project_lifecycle import ProjectLifecycle, transition_plan
from .projects import ProjectConflict


def task_plan(binding, publication):
    return {"task_id": binding["task_id"], "revision": publication["revision"],
            "version_no": publication["version_no"], "manifest_sha256": publication["manifest_sha256"],
            "base_version": binding["base_version"], "base_manifest_sha256": binding["manifest_sha256"]}


def require_task_materialization(cursor, project, job):
    """A lifecycle lease is still required by the caller, never a generic bypass."""
    if job is None or project["lifecycle_job_id"] != job.job_id:
        raise ProjectConflict("Project is locked by its task, not this lifecycle")
    row = cursor.execute("""SELECT m.status AS materialization_status,p.*,b.base_version,
        b.manifest_sha256 AS base_manifest_sha256,b.generation AS source_generation,
        b.actor_principal_id,c.revision AS current_revision,t.status AS task_status,
        t.cancel_requested,t.project_id AS task_project,t.project_base_version
        FROM project_task_materializations m JOIN project_task_publications p USING(task_id)
        JOIN project_task_bindings b USING(task_id) JOIN subagent_controls c USING(task_id)
        JOIN subagent_tasks t USING(task_id) WHERE m.job_id=?""", (job.job_id,)).fetchone()
    run = cursor.execute("SELECT * FROM project_lifecycle_runs WHERE job_id=?", (job.job_id,)).fetchone()
    if (row is None or run is None or row["materialization_status"] != "pending"
            or project["locked_task_id"] != row["task_id"] or row["project_id"] != project["project_id"]
            or row["task_project"] != project["project_id"] or row["project_base_version"] != row["base_version"]
            or row["current_revision"] != row["revision"] or row["cancel_requested"]
            or row["task_status"] not in {"completed", "partial"}
            or run["action"] != "restore" or run["project_id"] != project["project_id"]
            or run["actor_principal_id"] != row["actor_principal_id"]
            or run["generation"] != project["workspace_generation"]
            or run["generation"] != row["source_generation"] + 1):
        raise ProjectConflict("Project task materialization authority changed")
    expected = {"task_id": row["task_id"], "revision": row["revision"],
        "version_no": row["version_no"], "manifest_sha256": row["manifest_sha256"],
        "base_version": row["base_version"], "base_manifest_sha256": row["base_manifest_sha256"]}
    if (run["plan"].get("task_publication") != expected
            or run["plan"].get("baseline") != {k: row[k] for k in ("version_no", "manifest_sha256")}
            or run["plan"]["restore"]["source_generation"] != row["source_generation"]
            or ProjectLifecycle._baseline(cursor, project["project_id"]) != run["plan"]["baseline"]):
        raise ProjectConflict("Project materialization plan does not match its publication")
    return dict(row)


class ProjectMaterializations:
    def __init__(self, publications, lifecycle):
        self.publications, self.lifecycle = publications, lifecycle
        self.projects, self.tasks = publications.projects, publications.tasks
        if self.projects is not lifecycle.projects:
            raise ValueError("Materialization requires the publication's project store")

    def request(self, access, task_id, *, preflight):
        """Queue one restore atomically, without releasing task ownership."""
        if not callable(preflight):
            raise ValueError("A backend preflight is required")
        now = int(time.time())
        with self.tasks._transaction() as cursor:
            self.projects._scope_lock(cursor, access)
            control = cursor.execute("SELECT revision FROM subagent_controls WHERE task_id=? FOR UPDATE", (task_id,)).fetchone()
            project, binding, task = self.publications.bindings._owned(cursor, access, task_id)
            publication = cursor.execute("SELECT * FROM project_task_publications WHERE task_id=?", (task_id,)).fetchone()
            if publication is None or control is None or publication["revision"] != control["revision"]:
                raise ProjectConflict("Task has no current accepted project publication")
            prior = cursor.execute("""SELECT r.* FROM project_task_materializations m
                JOIN project_lifecycle_runs r USING(job_id) WHERE m.task_id=?""", (task_id,)).fetchone()
            if prior is not None:
                if prior["plan"].get("task_publication") != task_plan(binding, publication):
                    raise ProjectConflict("Materialization request identity changed")
                return self.lifecycle._public_run(prior)
            if (project["locked_task_id"] != task_id or project["lifecycle_job_id"] is not None
                    or project["workspace_generation"] != binding["generation"]
                    or project["lifecycle_fence"] != binding["fence"]
                    or project["status"] not in {"stopped", "hibernated", "archived"}
                    or task["cancel_requested"] or task["status"] not in {"completed", "partial"}):
                raise ProjectConflict("Project is not ready to materialize its accepted task")
            if cursor.execute("SELECT 1 FROM project_turn_saves WHERE project_id=? AND state='pending' LIMIT 1",
                              (project["project_id"],)).fetchone():
                raise ProjectConflict("Project has an unfinished automatic save")
            baseline = {k: publication[k] for k in ("version_no", "manifest_sha256")}
            document = self.publications.bindings._manifest(cursor, access, project["project_id"], baseline)
            generation, plan = transition_plan(project, "restore", baseline, rebuild=document.get("rebuild"))
            if generation >= 2**53:
                raise ProjectConflict("Workspace generation exhausted")
            plan["task_publication"] = task_plan(binding, publication)
            preflight(plan)
            job, run = self.lifecycle._enqueue(cursor, access, project, "restore", generation, plan,
                                               f"task-materialize:{task_id}", now)
            cursor.execute("INSERT INTO project_task_materializations(task_id,job_id,created_at) VALUES (?,?,?)",
                           (task_id, job.job_id, now))
            current = self.projects._get(cursor, access, project["project_id"])
            self.lifecycle._idle(cursor, current, lifecycle_job=job)
            self.tasks.append_checkpoint(task_id, "project_materialization_queued",
                {"job_id": job.job_id, "project_id": project["project_id"], **baseline}, transaction_cursor=cursor)
        self.tasks._notify_changed(task_id)
        return self.lifecycle._public_run(run)
