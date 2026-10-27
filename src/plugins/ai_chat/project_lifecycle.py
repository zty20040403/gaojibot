"""Private lifecycle journal, not an enabled scheduler or a host-command API.

The host adapter must reconcile each persisted step before acknowledging it.
No production worker is registered until backend/native acceptance is complete.
"""

from __future__ import annotations

import json
import re
import time
import uuid

from .projects import ProjectConflict, require_project_lease


KIND = "project.lifecycle"
TARGETS = {
    "wake": "active",
    "stop": "stopped",
    "hibernate": "hibernated",
    "archive": "archived",
}


def transition_plan(project, action, baseline):
    source = project["status"]
    allowed = {
        "wake": {"stopped", "hibernated", "archived"},
        "stop": {"active"},
        "hibernate": {"stopped"},
        "archive": {"hibernated"},
    }
    if action not in allowed or source not in allowed[action]:
        raise ProjectConflict("Project transition is not valid from its current state")
    generation = project["workspace_generation"]
    if action != "wake" and generation == 0:
        raise ProjectConflict("Project has no materialized workspace")
    if action == "archive" and project["pinned"]:
        raise ProjectConflict("Pinned projects cannot be archived")
    if source == "archived" and baseline is None:
        raise ProjectConflict("Archived project has no retained version")
    if action == "wake":
        fresh = generation == 0 or source == "archived"
        generation += int(fresh)
        steps = (["create_volume"] if fresh else []) + ["reserve_workspace"]
        if fresh or source == "hibernated":
            steps += ["mount_volume"]
        if fresh and baseline is not None:
            steps += ["restore_version"]
        steps += ["start_workspace"]
        if source in {"hibernated", "archived"} and project["rebuild_command"]:
            steps += ["rebuild_dependencies"]
    else:
        # Quiesce must stop writers before exporting a consistent snapshot.
        steps = [
            "reserve_workspace",
            "mount_volume" if action == "archive" else "quiesce_workspace",
            "snapshot_workspace",
        ]
        if action == "stop":
            steps += ["stop_workspace"]
        elif action == "hibernate":
            steps += [
                "prune_rebuildable",
                "remove_workspace",
                "trim_volume",
                "unmount_volume",
            ]
        else:
            steps += ["remove_workspace", "unmount_volume", "delete_volume"]
    steps += ["release_workspace", "verify_" + TARGETS[action]]
    return generation, {
        "protocol": 2,
        "reservation": uuid.uuid4().hex,
        "steps": steps,
        "backend": project["backend"],
        "quota_bytes": project["quota_bytes"],
        "rebuild_command": project["rebuild_command"],
        "baseline": baseline,
    }


