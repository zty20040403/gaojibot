"""Leased project execution over the private helper, explicitly wired by host.

The Docker backend handles verified workspace/volume effects. Retained-file
operations are supplied by the host storage adapter, never by model tool calls.
Preflight checks the entire frozen plan before its first physical mutation.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import time

from .project_lifecycle import KIND
from .projects import ProjectAccess, ProjectConflict
from .workers.durable_jobs import JobDeferred
from . import project_browser_drain


async def _store(method, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(method, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A DB publication must finish before its lease is handed back. Every
        # store method also verifies the lease before publishing its changes.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        task.result()
        raise


class ProjectBackendUnavailable(ValueError):
    pass


def project_io_evidence(project, ready):
    if not ready:
        raise ProjectConflict("Project execution is not physically quiescent")
    return {
        "project_id": project["project_id"],
        "sandbox_id": project["sandbox_id"],
        "generation": project["workspace_generation"],
        "fence": project["lifecycle_fence"],
        "pending": False,
        "quiescent": True,
    }


class DockerProjectBackend:
    VOLUMES = {
        "create_volume": "create",
        "mount_volume": "mount",
        "trim_volume": "trim",
        "unmount_volume": "unmount",
        "delete_volume": "delete",
    }
    WORKSPACES = {
        "reserve_workspace": "reserve",
        "release_workspace": "release",
        "start_workspace": "start",
        "stop_workspace": "stop",
        "quiesce_workspace": "stop",
        "remove_workspace": "remove",
    }
    FILES = frozenset(
        {
            "restore_version",
            "snapshot_workspace",
            "prune_rebuildable",
            "rebuild_dependencies",
            "check_restore_source",
            "publish_restore",
        }
    )
    VERIFICATIONS = frozenset(
        {"verify_active", "verify_stopped", "verify_hibernated", "verify_archived"}
    )

    def __init__(self, client, *, files=None, quota_mode="hard"):
        if quota_mode not in {"hard", "soft"}:
            raise ValueError("Unknown project quota mode")
        self.client, self.files = client, files
        self.quota_mode = quota_mode

    async def sample_usage(self, project):
        if project["workspace_generation"] == 0:
            return {"project_id": project["project_id"], "generation": 0, "fence": 0,
                    "quota_bytes": project["quota_bytes"], "quota_mode": self.quota_mode, "used_bytes": 0,
                    "rebuildable_bytes": 0, "complete": True, "sampled_at": int(time.time())}
        return await self.client.project_usage(
            project["scope_key"], project["project_id"], generation=project["workspace_generation"],
            rebuild_command=project["rebuild_command"],
        )

    async def io_evidence(self, project):
        state = await self.client.project_workspace(
            "status", project["scope_key"], project["project_id"],
            generation=project["workspace_generation"],
        )
        return project_io_evidence(project, (
            project["status"] == "active"
            and state.get("verified") is True
            and state.get("quiescent") is True and state.get("busy") is False
            and state.get("state") == "running"
            and state.get("sandbox_id") == project["sandbox_id"]
            and state.get("project_id") == project["project_id"]
            and state.get("generation") == project["workspace_generation"]
            and state.get("fence") == project["lifecycle_fence"] - 1
            and state.get("pending") is False
            and state.get("maintenance_active") is False
        ))

    async def current_notes(self, project):
        return await self.client.project_notes(
            project["scope_key"], project["project_id"],
            generation=project["workspace_generation"], fence=project["lifecycle_fence"] - 1,
        )

    async def maintenance_ready(self, project):
        if project["lifecycle_job_id"] is not None or project["locked_task_id"] is not None:
            return False
        if project["workspace_generation"] == 0:
            return project["status"] == "stopped"
        options = {"generation": project["workspace_generation"]}
        if project["status"] == "archived":
            state = await self.client.project_volume(
                "status", project["scope_key"], project["project_id"], **options
            )
            ready = state.get("deleted") is True
        else:
            state = await self.client.project_workspace(
                "status", project["scope_key"], project["project_id"], **options
            )
            ready = (
                state.get("verified") is True and state.get("busy") is False
                and state.get("state") == {
                    "active": "running", "stopped": "stopped", "hibernated": "absent",
                }.get(project["status"])
                and (project["status"] == "hibernated"
                     or state.get("sandbox_id") == project["sandbox_id"])
            )
        return (
            ready and state.get("project_id") == project["project_id"]
            and state.get("generation") == project["workspace_generation"]
            and not state.get("pending") and not state.get("maintenance_active")
        )

    def preflight(self, plan):
        if (
            plan.get("protocol") != 2
            or not isinstance(plan.get("reservation"), str)
            or not re.fullmatch(r"[a-f0-9]{32}", plan["reservation"])
            or plan.get("backend") != "oci"
            or not isinstance(plan.get("steps"), list)
            or not plan["steps"]
            or any(not isinstance(step, str) for step in plan["steps"])
        ):
            raise ProjectBackendUnavailable(
                "Lifecycle plan needs a supported reserved OCI adapter"
            )
        supported = set(self.VOLUMES) | set(self.WORKSPACES) | self.VERIFICATIONS
        if self.files is not None:
            supported |= self.FILES & self.files.operations
        missing = set(plan["steps"]) - supported
        if missing:
            raise ProjectBackendUnavailable(
                "Lifecycle adapters are unavailable: " + ", ".join(sorted(missing))
            )
        if "restore" in plan:
            from .project_restore import valid_restore_plan
            if not valid_restore_plan(plan):
                raise ProjectBackendUnavailable("Rollback operations are out of order")
        if "rebuild_dependencies" in plan["steps"] and (
            not plan.get("rebuild_spec")
            or not plan.get("baseline")
            or "start_workspace" not in plan["steps"]
            or plan["steps"].index("rebuild_dependencies")
            > plan["steps"].index("start_workspace")
        ):
            raise ProjectBackendUnavailable(
                "Dependency rebuilding requires a pinned version before workspace startup"
            )

    @staticmethod
    def _identity(step, observed):
        args = step["arguments"]
        if (
            not isinstance(observed, dict)
            or any(
                type(observed.get(key)) is not int or observed[key] != args[key]
                for key in ("project_id", "generation")
            )
            or type(observed.get("fence")) is not int
            or observed["fence"] > args["fence"]
            or observed.get("pending")
        ):
            raise ProjectConflict(
                "Helper observation is pending, superseded or belongs to another workspace"
            )

    @staticmethod
    def _evidence(step, observed, **extra):
        return {
            **{
                key: step["arguments"][key]
                for key in ("project_id", "generation", "fence")
            },
            "verified": True,
            "observation": "Private helper inspected the project volume and Docker binding",
            "helper_fence": observed["fence"],
            **extra,
        }

    async def _official_fence(self,job,access):
        from .agent.control import assert_job_owned
        assert_job_owned()
        from .project_quota import QuotaAuthority
        if type(access) is QuotaAuthority:
            # Resize uses its own administrator audit and lifecycle checks;
            # it never grants a chat actor access to project files or tools.
            if access.job_id != job.job_id:
                raise PermissionError('project_quota_job_mismatch')
            return
        from .project_authorization import official_project
        if official_project(access) is not None:
            if self.files is None:
                raise PermissionError('project_authority_unavailable')
            await _store(self.files.lifecycle.pre_rpc,job)

    async def execute(self, job, step, access):
        args, operation = step["arguments"], step["operation"]
        owner, pid, generation = args["owner"], args["project_id"], args["generation"]
        options = {
            "generation": generation,
            "fence": args["fence"],
            "request_id": step["request_id"],
            "reservation": args["reservation"],
        }
        if operation in self.FILES:
            if self.files is None or operation not in self.files.operations:
                raise ProjectBackendUnavailable("Project file adapter is unavailable")
            return await self.files.execute(job, step, access)
        if operation in self.VOLUMES:
            action = self.VOLUMES[operation]
            if action == "create":
                options.pop("reservation")
                options["quota_bytes"] = args["quota_bytes"]
            await self._official_fence(job,access)
            observed = await self.client.project_volume(action, owner, pid, **options)
            self._identity(step, observed)
            if observed["fence"] != args["fence"]:
                raise ProjectConflict("Volume effect fence was not acknowledged")
            if action == "delete":
                valid = observed.get("deleted") is True
            else:
                valid = (
                    not observed.get("deleted")
                    and observed.get("quota_bytes") == args["quota_bytes"]
                )
                if action in {"mount", "trim"}:
                    valid &= observed.get("mounted") is True
                elif action in {"create", "unmount"}:
                    valid &= (
                        observed.get("mounted") is False
                        and observed.get("attached") is False
                    )
            if not valid:
                raise ProjectConflict("Volume effect was not observed")
            return self._evidence(step, observed)
        if operation in self.WORKSPACES:
            action = self.WORKSPACES[operation]
            await self._official_fence(job,access)
            observed = await self.client.project_workspace(
                action, owner, pid, **options
            )
            self._identity(step, observed)
            if observed["fence"] != args["fence"]:
                raise ProjectConflict("Workspace effect fence was not acknowledged")
            if action in {"reserve", "release"}:
                valid = observed.get("maintenance_active") is (action == "reserve")
            else:
                valid = (
                    observed.get("verified") is True
                    and observed.get("state")
                    == {
                        "start": "running",
                        "stop": "stopped",
                        "remove": "absent",
                    }[action]
                )
            if not valid:
                raise ProjectConflict("Workspace effect was not observed")
            return self._evidence(step, observed, sandbox_id=observed.get("sandbox_id"))
        if operation not in self.VERIFICATIONS:
            raise ProjectBackendUnavailable("Unknown project operation")
        state = operation.removeprefix("verify_")
        await self._official_fence(job,access)
        volume = await self.client.project_volume(
            "status", owner, pid, generation=generation
        )
        self._identity(step, volume)
        if volume.get("maintenance_active"):
            raise ProjectConflict("Project reservation has not been released")
        sandbox_id = None
        if state == "archived":
            valid = volume.get("deleted") is True
        else:
            await self._official_fence(job,access)
            workspace = await self.client.project_workspace(
                "status", owner, pid, generation=generation
            )
            self._identity(step, workspace)
            valid = (
                not volume.get("deleted")
                and volume.get("quota_bytes") == args["quota_bytes"]
                and workspace.get("verified") is True
                and workspace.get("state")
                == {
                    "active": "running",
                    "stopped": "stopped",
                    "hibernated": "absent",
                }[state]
            )
            if state == "hibernated":
                valid &= (
                    volume.get("mounted") is False and volume.get("attached") is False
                )
            else:
                valid &= volume.get("mounted") is True
                sandbox_id = workspace.get("sandbox_id")
        if not valid:
            raise ProjectConflict("Final project state has not been verified")
        return self._evidence(step, volume, state=state, sandbox_id=sandbox_id)


class ProjectLifecycleExecutor:
    def __init__(self, lifecycle, backends, *, resolve_access, browser=None):
        self.lifecycle, self.backends = lifecycle, dict(backends)
        self.resolve_access = resolve_access
        self.browser = browser

    def register(self, worker):
        """Explicit host composition only; importing this file registers nothing."""
        worker.register(KIND, self)

    async def __call__(self, job):
        try:
            project, run = await _store(self.lifecycle.context, job)
            if run["action"] == "task_prepare" and self.lifecycle.task_admission is None:
                raise JobDeferred("Project task admission is temporarily unavailable", 30)
            backend = self.backends.get(run["plan"]["backend"])
            if backend is None:
                raise ProjectBackendUnavailable(
                    "Project backend is not available on this host"
                )
            backend.preflight(run["plan"])
            if project_browser_drain.pending(run["plan"]):
                if self.browser is None:
                    raise JobDeferred("Project browser shutdown adapter is unavailable", 30)
                await self.browser.drain_lifecycle(job, await self._access(project, run, job))
            while True:
                access = await self._access(project, run, job)
                project, run = await _store(self.lifecycle.context, job, access=access)
                step = await _store(self.lifecycle.next_step, job)
                if step is None:
                    return await _store(self.lifecycle.finish, job, access=access)
                if run.get('official_authority'):
                    access = await self._access(project, run, job)
                    await _store(self.lifecycle.context, job, access=access)
                from .adapters.onebot_route_context import start_project_effect
                managed_effect = (None if run['action'] == 'resize' else
                    await _store(start_project_effect, access.scope,
                        domain="project.lifecycle", reference=f"{job.job_id}:{step['step_index']}"))
                evidence = await backend.execute(job, step, access)
                if run.get('official_authority') or managed_effect is not None:
                    # Historical receipt only: no authority for the next effect.
                    await _store(self.lifecycle.acknowledge, job, step, evidence)
                await _store(
                    self.lifecycle.context, job, access=await self._access(project, run, job)
                )
                if not run.get('official_authority') and managed_effect is None:
                    await _store(self.lifecycle.acknowledge, job, step, evidence)
        except JobDeferred:
            # A disconnected account is a wait, not a failed physical operation.
            # Keep the persisted step intact for the next leased attempt.
            raise
        except Exception:
            try:
                await _store(self.lifecycle.needs_attention, job)
            except (ProjectConflict, PermissionError):
                pass
            raise

    async def _access(self, project, run, job=None):
        if run["action"] == "resize":
            return await _store(self.lifecycle.quota_authority, job)
        access = (self.resolve_access(project, run["actor_principal_id"], job=job)
                  if run.get('official_authority') else self.resolve_access(project, run["actor_principal_id"]))
        access = await access if inspect.isawaitable(access) else access
        if not isinstance(access, ProjectAccess):
            raise PermissionError(
                "A current host-resolved project identity is required"
            )
        return access
