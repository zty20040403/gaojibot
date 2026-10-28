"""Frozen rollback plans and atomic version publication after physical restore."""

import json
import time

from .projects import ProjectConflict, require_project_lease


def restore_steps(backend, source, generation, rebuild):
    steps = ["check_restore_source"]
    if generation and source != "archived":
        if backend == "vm":
            steps += [
                "reserve_workspace",
                "quiesce_workspace" if source == "active" else "boot_workspace",
                "snapshot_workspace",
                "unmount_volume",
                "remove_workspace",
                "delete_volume",
                "release_workspace",
            ]
        else:
            steps += [
                "reserve_workspace",
                "mount_volume" if source == "hibernated" else "quiesce_workspace",
                "snapshot_workspace",
                "remove_workspace",
                "unmount_volume",
                "delete_volume",
                "release_workspace",
            ]
    replace_at = len(steps)
    steps += [
        "create_volume",
        "reserve_workspace",
        "boot_workspace" if backend == "vm" else "mount_volume",
        "restore_version",
        "publish_restore",
    ]
    if rebuild:
        steps += ["rebuild_dependencies"]
    if backend != "vm":
        steps += ["start_workspace"]
    return steps + ["release_workspace", "verify_active"], replace_at


def valid_restore_plan(plan):
    restore = plan.get("restore")
    if not isinstance(restore, dict):
        return False
    source, generation = restore.get("source_state"), restore.get("source_generation")
    if (
        source not in {"active", "stopped", "hibernated", "archived"}
        or type(generation) is not int
        or generation < 0
        or not isinstance(restore.get("original_rebuild_command"), str)
        or not plan.get("baseline")
    ):
        return False
    steps, replace_at = restore_steps(
        plan["backend"], source, generation, bool(plan.get("rebuild_spec"))
    )
    return steps == plan.get("steps") and replace_at == restore.get("replace_at")


