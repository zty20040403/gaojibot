"""Leased project execution over the private helper, disabled until wired by host.

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
        }
    )
    VERIFICATIONS = frozenset(
        {"verify_active", "verify_stopped", "verify_hibernated", "verify_archived"}
    )

    def __init__(self, client, *, files=None):
        self.client, self.files = client, files

    async def sample_usage(self, project):
        if project["workspace_generation"] == 0:
            return {"project_id": project["project_id"], "generation": 0, "fence": 0,
                    "quota_bytes": project["quota_bytes"], "quota_mode": "hard", "used_bytes": 0,
                    "rebuildable_bytes": 0, "complete": True, "sampled_at": int(time.time())}
        return await self.client.project_usage(
            project["scope_key"], project["project_id"], generation=project["workspace_generation"],
            rebuild_command=project["rebuild_command"],
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
    def __init__(self, lifecycle, backends, *, resolve_access):
        self.lifecycle, self.backends = lifecycle, dict(backends)
        self.resolve_access = resolve_access

    def register(self, worker):
        """Explicit host composition only; importing this file registers nothing."""
        worker.register(KIND, self)

    async def __call__(self, job):
        try:
            project, run = await _store(self.lifecycle.context, job)
            backend = self.backends.get(run["plan"]["backend"])
            if backend is None:
                raise ProjectBackendUnavailable(
                    "Project backend is not available on this host"
                )
            backend.preflight(run["plan"])
            while True:
                access = await self._access(project, run)
                project, run = await _store(self.lifecycle.context, job, access=access)
                step = await _store(self.lifecycle.next_step, job)
                if step is None:
                    return await _store(self.lifecycle.finish, job)
                evidence = await backend.execute(job, step, access)
                # Membership can be revoked while a long helper operation runs.
                await _store(
                    self.lifecycle.context, job, access=await self._access(project, run)
                )
                await _store(self.lifecycle.acknowledge, job, step, evidence)
        except Exception:
            try:
                await _store(self.lifecycle.needs_attention, job)
            except ProjectConflict:
                pass
            raise

    async def _access(self, project, run):
        access = self.resolve_access(project, run["actor_principal_id"])
        access = await access if inspect.isawaitable(access) else access
        if not isinstance(access, ProjectAccess):
            raise PermissionError(
                "A current host-resolved project identity is required"
            )
        return access
