"""Protocol-3 VM lifecycle adapter. Host composition remains explicit."""

import re
import time

from .project_executor import DockerProjectBackend, ProjectBackendUnavailable, project_io_evidence
from .projects import ProjectConflict


class VmProjectBackend:
    quota_mode = "hard"
    ACTIONS = {
        "create_volume": "create",
        "delete_volume": "delete",
        "reserve_workspace": "reserve",
        "release_workspace": "release",
        "boot_workspace": "boot",
        "quiesce_workspace": "quiesce",
        "trim_volume": "trim",
        "unmount_volume": "unmount",
        "stop_workspace": "stop",
        "remove_workspace": "remove",
    }
    FILES = DockerProjectBackend.FILES
    VERIFICATIONS = DockerProjectBackend.VERIFICATIONS

    def __init__(self, runtime, *, files):
        self.runtime, self.files = runtime, files

    @staticmethod
    def _settled(project, observed):
        target = project["status"]
        if target in {"hibernated", "archived"}:
            attached = (
                observed.get("retired") is True
                and not observed.get("attached")
                and not observed.get("sandbox_id")
            )
        else:
            attached = (
                observed.get("attached") is True
                and observed.get("sandbox_id") == project["sandbox_id"]
            )
        return (
            observed.get("verified") is True
            and observed.get("project_id") == project["project_id"]
            and observed.get("generation") == project["workspace_generation"]
            and observed.get("fence") == project["lifecycle_fence"] - 1
            and observed.get("quota_bytes") == project["quota_bytes"]
            and not observed.get("pending")
            and not observed.get("maintenance_active")
            and observed.get("deleted") is (target == "archived")
            and observed.get("state")
            == {
                "active": "running",
                "stopped": "stopped",
                "hibernated": "absent",
                "archived": "absent",
            }.get(target)
            and attached
        )

    async def sample_usage(self, project):
        if project["lifecycle_job_id"] is not None:
            raise ProjectConflict("Project usage waits for lifecycle completion")
        if project["workspace_generation"] == 0:
            if project["status"] != "stopped" or project["sandbox_id"]:
                raise ProjectConflict(
                    "Uninitialized VM project has unexpected runtime state"
                )
            return {
                "project_id": project["project_id"],
                "generation": 0,
                "fence": 0,
                "quota_bytes": project["quota_bytes"],
                "quota_mode": "hard",
                "used_bytes": 0,
                "rebuildable_bytes": 0,
                "complete": True,
                "sampled_at": int(time.time()),
            }
        observed = await self.runtime.sample_usage(
            project["scope_key"],
            project["project_id"],
            generation=project["workspace_generation"],
            rebuild_command=project["rebuild_command"],
        )
        if not self._settled(project, observed):
            raise ProjectConflict(
                "VM accounting does not match the committed lifecycle"
            )
        return observed

    async def maintenance_ready(self, project):
        if (
            project["lifecycle_job_id"] is not None
            or project["locked_task_id"] is not None
        ):
            return False
        if project["workspace_generation"] == 0:
            return project["status"] == "stopped" and not project["sandbox_id"]
        observed = await self.runtime.inspect(
            project["scope_key"],
            project["project_id"],
            generation=project["workspace_generation"],
        )
        return self._settled(project, observed) and observed.get("busy") is False

    async def io_evidence(self, project):
        observed = await self.runtime.inspect(
            project["scope_key"], project["project_id"],
            generation=project["workspace_generation"],
        )
        return project_io_evidence(project, (
            project["status"] == "active" and self._settled(project, observed)
            and observed.get("busy") is False
        ))

    async def current_notes(self, project):
        await self.io_evidence(project)
        result = await self.runtime.manager.read_project_notes(
            project["scope_key"], project["sandbox_id"], project_id=project["project_id"],
            generation=project["workspace_generation"], fence=project["lifecycle_fence"] - 1,
        )
        await self.io_evidence(project)
        return result

    def preflight(self, plan):
        steps = plan.get("steps")
        if (
            plan.get("protocol") != 3
            or plan.get("backend") != "vm"
            or not isinstance(plan.get("reservation"), str)
            or not re.fullmatch(r"[a-f0-9]{32}", plan["reservation"])
            or not isinstance(steps, list)
            or not steps
            or any(not isinstance(s, str) for s in steps)
        ):
            raise ProjectBackendUnavailable(
                "VM lifecycle needs a reserved protocol-3 plan"
            )
        supported = set(self.ACTIONS) | self.VERIFICATIONS
        if self.files is not None:
            supported |= self.FILES & self.files.operations
        if set(steps) - supported:
            raise ProjectBackendUnavailable("VM lifecycle adapters are incomplete")
        candidates = [
            [
                "reserve_workspace", "quiesce_workspace", "snapshot_workspace",
                "release_workspace", "verify_active",
            ],
            [
                "reserve_workspace", "boot_workspace", "snapshot_workspace",
                "stop_workspace", "release_workspace", "verify_stopped",
            ],
            [
                "reserve_workspace",
                "quiesce_workspace",
                "snapshot_workspace",
                "stop_workspace",
                "release_workspace",
                "verify_stopped",
            ],
            [
                "reserve_workspace",
                "boot_workspace",
                "snapshot_workspace",
                "prune_rebuildable",
                "trim_volume",
                "unmount_volume",
                "remove_workspace",
                "release_workspace",
                "verify_hibernated",
            ],
            [
                "reserve_workspace",
                "boot_workspace",
                "snapshot_workspace",
                "unmount_volume",
                "remove_workspace",
                "delete_volume",
                "release_workspace",
                "verify_archived",
            ],
        ]
        for create in (False, True):
            for restore in (False, True):
                for rebuild in (False, True):
                    if restore and not create:
                        continue
                    candidates.append(
                        (["create_volume"] if create else [])
                        + ["reserve_workspace", "boot_workspace"]
                        + (["restore_version"] if restore else [])
                        + (["rebuild_dependencies"] if rebuild else [])
                        + ["release_workspace", "verify_active"]
                    )
        if steps not in candidates:
            raise ProjectBackendUnavailable("VM lifecycle operations are out of order")
        if "restore_version" in steps and not plan.get("baseline"):
            raise ProjectBackendUnavailable("VM restore requires a retained version")
        if "rebuild_dependencies" in steps and (
            not plan.get("baseline")
            or not plan.get("rebuild_spec")
            or plan["rebuild_spec"].get("command") != plan.get("rebuild_command")
        ):
            raise ProjectBackendUnavailable(
                "VM dependency rebuild requires a pinned recipe and version"
            )

    @staticmethod
    def _evidence(step, observed, **extra):
        return {
            **DockerProjectBackend._evidence(step, observed, **extra),
            "observation": "Private VM runtime verified the disk journal, immutable domain and guest admission",
        }

    async def execute(self, job, step, access):
        args, operation = step["arguments"], step["operation"]
        if args.get("backend") != "vm":
            raise ProjectBackendUnavailable(
                "VM adapter cannot execute another backend's operation"
            )
        if operation in self.FILES:
            if self.files is None or operation not in self.files.operations:
                raise ProjectBackendUnavailable("VM file adapter is unavailable")
            return await self.files.execute(job, step, access)
        if operation in self.ACTIONS:
            action = self.ACTIONS[operation]
            observed = await self.runtime.operate(
                action,
                args["owner"],
                args["project_id"],
                **{
                    k: args[k]
                    for k in ("generation", "fence", "reservation", "quota_bytes")
                },
            )
            DockerProjectBackend._identity(step, observed)
            valid = (
                observed["fence"] == args["fence"]
                and observed.get("quota_bytes") == args["quota_bytes"]
            )
            if action == "create":
                valid &= (
                    not observed.get("deleted")
                    and observed.get("attached") is False
                    and not observed.get("sandbox_id")
                )
            elif action == "delete":
                valid &= observed.get("deleted") is True and not observed.get(
                    "sandbox_id"
                )
            elif action in {"reserve", "release"}:
                valid &= observed.get("maintenance_active") is (action == "reserve")
            else:
                valid &= (
                    observed.get("verified") is True
                    and observed.get("state")
                    == {
                        "boot": "running",
                        "quiesce": "quiesced",
                        "trim": "trimmed",
                        "unmount": "unmounted",
                        "stop": "stopped",
                        "remove": "absent",
                    }[action]
                )
                valid &= observed.get(
                    "maintenance_active"
                ) is True and not observed.get("deleted")
            if not valid:
                raise ProjectConflict(
                    "VM lifecycle effect was not verified at its fence"
                )
            return self._evidence(step, observed, sandbox_id=observed.get("sandbox_id"))
        if operation not in self.VERIFICATIONS:
            raise ProjectBackendUnavailable("Unknown VM lifecycle operation")
        target = operation.removeprefix("verify_")
        observed = await self.runtime.inspect(
            args["owner"], args["project_id"], generation=args["generation"]
        )
        DockerProjectBackend._identity(step, observed)
        valid = (
            observed.get("verified") is True
            and observed["fence"] == args["fence"] - 1
            and not observed.get("maintenance_active")
            and observed.get("quota_bytes") == args["quota_bytes"]
            and observed.get("busy") is False
            and observed.get("deleted") is (target == "archived")
            and observed.get("state")
            == {
                "active": "running",
                "stopped": "stopped",
                "hibernated": "absent",
                "archived": "absent",
            }[target]
        )
        if target in {"hibernated", "archived"}:
            valid &= (
                observed.get("retired") is True
                and not observed.get("sandbox_id")
                and not observed.get("attached")
            )
        else:
            valid &= (
                bool(observed.get("sandbox_id")) and observed.get("attached") is True
            )
        if not valid:
            raise ProjectConflict("Final VM project state has not been verified")
        return self._evidence(
            step, observed, state=target, sandbox_id=observed.get("sandbox_id")
        )