class ProjectRestores:
    def __init__(self, projects):
        self.projects = projects

    def publish(self, access, job, step):
        """Clone immutable references once, only after verified physical import."""
        now = int(time.time())
        pid = step["project_id"]
        with self.projects._transaction() as cursor:
            self.projects._scope_lock(cursor, access)
            project = self.projects._get(
                cursor, access, pid, edit=True, lifecycle_job=job
            )
            run = cursor.execute(
                "SELECT * FROM project_lifecycle_runs WHERE job_id=? FOR UPDATE",
                (job.job_id,),
            ).fetchone()
            current = cursor.execute(
                "SELECT * FROM project_lifecycle_steps WHERE job_id=? AND step_index=? FOR UPDATE",
                (job.job_id, step["step_index"]),
            ).fetchone()
            if (
                run is None
                or run["action"] != "restore"
                or run["status"] != "running"
                or run["actor_principal_id"] != access.principal_id
                or run["project_id"] != pid
                or run["generation"] != project["workspace_generation"]
                or run["next_step"] != step["step_index"]
                or current is None
                or current["status"] != "pending"
                or current["operation"] != "publish_restore"
                or current["dispatch_lease"] != job.lease_generation
                or any(
                    current[k] != step[k]
                    for k in ("request_id", "arguments", "fence", "dispatch_lease")
                )
            ):
                raise ProjectConflict("Restore publication was superseded")
            baseline = run["plan"]["baseline"]
            prior = cursor.execute(
                "SELECT * FROM project_lifecycle_steps WHERE job_id=? AND step_index=?",
                (job.job_id, step["step_index"] - 1),
            ).fetchone()
            if (
                prior is None
                or prior["operation"] != "restore_version"
                or prior["status"] != "succeeded"
                or prior["evidence"].get("restored_version") != baseline
                or prior["arguments"]["generation"] != project["workspace_generation"]
            ):
                raise ProjectConflict(
                    "Restore publication needs a verified imported tree"
                )
            version = cursor.execute(
                "SELECT * FROM project_versions WHERE project_id=? AND version_no=?",
                (pid, baseline["version_no"]),
            ).fetchone()
            if (
                version is None
                or version["manifest_sha256"] != baseline["manifest_sha256"]
            ):
                raise ProjectConflict("Restore source version changed")
            publication = current["evidence"].get("publication")
            if publication is not None:
                restored = cursor.execute(
                    "SELECT * FROM project_versions WHERE project_id=? AND version_no=?",
                    (pid, publication["version_no"]),
                ).fetchone()
                if (
                    restored is None
                    or restored["manifest_sha256"] != publication["manifest_sha256"]
                    or restored["manifest_sha256"] != baseline["manifest_sha256"]
                ):
                    raise ProjectConflict("Restore publication receipt changed")
                return dict(restored)
            if "task_publication" in run["plan"]:
                # Task publication already created the immutable version. Record
                # its physical import, not a second copy of the same version.
                from .project_materialization import require_task_materialization

                require_task_materialization(cursor, project, job)
                cursor.execute(
                    "UPDATE project_lifecycle_steps SET evidence=?::jsonb,updated_at=? WHERE job_id=? AND step_index=?",
                    (json.dumps({"publication": baseline}), now, job.job_id, step["step_index"]),
                )
                require_project_lease(cursor, project, job, None)
                return dict(version)
            number = cursor.execute(
                "SELECT coalesce(max(version_no),0)+1 FROM project_versions WHERE project_id=?",
                (pid,),
            ).fetchone()[0]
            restored = cursor.execute(
                """INSERT INTO project_versions(project_id,version_no,manifest_sha256,file_count,total_bytes,
                label,created_by_principal_id,created_at) VALUES (?,?,?,?,?,?,?,?) RETURNING *""",
                (
                    pid,
                    number,
                    version["manifest_sha256"],
                    version["file_count"],
                    version["total_bytes"],
                    f"Restored from v{version['version_no']}",
                    access.principal_id,
                    now,
                ),
            ).fetchone()
            cursor.execute(
                """INSERT INTO project_version_blobs(project_id,version_no,sha256)
                SELECT project_id,?,sha256 FROM project_version_blobs WHERE project_id=? AND version_no=?""",
                (number, pid, baseline["version_no"]),
            )
            publication = {
                key: restored[key] for key in ("version_no", "manifest_sha256")
            }
            cursor.execute(
                "UPDATE project_lifecycle_steps SET evidence=?::jsonb,updated_at=? WHERE job_id=? AND step_index=?",
                (
                    json.dumps({"publication": publication}),
                    now,
                    job.job_id,
                    step["step_index"],
                ),
            )
            cursor.execute(
                "UPDATE projects SET version=version+1,updated_at=? WHERE project_id=?",
                (now, pid),
            )
            self.projects._event(
                cursor,
                pid,
                access,
                "version_restored",
                {
                    "source_version": baseline["version_no"],
                    "version_no": number,
                    "job_id": job.job_id,
                },
                now,
            )
            require_project_lease(cursor, project, job, None)
            return dict(restored)

    def status(self, access, project_id, *, job_id=None):
        with self.projects._transaction() as cursor:
            self.projects._get(cursor, access, project_id)
            row = cursor.execute(
                """SELECT r.*,j.status AS job_status,j.result_json FROM project_lifecycle_runs r
                JOIN durable_jobs j USING(job_id) WHERE r.project_id=? AND r.action='restore'
                AND (?::bigint IS NULL OR r.job_id=?) ORDER BY r.job_id DESC LIMIT 1""",
                (project_id, job_id, job_id),
            ).fetchone()
            if row is None:
                return None
            complete = row["status"] == "completed" and row["job_status"] == "succeeded"
            result = json.loads(row["result_json"]) if complete else {}
            version = result.get("snapshot", {}).get("version_no")
            complete = complete and type(version) is int
            return {
                "status": "completed"
                if complete
                else (
                    "needs_attention"
                    if row["status"] == "needs_attention"
                    else row["job_status"]
                ),
                "restored": complete,
                "source_version": f"v{row['plan']['baseline']['version_no']}",
                "version": f"v{version}" if complete else None,
                "completed_steps": row["next_step"],
                "total_steps": len(row["plan"]["steps"]),
                "updated_at": row["updated_at"],
            }
