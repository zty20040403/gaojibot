"""Private lifecycle journal, not a host-command API.

The host adapter must reconcile each persisted step before acknowledging it.
Registration is owned by the opt-in project runtime, disabled by default.
"""

from __future__ import annotations

import json
import re
import time
import uuid

from .projects import ProjectConflict, require_project_lease, require_project_io_idle
from .project_policy import ProjectPolicy


KIND = "project.lifecycle"
TARGETS = {
    "wake": "active",
    "stop": "stopped",
    "hibernate": "hibernated",
    "archive": "archived",
    "restore": "active",
}


def transition_plan(project, action, baseline, *, rebuild=None):
    source = project["status"]
    allowed = {
        "wake": {"stopped", "hibernated", "archived"},
        "stop": {"active"},
        "hibernate": {"stopped"},
        "archive": {"hibernated"},
        "snapshot": {"active", "stopped"},
        "restore": {"active", "stopped", "hibernated", "archived"},
    }
    if action not in allowed or source not in allowed[action]:
        raise ProjectConflict("Project transition is not valid from its current state")
    generation = project["workspace_generation"]
    if action not in {"wake", "restore"} and generation == 0:
        raise ProjectConflict("Project has no materialized workspace")
    if action == "archive" and project["pinned"]:
        raise ProjectConflict("Pinned projects cannot be archived")
    if source == "archived" and baseline is None:
        raise ProjectConflict("Archived project has no retained version")
    command = (
        rebuild["command"]
        if rebuild and action in {"wake", "archive", "restore"}
        else project["rebuild_command"]
    )
    if action == "restore":
        from .project_restore import restore_steps

        if baseline is None:
            raise ProjectConflict("Restore requires a retained version")
        steps, replace_at = restore_steps(project["backend"], source, generation, bool(rebuild))
        return generation + 1, {
            "protocol": 3 if project["backend"] == "vm" else 2,
            "reservation": uuid.uuid4().hex, "steps": steps,
            "backend": project["backend"], "quota_bytes": project["quota_bytes"],
            "rebuild_command": rebuild["command"] if rebuild else "",
            "rebuild_spec": rebuild, "baseline": baseline,
            "restore": {"source_state": source, "source_generation": generation,
                        "replace_at": replace_at, "original_rebuild_command": project["rebuild_command"]},
        }
    if action == "wake":
        fresh = generation == 0 or source == "archived"
        generation += int(fresh)
        steps = (["create_volume"] if fresh else []) + ["reserve_workspace"]
        if fresh or source == "hibernated":
            steps += ["mount_volume"]
        if fresh and baseline is not None:
            steps += ["restore_version"]
        if rebuild and (source == "hibernated" or fresh):
            steps += ["rebuild_dependencies"]
        steps += ["start_workspace"]
    else:
        # Quiesce must stop writers before exporting a consistent snapshot.
        steps = [
            "reserve_workspace",
            "mount_volume" if action == "archive" else "quiesce_workspace",
            "snapshot_workspace",
        ]
        if action == "snapshot":
            if source == "active":
                steps += ["start_workspace"]
        elif action == "stop":
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
    if project["backend"] == "vm":
        # Guest file and trim operations require a running, closed-to-users VM.
        if action == "wake":
            steps = (["create_volume"] if fresh else []) + ["reserve_workspace", "boot_workspace"]
            if fresh and baseline is not None:
                steps += ["restore_version"]
            if rebuild and (source == "hibernated" or fresh):
                steps += ["rebuild_dependencies"]
        elif action == "snapshot":
            steps = ["reserve_workspace",
                     "quiesce_workspace" if source == "active" else "boot_workspace",
                     "snapshot_workspace"]
            if source == "stopped":
                steps += ["stop_workspace"]
        elif action == "stop":
            steps = ["reserve_workspace", "quiesce_workspace", "snapshot_workspace", "stop_workspace"]
        else:
            steps = ["reserve_workspace", "boot_workspace", "snapshot_workspace"]
            if action == "hibernate":
                steps += ["prune_rebuildable", "trim_volume"]
            steps += ["unmount_volume", "remove_workspace"]
            if action == "archive":
                steps += ["delete_volume"]
    target = source if action == "snapshot" else TARGETS[action]
    steps += ["release_workspace", "verify_" + target]
    return generation, {
        "protocol": 3 if project["backend"] == "vm" else 2,
        "reservation": uuid.uuid4().hex,
        "steps": steps,
        "backend": project["backend"],
        "quota_bytes": project["quota_bytes"],
        "rebuild_command": command,
        "rebuild_spec": rebuild,
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
        require_project_io_idle(cursor, project["project_id"])
        if cursor.execute("""SELECT 1 FROM project_file_deliveries f JOIN deliveries d USING(delivery_id)
                WHERE f.project_id=? AND NOT (d.status='committed'
                OR (d.status='cancelled' AND f.upload_started_at IS NULL)) LIMIT 1""", (project["project_id"],)).fetchone():
            raise ProjectConflict("Project file deliveries are unresolved")
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
        self, access, project_id, action, *, expected_version, request_key, now=None,
        idle_policy=None, preflight=None, label="", source_turn_id=None,
        restore_version=None,
    ):
        from .project_turn_saves import require_finished_save

        if ((action == "restore" and (type(restore_version) is not int or not 1 <= restore_version < 2**53))
                or (action != "restore" and restore_version is not None)):
            raise ValueError("Invalid restore version")
        if source_turn_id is not None and (
            type(source_turn_id) is not int or source_turn_id <= 0 or action != "snapshot" or label
        ):
            raise ValueError("Invalid automatic snapshot trigger")
        if not isinstance(label, str) or len(label) > 120 or (label and action != "snapshot"):
            raise ValueError("Invalid snapshot label")
        if project_id is None and action not in {"snapshot", "restore"}:
            raise ValueError("A lifecycle target is required")
        if idle_policy is not None and (
            not isinstance(idle_policy, ProjectPolicy) or not callable(preflight)
        ):
            raise ValueError("Automatic lifecycle requires a host policy and backend preflight")
        if not isinstance(request_key, str) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,120}", request_key
        ):
            raise ValueError("Invalid lifecycle request key")
        now = int(time.time()) if now is None else now
        with self.projects._transaction() as cursor:
            self.projects._scope_lock(cursor, access)
            if action in {"snapshot", "restore"}:
                # Resolve the persisted request before the user's mutable focus.
                prior = cursor.execute(
                    """SELECT r.* FROM project_lifecycle_runs r JOIN projects p USING(project_id)
                    WHERE p.scope_key=? AND p.bot_native_user_id=?
                    AND r.actor_principal_id=? AND r.request_key=? LIMIT 2""",
                    (access.scope.key, access.scope.bot_native_user_id, access.principal_id, request_key),
                ).fetchall()
                if prior:
                    if (len(prior) != 1 or prior[0]["action"] != action
                        or prior[0]["plan"].get("label", "") != label
                        or prior[0]["plan"].get("source_turn_id") != source_turn_id
                        or (action == "restore" and prior[0]["plan"]["baseline"]["version_no"] != restore_version)
                        or (project_id is not None and project_id != prior[0]["project_id"])):
                        raise ProjectConflict("Project request identity was reused")
                    project = self.projects._get(cursor, access, prior[0]["project_id"])
                    self.projects._editor(cursor, access, project)
                    return self._public_run(prior[0])
                if project_id is None:
                    current = self.projects._current(cursor, access, now)
                    if current is None:
                        raise LookupError("No current project")
                    project_id = current["project_id"]
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
                cursor, access, project_id, edit=True, expected=expected_version,
                owner=idle_policy is not None,
            )
            if idle_policy is not None and idle_policy.idle_action(project, now=now) != action:
                raise ProjectConflict("Project is no longer eligible for this idle transition")
            if source_turn_id is not None and not require_finished_save(cursor, access, project_id, source_turn_id):
                raise ProjectConflict("Finished turn has no confirmed mutation to save")
            if action != "snapshot" and cursor.execute(
                "SELECT 1 FROM project_turn_saves WHERE project_id=? AND state='pending' LIMIT 1",
                (project_id,),
            ).fetchone():
                raise ProjectConflict("Project has an unfinished automatic save")
            self._idle(cursor, project)
            baseline = self._baseline(cursor, project_id)
            if action == "restore":
                target = cursor.execute(
                    "SELECT version_no,manifest_sha256 FROM project_versions WHERE project_id=? AND version_no=?",
                    (project_id, restore_version),
                ).fetchone()
                if target is None:
                    raise ValueError("Project version does not exist")
                baseline = dict(target)
            rebuild = (
                self.projects._manifest(
                    cursor, access, project_id, baseline["version_no"]
                ).get("rebuild")
                if baseline
                else None
            )
            generation, plan = transition_plan(
                project, action, baseline, rebuild=rebuild
            )
            if action == "snapshot":
                plan["label"] = label
                if source_turn_id is not None:
                    plan["source_turn_id"] = source_turn_id
                    plan["trigger"] = {"kind": "turn", "turn_id": source_turn_id}
            if generation >= 2**53:
                raise ProjectConflict("Workspace generation exhausted")
            if idle_policy is not None:
                plan["trigger"] = {"kind": "idle", "last_active_at": project["last_active_at"]}
                preflight(plan)
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
                    project["status"] if action == "snapshot" else TARGETS[action],
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
            if source_turn_id is not None:
                cursor.execute(
                    """UPDATE project_turn_saves SET state='queued',job_id=?,error_code='',updated_at=?
                    WHERE project_id=? AND turn_id=?""", (job.job_id, now, project_id, source_turn_id),
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
                    "rebuild_spec": plan.get("rebuild_spec"),
                    "baseline": plan["baseline"],
                    "reservation": plan.get("reservation"),
                }
                if run["action"] == "restore":
                    restore = plan["restore"]
                    if 0 < index < restore["replace_at"]:
                        arguments["generation"] = restore["source_generation"]
                        arguments["rebuild_command"] = restore["original_rebuild_command"]
                    if plan["steps"][index] == "delete_volume":
                        backup = cursor.execute(
                            """SELECT evidence FROM project_lifecycle_steps WHERE job_id=?
                            AND operation='snapshot_workspace' AND status='succeeded' AND step_index<?""",
                            (job.job_id, index),
                        ).fetchone()
                        if backup is None or backup["evidence"].get("snapshot") != self._baseline(cursor, project["project_id"]):
                            raise ProjectConflict("Rollback must preserve current files before replacing the workspace")
                if run["action"] == "snapshot":
                    arguments["label"] = plan.get("label", "")
                    if "source_turn_id" in plan:
                        arguments["source_turn_id"] = plan["source_turn_id"]
                if plan["steps"][index] == "prune_rebuildable":
                    source = cursor.execute(
                        """SELECT fence,evidence FROM project_lifecycle_steps WHERE job_id=?
                        AND operation='snapshot_workspace' AND status='succeeded' AND step_index<?
                        ORDER BY step_index DESC LIMIT 1""",
                        (job.job_id, index),
                    ).fetchone()
                    if source is None or source["evidence"].get(
                        "snapshot"
                    ) != self._baseline(cursor, project["project_id"]):
                        raise ProjectConflict(
                            "Cleanup requires a published current snapshot"
                        )
                    arguments.update(
                        snapshot=source["evidence"]["snapshot"],
                        snapshot_fence=source["fence"],
                    )
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
        if step["operation"] in {"check_restore_source", "restore_version"}:
            if evidence.get("restored_version") != step["arguments"]["baseline"]:
                raise ProjectConflict("Restore evidence does not match the requested version")
        if step["operation"] == "publish_restore":
            latest = ProjectLifecycle._baseline(cursor, run["project_id"])
            if (latest is None or evidence.get("snapshot") != latest
                    or latest["manifest_sha256"] != run["plan"]["baseline"]["manifest_sha256"]
                    or evidence.get("restored_version") != run["plan"]["baseline"]):
                raise ProjectConflict("Rollback version was not published with the requested content")
        if step["operation"] == "prune_rebuildable":
            if evidence.get("snapshot") != step["arguments"].get(
                "snapshot"
            ) or evidence.get("snapshot") != ProjectLifecycle._baseline(
                cursor, run["project_id"]
            ):
                raise ProjectConflict(
                    "Cleanup evidence does not reference its published snapshot"
                )
        if step["operation"] == "rebuild_dependencies" and (
            evidence.get("rebuilt_version") != step["arguments"]["baseline"]
            or evidence.get("command") != step["arguments"]["rebuild_command"]
            or type(evidence.get("exit_code")) is not int
            or evidence["exit_code"] != 0
        ):
            raise ProjectConflict(
                "Rebuild evidence does not prove the pinned recipe completed"
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

    def record_progress(self, job, step, observation):
        """Trusted adapter observations are progress, never a completion receipt."""
        fields = {
            "state",
            "command",
            "exit_code",
            "error",
            "created_at",
            "deadline",
            "finished_at",
        }
        progress = {key: observation.get(key) for key in fields}
        if len(json.dumps(progress)) > 8192:
            raise ValueError("Project progress is too large")
        with self.projects._transaction() as cursor:
            project, run = self._owned(cursor, job, None)
            row = cursor.execute(
                "SELECT * FROM project_lifecycle_steps WHERE job_id=? AND step_index=?",
                (job.job_id, step["step_index"]),
            ).fetchone()
            if (
                row is None
                or row["status"] != "pending"
                or row["step_index"] != run["next_step"]
                or row["operation"] != "rebuild_dependencies"
                or any(
                    row[key] != step[key]
                    for key in ("request_id", "dispatch_lease", "arguments", "fence")
                )
                or row["dispatch_lease"] != job.lease_generation
            ):
                raise ProjectConflict("Progress dispatch was superseded")
            require_project_lease(cursor, project, job, None)
            cursor.execute(
                "UPDATE project_lifecycle_steps SET evidence=?::jsonb,updated_at=? WHERE job_id=? AND step_index=?",
                (
                    json.dumps({"progress": progress}),
                    int(time.time()),
                    job.job_id,
                    row["step_index"],
                ),
            )

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
            if run["action"] == "snapshot":
                result["snapshot"] = next(
                    row["evidence"]["snapshot"] for row in rows
                    if row["operation"] == "snapshot_workspace"
                )
            if run["action"] == "restore":
                result["snapshot"] = next(row["evidence"]["snapshot"] for row in rows if row["operation"] == "publish_restore")
                result["restored_version"] = run["plan"]["baseline"]
            require_project_lease(cursor, project, job, lease_now)
            cursor.execute(
                """UPDATE projects SET status=?,sandbox_id=?,lifecycle_job_id=NULL,
                version=version+1,state_changed_at=?,updated_at=?,last_active_at=? WHERE project_id=?""",
                (
                    run["target_state"],
                    rows[-1]["evidence"].get("sandbox_id"),
                    project["state_changed_at"] if run["action"] == "snapshot" else now,
                    now,
                    now if run["action"] in {"wake", "restore"} else project["last_active_at"],
                    project["project_id"],
                ),
            )
            if run["action"] == "restore":
                cursor.execute(
                    "UPDATE projects SET rebuild_command=?,sampled_at=NULL WHERE project_id=?",
                    (run["plan"]["rebuild_command"], project["project_id"]),
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

    def snapshot_status(self, access, project_id, *, job_id=None):
        """Public save receipt: queued work is never represented as a saved version."""
        with self.projects._transaction() as cursor:
            self.projects._get(cursor, access, project_id)
            row = cursor.execute(
                """SELECT r.*,j.status AS job_status,j.result_json FROM project_lifecycle_runs r
                JOIN durable_jobs j USING(job_id) WHERE r.project_id=? AND r.action='snapshot'
                AND (?::bigint IS NULL OR r.job_id=?) ORDER BY r.job_id DESC LIMIT 1""",
                (project_id, job_id, job_id),
            ).fetchone()
            if row is None:
                return None
            saved = row["status"] == "completed" and row["job_status"] == "succeeded"
            result = json.loads(row["result_json"]) if saved else {}
            snapshot = result.get("snapshot", {})
            saved = saved and type(snapshot.get("version_no")) is int
            return {
                "status": "completed" if saved else (
                    "needs_attention" if row["status"] == "needs_attention" else row["job_status"]
                ),
                "saved": saved,
                "version": f"v{snapshot['version_no']}" if saved else None,
                "label": row["plan"].get("label", ""),
                "completed_steps": row["next_step"],
                "total_steps": len(row["plan"]["steps"]),
                "updated_at": row["updated_at"],
            }

    def maintenance_candidates(self, *, after=0, limit=50):
        """Private host inventory, not a cross-conversation API."""
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid project maintenance page")
        with self.projects._transaction() as cursor:
            return [dict(row) for row in cursor.execute(
                """SELECT * FROM projects WHERE project_id>? AND status IN
                ('active','stopped','hibernated','archived') AND lifecycle_job_id IS NULL
                AND locked_task_id IS NULL ORDER BY project_id LIMIT ?""",
                (after, limit),
            ).fetchall()]

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
