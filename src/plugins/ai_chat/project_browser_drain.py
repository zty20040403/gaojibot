"""A leased browser shutdown prelude before changing the workspace epoch.

The lifecycle lock stops new tool dispatches while the existing IO grant keeps
the runner's original identity. No snapshot or new physical fence is admitted
until both browser shutdown and whole-workspace quiescence are durable.
"""

import json
import time

from .projects import ProjectConflict, require_project_io_idle, require_project_lease


def pending(plan):
    drain = plan.get("browser_drain")
    return drain if drain and drain.get("completed") is not True else None


def identity(operation):
    return {key: operation[key] for key in ("operation_id", "sandbox_id", "generation", "fence")}


def request_project(projects, cursor, access, project_id, *, expected=None, owner=False, turn_id=None):
    from .project_io import BROWSER_SESSION_TOOL
    from .project_turn_saves import require_running_turn

    project = projects._get(cursor, access, project_id, expected=expected, owner=owner)
    projects._editor(cursor, access, project)
    if project["lifecycle_job_id"] is not None or project["locked_task_id"] is not None:
        raise ProjectConflict("Project already belongs to a lifecycle or task")
    rows = cursor.execute("""SELECT io.*,t.status AS turn_status,t.finished_at
        FROM project_io io LEFT JOIN agent_turns t ON t.turn_id=io.journal_turn_id
        WHERE io.project_id=? AND io.state IN ('reserved','dispatched','needs_attention')
        FOR UPDATE OF io""", (project_id,)).fetchall()
    if not rows:
        return project, None
    operation = rows[0]
    if (len(rows) != 1 or operation["tool_name"] != BROWSER_SESSION_TOOL
            or project["status"] != "active" or not operation["mutating"]
            or operation["sandbox_id"] != project["sandbox_id"]
            or operation["generation"] != project["workspace_generation"]
            or operation["fence"] != project["lifecycle_fence"]):
        raise ProjectConflict("Project has an active or unconfirmed tool operation")
    if operation["turn_status"] == "running" or operation["finished_at"] is None:
        if operation["journal_turn_id"] != turn_id or operation["actor_principal_id"] != access.principal_id:
            raise ProjectConflict("Another running turn owns the project browser")
        require_running_turn(cursor, access, turn_id)
    return project, identity(operation)


def idle(cursor, project, drain):
    if drain is None:
        require_project_io_idle(cursor, project["project_id"])
        return
    rows = cursor.execute("""SELECT * FROM project_io WHERE project_id=?
        AND state IN ('reserved','dispatched','needs_attention')""", (project["project_id"],)).fetchall()
    if any(identity(row) != drain for row in rows):
        raise ProjectConflict("Lifecycle does not own the pending project IO")


def require_drain(cursor, project, grant, job, *, access=None):
    require_project_lease(cursor, project, job)
    run = cursor.execute("SELECT * FROM project_lifecycle_runs WHERE job_id=? FOR UPDATE",
                         (job.job_id,)).fetchone()
    drain = pending(run["plan"]) if run else None
    if (not drain or drain["operation_id"] != grant.operation_id
            or project["project_id"] != grant.project_id or run["next_step"] != 0
            or run["status"] not in {"pending", "running", "needs_attention"}
            or run["source_state"] != "active" or project["status"] != "active"
            or project["workspace_generation"] != drain["generation"]
            or project["lifecycle_fence"] != drain["fence"]
            or project["sandbox_id"] != drain["sandbox_id"]
            or cursor.execute("SELECT 1 FROM project_lifecycle_steps WHERE job_id=? LIMIT 1",
                              (job.job_id,)).fetchone()):
        raise ProjectConflict("Browser shutdown no longer owns the original workspace")
    if access is not None and run["actor_principal_id"] != access.principal_id:
        raise PermissionError("Browser shutdown belongs to another lifecycle actor")


def finish(lifecycle, job, access):
    now = int(time.time())
    projects = lifecycle.projects
    with projects._transaction() as cursor:
        projects._scope_lock(cursor, access)
        project, run = lifecycle._owned(cursor, job, None)
        projects._get(cursor, access, project["project_id"])
        projects._editor(cursor, access, project)
        if access.principal_id != run["actor_principal_id"]:
            raise PermissionError("Lifecycle actor changed")
        drain = pending(run["plan"])
        if drain is None:
            return
        require_project_io_idle(cursor, project["project_id"])
        row = cursor.execute("SELECT * FROM project_io WHERE operation_id=? AND project_id=?",
                             (drain["operation_id"], project["project_id"])).fetchone()
        if row is None or identity(row) != drain or row["state"] not in {"completed", "aborted"}:
            raise ProjectConflict("Browser shutdown has no settled IO receipt")
        if row["state"] == "completed":
            session = cursor.execute("SELECT state,closed_evidence FROM project_browser_sessions WHERE operation_id=?",
                                     (row["operation_id"],)).fetchone()
            if (not session or session["state"] != "closed" or not session["closed_evidence"]
                    or row["evidence"] != {"project_id": project["project_id"],
                        "sandbox_id": drain["sandbox_id"], "generation": drain["generation"],
                        "fence": drain["fence"], "pending": False, "quiescent": True}):
                raise ProjectConflict("Browser or workspace shutdown evidence is missing")
        plan = {**run["plan"], "browser_drain": {**drain, "completed": True}}
        cursor.execute("UPDATE project_lifecycle_runs SET plan=?::jsonb,updated_at=? WHERE job_id=?",
                       (json.dumps(plan), now, job.job_id))
        cursor.execute("UPDATE projects SET workspace_generation=?,version=version+1,updated_at=? WHERE project_id=?",
                       (run["generation"], now, project["project_id"]))
        projects._event(cursor, project["project_id"], access, "browser_drained",
                        {"job_id": job.job_id, "operation_id": row["operation_id"]}, now)


def grant_for(lifecycle, job, access):
    from .project_io import ProjectIOGrant

    with lifecycle.projects._transaction() as cursor:
        lifecycle.projects._scope_lock(cursor, access)
        project, run = lifecycle._owned(cursor, job, None)
        lifecycle.projects._get(cursor, access, project["project_id"])
        lifecycle.projects._editor(cursor, access, project)
        drain = pending(run["plan"])
        if drain is None:
            return None
        row = cursor.execute("SELECT token FROM project_io WHERE project_id=? AND operation_id=?",
                             (project["project_id"], drain["operation_id"])).fetchone()
        if row is None:
            raise ProjectConflict("Browser IO journal disappeared")
        grant = ProjectIOGrant(drain["operation_id"], project["project_id"], row["token"])
        require_drain(cursor, project, grant, job, access=access)
        return grant
