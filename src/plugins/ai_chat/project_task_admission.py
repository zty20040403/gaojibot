"""Durable save-to-task handoff; callable only with host-resolved identities."""

import json
import re
import time

from .agent.contracts import ContextPacket
from .agent.execution import EntryDecision
from .project_lifecycle import ProjectLifecycle, transition_plan
from .project_tasks import ProjectTaskStore, _digest
from .project_turn_saves import require_running_turn
from .projects import ProjectConflict


class ProjectTaskAdmission:
    def __init__(self, lifecycle, tasks):
        self.lifecycle, self.projects, self.tasks = lifecycle, lifecycle.projects, tasks
        self.bindings = ProjectTaskStore(self.projects, tasks)

    def _prior(self, cursor, access, request_key):
        rows = cursor.execute(
            """SELECT r.* FROM project_lifecycle_runs r JOIN projects p USING(project_id)
            WHERE p.scope_key=? AND p.bot_native_user_id=? AND r.actor_principal_id=?
            AND r.request_key=? LIMIT 2""",
            (access.scope.key, access.scope.bot_native_user_id, access.principal_id,
             "task:" + request_key),
        ).fetchall()
        if len(rows) > 1:
            raise ProjectConflict("Project preparation has ambiguous ownership")
        return dict(rows[0]) if rows else None

    def _receipt(self, cursor, access, run):
        project = self.projects._get(cursor, access, run["project_id"])
        self.projects._editor(cursor, access, project)
        if run["actor_principal_id"] != access.principal_id:
            raise PermissionError("Project preparation belongs to another actor")
        job = cursor.execute("SELECT status,result_json FROM durable_jobs WHERE job_id=?",
                             (run["job_id"],)).fetchone()
        result = json.loads(job["result_json"]) if job["status"] == "succeeded" else {}
        return {"project_id": run["project_id"], "job_id": run["job_id"],
                "status": "needs_attention" if run["status"] == "needs_attention" else job["status"],
                "task_id": result.get("task_id"), "task_bound": result.get("task_bound") is True,
                "completed_steps": run["next_step"], "total_steps": len(run["plan"]["steps"])}

    def request(self, access, *, project_id=None, expected_version=None, request_key,
                turn_id, packet, decision, dispatch, max_parallelism, max_steps,
                timeout_seconds, preflight, now=None):
        """Freeze one project now; bind only after its physical save is verified.

        Retries resolve the original envelope before mutable focus or turn state.
        The host caller is responsible for live sandbox admission and planning.
        """
        if (not isinstance(request_key, str) or not re.fullmatch(r"[a-f0-9]{64}", request_key)
                or type(turn_id) is not int or turn_id <= 0
                or (project_id is not None and (type(project_id) is not int or project_id <= 0))
                or (expected_version is not None and (type(expected_version) is not int or expected_version <= 0))
                or type(max_parallelism) is not int or not 1 <= max_parallelism <= 6
                or type(max_steps) is not int or not 1 <= max_steps <= 12
                or type(timeout_seconds) is not int or not 30 <= timeout_seconds <= 3600):
            raise ValueError("Invalid project task preparation")
        if not callable(preflight):
            raise ValueError("Project preparation requires backend preflight")
        if (packet.scope_key != access.scope.key
                or str(packet.requester_user_id) != access.scope.actor_native_user_id
                or str(dispatch.get("bot_id", "")) != access.scope.bot_native_user_id):
            raise PermissionError("Project preparation context belongs to another identity")
        if "deadline" in dispatch:
            raise ValueError("Task deadline starts after project preparation")
        if decision is not None:
            restored = EntryDecision.from_payload(decision.as_payload(), max_steps=max_steps)
            if restored.mode not in {"delegate", "workflow"}:
                raise ValueError("Project preparation requires a new executable task")
        envelope = {"request_key": request_key, "turn_id": turn_id,
                    "packet": packet.as_payload(), "decision": decision.as_payload() if decision else None,
                    "dispatch": dispatch, "max_parallelism": max_parallelism,
                    "max_steps": max_steps, "timeout_seconds": timeout_seconds}
        # Copy caller-owned containers before persisting or computing the digest.
        serialized = json.dumps(envelope, ensure_ascii=False, allow_nan=False)
        if len(serialized.encode()) > 1024 * 1024:
            raise ValueError("Project task context exceeds the preparation limit")
        envelope = json.loads(serialized)
        now = int(time.time()) if now is None else now
        with self.projects._transaction() as cursor:
            self.projects._scope_lock(cursor, access)
            prior = self._prior(cursor, access, request_key)
            if prior is not None:
                if (prior["action"] != "task_prepare"
                        or prior["plan"].get("task_admission", {}).get("envelope") != envelope
                        or (project_id is not None and prior["project_id"] != project_id)):
                    raise ProjectConflict("Project preparation request identity was reused")
                return self._receipt(cursor, access, prior)
            require_running_turn(cursor, access, turn_id)
            trigger = cursor.execute("SELECT trigger_canonical_message_id FROM agent_turns WHERE turn_id=?",
                                     (turn_id,)).fetchone()[0]
            if trigger != packet.trigger_message_id:
                raise PermissionError("Task context does not belong to its triggering message")
            focus = self.projects._current(cursor, access, now)
            if focus is None or (project_id is not None and focus["project_id"] != project_id):
                raise ProjectConflict("Current project changed or expired")
            project_id = focus["project_id"]
            project = self.projects._get(cursor, access, project_id, edit=True, expected=expected_version)
            if not project["bot_native_user_id"]:
                raise PermissionError("Project requires a bot account binding")
            if self.bindings._prior(cursor, access, request_key) is not None:
                raise ProjectConflict("Task request already has a different admission")
            ProjectLifecycle._idle(cursor, project)
            if cursor.execute(
                """SELECT 1 FROM project_turn_saves s JOIN agent_turns t USING(turn_id)
                WHERE s.project_id=? AND s.state='pending' AND s.turn_id<>?
                AND (t.status='running' OR t.finished_at IS NULL) LIMIT 1""",
                (project_id, turn_id),
            ).fetchone():
                raise ProjectConflict("Another project turn still owns unsaved work")
            baseline = ProjectLifecycle._baseline(cursor, project_id)
            rebuild = (self.projects._manifest(cursor, access, project_id, baseline["version_no"]).get("rebuild")
                       if baseline else None)
            generation, plan = transition_plan(project, "task_prepare", baseline, rebuild=rebuild)
            if generation >= 2**53:
                raise ProjectConflict("Workspace generation exhausted")
            plan["task_admission"] = {"envelope": envelope,
                                      "intent_sha256": _digest({"project_id": project_id, **envelope})}
            preflight(plan)
            job, run = self.lifecycle._enqueue(cursor, access, project, "task_prepare", generation,
                                               plan, "task:" + request_key, now)
            # All existing effects have settled and new IO is now locked out.
            # This one snapshot includes the triggering turn's pending writes.
            cursor.execute(
                """UPDATE project_turn_saves SET state='queued',job_id=?,error_code='',updated_at=?
                WHERE project_id=? AND state='pending'""", (job.job_id, now, project_id),
            )
            return self._receipt(cursor, access, run)

    def _bind_saved(self, cursor, access, run, snapshot, *, now):
        """Called inside lifecycle completion, before releasing its DB row lock."""
        stored = cursor.execute("""SELECT r.* FROM project_lifecycle_runs r JOIN durable_jobs j USING(job_id)
            WHERE r.job_id=? AND r.status='completed' AND j.status='running'""", (run["job_id"],)).fetchone()
        if stored is None or any(stored[key] != run[key] for key in (
            "project_id", "actor_principal_id", "request_key", "action", "target_state", "generation", "plan"
        )):
            raise ProjectConflict("Task preparation is not completing its own lifecycle journal")
        admission = run["plan"].get("task_admission", {})
        envelope = admission.get("envelope", {})
        if (run["action"] != "task_prepare" or run["target_state"] != "stopped"
                or access.principal_id != run["actor_principal_id"]
                or admission.get("intent_sha256") != _digest({"project_id": run["project_id"], **envelope})
                or run["request_key"] != "task:" + envelope.get("request_key", "")):
            raise ProjectConflict("Project preparation envelope changed")
        project = self.projects._get(cursor, access, run["project_id"], edit=True)
        baseline = ProjectLifecycle._baseline(cursor, run["project_id"])
        if (baseline != snapshot or project["workspace_generation"] != run["generation"]
                or self.bindings._prior(cursor, access, envelope["request_key"]) is not None):
            raise ProjectConflict("Project preparation lost its saved workspace")
        ProjectLifecycle._idle(cursor, project)
        self.bindings._clean_baseline(cursor, project, baseline)
        self.bindings._manifest(cursor, access, run["project_id"], baseline)
        packet = ContextPacket.from_payload(envelope["packet"])
        if (packet.scope_key != access.scope.key
                or str(packet.requester_user_id) != access.scope.actor_native_user_id
                or str(envelope["dispatch"].get("bot_id", "")) != access.scope.bot_native_user_id):
            raise PermissionError("Saved task identity changed")
        decision = (EntryDecision.from_payload(envelope["decision"], max_steps=envelope["max_steps"])
                    if envelope["decision"] is not None else None)
        row = self.bindings._bind(cursor, access, project, baseline,
            request_key=envelope["request_key"], intent=admission["intent_sha256"], packet=packet,
            decision=decision, dispatch={**envelope["dispatch"], "deadline": now + envelope["timeout_seconds"] + 900},
            max_parallelism=envelope["max_parallelism"], max_steps=envelope["max_steps"], now=now)
        return row["task_id"]

    def status(self, access, request_key, *, entry=None, turn_id=None):
        with self.projects._transaction() as cursor:
            run = self._prior(cursor, access, request_key)
            if run is not None and entry is not None:
                envelope = run["plan"].get("task_admission", {}).get("envelope", {})
                if (envelope.get("turn_id") != turn_id
                        or envelope.get("dispatch", {}).get("project_entry") != entry):
                    raise ProjectConflict("本轮已登记另一个项目任务，请使用原任务或另发一条消息。")
            return self._receipt(cursor, access, run) if run is not None else None