class ProjectLifecycle:
    def __init__(self, projects, jobs):
        self.projects, self.jobs = projects, jobs

    @staticmethod
    def _public_run(run):
        result = dict(run)
        result["plan"] = {
            key: value for key, value in result["plan"].items() if key != "reservation"
        }
        return result

    @staticmethod
    def _idle(cursor, project):
        if project["locked_task_id"] is not None:
            raise ProjectConflict("Project has an attached task")
        # A failed task is not proof its files are disposable. The task adapter
        # must explicitly release its project reference after reconciliation.
        busy = cursor.execute(
            """SELECT 1 FROM subagent_tasks t WHERE t.project_id=? AND (
            t.status<>'completed' OR EXISTS (
                SELECT 1 FROM subagent_deliveries d
                WHERE d.task_id=t.task_id AND d.state<>'acknowledged')
            OR (coalesce(t.plan_json::jsonb #>> '{contract,delivery_required}','false')<>'false'
                AND NOT EXISTS (SELECT 1 FROM subagent_deliveries d
                    JOIN subagent_controls c ON c.task_id=d.task_id AND c.revision=d.revision
                    WHERE d.task_id=t.task_id AND d.state='acknowledged'))
            OR EXISTS (SELECT 1 FROM subagent_artifacts a WHERE a.task_id=t.task_id
                AND NOT EXISTS (SELECT 1 FROM subagent_deliveries d
                    JOIN subagent_controls c ON c.task_id=d.task_id AND c.revision=d.revision
                    WHERE d.task_id=t.task_id AND d.delivery_key=a.handle AND d.state='acknowledged'))
            OR EXISTS (SELECT 1 FROM subagent_runs r,
                jsonb_array_elements(coalesce(r.result_json::jsonb->'artifacts','[]'::jsonb)) a
                WHERE r.task_id=t.task_id AND NOT EXISTS (
                    SELECT 1 FROM subagent_deliveries d
                    JOIN subagent_controls c ON c.task_id=d.task_id AND c.revision=d.revision
                    WHERE d.task_id=t.task_id AND d.state='acknowledged'
                    AND d.delivery_key=coalesce(nullif(a->>'snapshot',''),a->>'handle')))
            ) LIMIT 1""",
            (project["project_id"],),
        ).fetchone()
        if busy:
            raise ProjectConflict("Project tasks or artifact deliveries are unresolved")

    @staticmethod
    def _baseline(cursor, project_id):
        row = cursor.execute(
            """SELECT version_no,manifest_sha256 FROM project_versions
            WHERE project_id=? ORDER BY version_no DESC LIMIT 1""",
            (project_id,),
        ).fetchone()
        return dict(row) if row else None

    def request(
        self, access, project_id, action, *, expected_version, request_key, now=None
    ):
        if not isinstance(request_key, str) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,120}", request_key
        ):
            raise ValueError("Invalid lifecycle request key")
        now = int(time.time()) if now is None else now
        with self.projects._transaction() as cursor:
            self.projects._scope_lock(cursor, access)
            project = self.projects._get(cursor, access, project_id)
            previous = cursor.execute(
                "SELECT * FROM project_lifecycle_runs WHERE project_id=? AND request_key=?",
                (project_id, request_key),
            ).fetchone()
            if previous:
                if (
                    previous["action"] != action
                    or previous["actor_principal_id"] != access.principal_id
                ):
                    raise ProjectConflict("Lifecycle request identity was reused")
                return self._public_run(previous)
            project = self.projects._get(
                cursor, access, project_id, edit=True, expected=expected_version
            )
            self._idle(cursor, project)
            generation, plan = transition_plan(
                project, action, self._baseline(cursor, project_id)
            )
            if generation >= 2**53:
                raise ProjectConflict("Workspace generation exhausted")
            job, created = self.jobs.enqueue(
                kind=KIND,
                idempotency_key=f"project:{project_id}:lifecycle:{request_key}",
                payload={"project_id": project_id},
                scope_key=access.scope.key,
                resume_on_lease_loss=True,
                now=now,
                transaction_cursor=cursor,
            )
            if not created:
                raise ProjectConflict(
                    "Lifecycle queue identity already exists without its journal"
                )
            row = cursor.execute(
                """INSERT INTO project_lifecycle_runs(job_id,project_id,actor_principal_id,request_key,
                action,source_state,target_state,generation,plan,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?::jsonb,?,?) RETURNING *""",
                (
                    job.job_id,
                    project_id,
                    access.principal_id,
                    request_key,
                    action,
                    project["status"],
                    TARGETS[action],
                    generation,
                    json.dumps(plan),
                    now,
                    now,
                ),
            ).fetchone()
            cursor.execute(
                """UPDATE projects SET lifecycle_job_id=?,workspace_generation=?,
                version=version+1,updated_at=? WHERE project_id=?""",
                (job.job_id, generation, now, project_id),
            )
            self.projects._event(
                cursor,
                project_id,
                access,
                "lifecycle_requested",
                {"job_id": job.job_id, "action": action, "generation": generation},
                now,
            )
            return self._public_run(row)

    def context(self, job, *, access=None, now=None):
        """Private worker context; never expose its reservation in tools or HTTP."""
        with self.projects._transaction() as cursor:
            project, run = self._owned(cursor, job, now)
            if access is not None:
                if access.principal_id != run["actor_principal_id"]:
                    raise PermissionError("Lifecycle actor changed")
                self.projects._get(
                    cursor,
                    access,
                    project["project_id"],
                    edit=True,
                    lifecycle_job=job,
                    now=now,
                )
            return project, run

    def _owned(self, cursor, job, now):
        project = cursor.execute(
            "SELECT * FROM projects WHERE project_id=? FOR UPDATE",
            (job.payload.get("project_id"),),
        ).fetchone()
        if project is None:
            raise ProjectConflict("Project lifecycle target disappeared")
        require_project_lease(cursor, project, job, now)
        run = cursor.execute(
            "SELECT * FROM project_lifecycle_runs WHERE job_id=? FOR UPDATE",
            (job.job_id,),
        ).fetchone()
        if (
            run is None
            or run["project_id"] != project["project_id"]
            or run["generation"] != project["workspace_generation"]
            or run["source_state"] != project["status"]
            or run["status"] not in {"pending", "running", "needs_attention"}
        ):
            raise ProjectConflict(
                "Project lifecycle journal no longer matches the workspace"
            )
        self._idle(cursor, project)
        return dict(project), dict(run)

    def next_step(self, job, *, now=None):
        lease_now = now
        now = int(time.time()) if now is None else now
        with self.projects._transaction() as cursor:
            project, run = self._owned(cursor, job, lease_now)
            index, plan = run["next_step"], run["plan"]
            if index == len(plan["steps"]):
                return None
            row = cursor.execute(
                "SELECT * FROM project_lifecycle_steps WHERE job_id=? AND step_index=?",
                (job.job_id, index),
            ).fetchone()
            if row is None:
                fence = project["lifecycle_fence"] + 1
                if fence >= 2**53:
                    raise ProjectConflict("Project operation fence exhausted")
                arguments = {
                    "project_id": project["project_id"],
                    "owner": project["scope_key"],
                    "generation": run["generation"],
                    "fence": fence,
                    "backend": plan["backend"],
                    "quota_bytes": plan["quota_bytes"],
                    "rebuild_command": plan["rebuild_command"],
                    "baseline": plan["baseline"],
                    "reservation": plan.get("reservation"),
                }
                row = cursor.execute(
                    """INSERT INTO project_lifecycle_steps(job_id,step_index,project_id,fence,request_id,
                    dispatch_lease,operation,arguments,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?::jsonb,?,?) RETURNING *""",
                    (
                        job.job_id,
                        index,
                        project["project_id"],
                        fence,
                        uuid.uuid4().hex,
                        job.lease_generation,
                        plan["steps"][index],
                        json.dumps(arguments),
                        now,
                        now,
                    ),
                ).fetchone()
                cursor.execute(
                    "UPDATE projects SET lifecycle_fence=? WHERE project_id=?",
                    (fence, project["project_id"]),
                )
            elif row["dispatch_lease"] != job.lease_generation:
                # Keep the effect fence/arguments; a new transport receipt can
                # reconcile the old helper intent after an uncertain RPC.
                row = cursor.execute(
                    """UPDATE project_lifecycle_steps SET request_id=?,dispatch_lease=?,updated_at=?
                    WHERE job_id=? AND step_index=? RETURNING *""",
                    (uuid.uuid4().hex, job.lease_generation, now, job.job_id, index),
                ).fetchone()
            cursor.execute(
                "UPDATE project_lifecycle_runs SET status='running',updated_at=? WHERE job_id=?",
                (now, job.job_id),
            )
            require_project_lease(cursor, project, job, lease_now)
            return dict(row)

    @staticmethod
    def _validate_evidence(cursor, run, step, evidence):
        if (
            not isinstance(evidence, dict)
            or evidence.get("verified") is not True
            or not isinstance(evidence.get("observation"), str)
            or not evidence["observation"].strip()
            or len(json.dumps(evidence)) > 32768
        ):
            raise ValueError(
                "A bounded, observed result is required; command exit status is not evidence"
            )
        for key in ("project_id", "generation", "fence"):
            if (
                type(evidence.get(key)) is not int
                or evidence[key] != step["arguments"][key]
            ):
                raise ProjectConflict("Evidence belongs to another project operation")
        if step["operation"] == "snapshot_workspace":
            baseline = ProjectLifecycle._baseline(cursor, run["project_id"])
            if baseline is None or evidence.get("snapshot") != baseline:
                raise ProjectConflict(
                    "Lifecycle snapshot was not published as the latest retained version"
                )
        if step["operation"].startswith("verify_"):
            if evidence.get("state") != run["target_state"]:
                raise ProjectConflict(
                    "Observed project state does not match transition target"
                )
            if run["target_state"] in {"active", "stopped"}:
                if not isinstance(evidence.get("sandbox_id"), str) or not re.fullmatch(
                    r"[A-Za-z0-9_.:-]{1,160}", evidence["sandbox_id"]
                ):
                    raise ValueError("Verified workspace identity is required")

    def acknowledge(self, job, step, evidence, *, now=None):
        lease_now = now
        now = int(time.time()) if now is None else now
        with self.projects._transaction() as cursor:
            project, run = self._owned(cursor, job, lease_now)
            stored = cursor.execute(
                "SELECT * FROM project_lifecycle_steps WHERE job_id=? AND step_index=?",
                (job.job_id, step["step_index"]),
            ).fetchone()
            if (
                stored is None
                or any(
                    stored[key] != step[key]
                    for key in (
                        "job_id",
                        "request_id",
                        "dispatch_lease",
                        "operation",
                        "arguments",
                        "fence",
                    )
                )
                or stored["dispatch_lease"] != job.lease_generation
            ):
                raise ProjectConflict("Step dispatch was superseded")
            if stored["status"] == "succeeded":
                if stored["evidence"] != evidence:
                    raise ProjectConflict("Completed step evidence is immutable")
                return
            if stored["step_index"] != run["next_step"]:
                raise ProjectConflict("Cannot acknowledge an out-of-order operation")
            self._validate_evidence(cursor, run, stored, evidence)
            require_project_lease(cursor, project, job, lease_now)
            cursor.execute(
                """UPDATE project_lifecycle_steps SET status='succeeded',evidence=?::jsonb,updated_at=?
                WHERE job_id=? AND step_index=?""",
                (json.dumps(evidence), now, job.job_id, stored["step_index"]),
            )
            cursor.execute(
                "UPDATE project_lifecycle_runs SET next_step=next_step+1,updated_at=? WHERE job_id=?",
                (now, job.job_id),
            )

    def finish(self, job, *, now=None):
        lease_now = now
        now = int(time.time()) if now is None else now
        with self.projects._transaction() as cursor:
            project, run = self._owned(cursor, job, lease_now)
            rows = cursor.execute(
                "SELECT * FROM project_lifecycle_steps WHERE job_id=? ORDER BY step_index",
                (job.job_id,),
            ).fetchall()
            steps = run["plan"]["steps"]
            if (
                run["next_step"] != len(steps)
                or len(rows) != len(steps)
                or any(
                    row["status"] != "succeeded" or row["operation"] != steps[i]
                    for i, row in enumerate(rows)
                )
            ):
                raise ProjectConflict("Project lifecycle has unverified operations")
            result = {
                "project_id": project["project_id"],
                "state": run["target_state"],
                "generation": run["generation"],
            }
            require_project_lease(cursor, project, job, lease_now)
            cursor.execute(
                """UPDATE projects SET status=?,sandbox_id=?,lifecycle_job_id=NULL,
                version=version+1,state_changed_at=?,updated_at=? WHERE project_id=?""",
                (
                    run["target_state"],
                    rows[-1]["evidence"].get("sandbox_id"),
                    now,
                    now,
                    project["project_id"],
                ),
            )
            if run["target_state"] == "archived":
                cursor.execute(
                    "UPDATE projects SET used_bytes=0,rebuildable_bytes=0,sampled_at=? WHERE project_id=?",
                    (now, project["project_id"]),
                )
            cursor.execute(
                "UPDATE project_lifecycle_runs SET status='completed',updated_at=? WHERE job_id=?",
                (now, job.job_id),
            )
            # Same transaction as releasing the project lock; no completion gap.
            cursor.execute(
                """UPDATE durable_jobs SET status='succeeded',result_json=?,lease_owner='',lease_until=NULL,
                updated_at=?,finished_at=? WHERE job_id=?""",
                (json.dumps(result), now, now, job.job_id),
            )
            cursor.execute(
                """INSERT INTO project_events(project_id,actor_principal_id,kind,payload,created_at)
                VALUES (?,?,'lifecycle_completed',?::jsonb,?)""",
                (
                    project["project_id"],
                    run["actor_principal_id"],
                    json.dumps(result),
                    now,
                ),
            )
            return result

    def needs_attention(self, job, *, now=None):
        """Never unlock or delete data just because an operation timed out."""
        lease_now = now
        now = int(time.time()) if now is None else now
        with self.projects._transaction() as cursor:
            _, run = self._owned(cursor, job, lease_now)
            cursor.execute(
                "UPDATE project_lifecycle_runs SET status='needs_attention',updated_at=? WHERE job_id=?",
                (now, run["job_id"]),
            )

    def journal(self, access, project_id):
        with self.projects._transaction() as cursor:
            self.projects._get(cursor, access, project_id)
            runs = cursor.execute(
                "SELECT * FROM project_lifecycle_runs WHERE project_id=? ORDER BY job_id DESC LIMIT 100",
                (project_id,),
            ).fetchall()
            return [self._public_run(row) for row in runs]
