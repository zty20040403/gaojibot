"""Host-administrator quota authority, limited to one immutable physical plan.

This is deliberately not ProjectAccess: it cannot read member files, execute
tools or manufacture conversation membership. Admin audit and job are atomic.
"""

from dataclasses import dataclass
import json
import uuid

from .projects import ProjectConflict


@dataclass(frozen=True)
class QuotaAuthority:
    job_id: int
    project_id: int
    actor: str
    resource_version: int


def resize_steps(source):
    if source not in {"active", "stopped"}:
        raise ProjectConflict("Resize requires an active or stopped workspace")
    return ["reserve_workspace", "quiesce_workspace" if source == "active" else "boot_workspace",
            "resize_volume"] + (["stop_workspace"] if source == "stopped" else []) + [
                "release_workspace", "verify_" + source]


def resize_plan(project, quota):
    if project["backend"] != "vm" or not project["workspace_generation"] or not project["sandbox_id"]:
        raise ProjectConflict("This workspace has no supported physical quota adapter")
    if type(quota) is not int or not 64 * 1024**2 <= quota <= 100 * 1024**3 or quota % 1024**2:
        raise ValueError("Invalid project quota")
    if quota == project["quota_bytes"]:
        raise ProjectConflict("Project quota is unchanged")
    return {"protocol": 3, "backend": "vm", "reservation": uuid.uuid4().hex,
            "steps": resize_steps(project["status"]), "quota_bytes": project["quota_bytes"],
            "baseline": None, "rebuild_spec": None, "rebuild_command": project["rebuild_command"],
            "resize": {"source_state": project["status"], "quota_bytes": quota}}


def valid_resize_plan(plan):
    try:
        resize = plan["resize"]
        old, new = plan["quota_bytes"], resize["quota_bytes"]
        return (plan["backend"] == "vm" and plan["protocol"] == 3
                and set(resize) == {"source_state", "quota_bytes"}
                and plan["steps"] == resize_steps(resize["source_state"])
                and all(type(value) is int and 64 * 1024**2 <= value <= 100 * 1024**3
                        and value % 1024**2 == 0 for value in (old, new))
                and old != new and plan.get("baseline") is None and plan.get("rebuild_spec") is None
                and not any(key in plan for key in ("restore", "task_admission", "browser_drain", "task_publication")))
    except (KeyError, TypeError, ProjectConflict):
        return False


def require_quota_authority(cursor, project, run):
    plan = run["plan"]
    grant = cursor.execute("SELECT * FROM project_quota_authorizations WHERE job_id=?", (run["job_id"],)).fetchone()
    if (run["actor_principal_id"] is not None or not valid_resize_plan(plan) or grant is None
            or grant["revoked_at"] is not None
            or any(grant[key] != project[key] for key in ("project_id", "scope_key", "bot_native_user_id"))
            or grant["generation"] != project["workspace_generation"]
            or grant["from_quota_bytes"] != project["quota_bytes"]
            or grant["from_quota_bytes"] != plan["quota_bytes"]
            or grant["quota_bytes"] != plan["resize"]["quota_bytes"]
            or run["source_state"] != plan["resize"]["source_state"]
            or run["target_state"] != run["source_state"]):
        raise PermissionError("Project resize authority is missing, revoked or changed")
    audit = cursor.execute("""SELECT after_json FROM admin_audit_log WHERE resource_key='projects'
        AND resource_version=? AND actor=? AND action='project.quota' AND target=? AND status='succeeded'""",
        (grant["resource_version"], grant["actor"], f"prj#{project['project_id']}")).fetchone()
    receipt = json.loads(audit[0]) if audit else {}
    if any(receipt.get(key) != grant[key] for key in ("job_id", "project_id", "quota_bytes", "from_quota_bytes")):
        raise PermissionError("Project resize has no matching committed administrator audit")
    return QuotaAuthority(run["job_id"], project["project_id"], grant["actor"], grant["resource_version"])
