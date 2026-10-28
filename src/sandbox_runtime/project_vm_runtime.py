"""Private VM lifecycle composition, not a public RPC or production registration."""

import json
import time

from .errors import SandboxError
from .project_vm_gate import HELPER_PATH
from .project_vm_file_guest import HELPER_PATH as FILES_PATH
from .project_vm_io import _journal
from .project_vm_maintenance import VmProjectMaintenance
from .project_vm_mounts import VmProjectMounts
from .project_vm_provision import VmProjectProvisioner
from .project_vm_resize import VmProjectResize


class VmProjectRuntime:
    def __init__(self, manager, disks, *, host=None):
        self.manager, self.disks = manager, disks
        self.mounts = VmProjectMounts(manager, disks)
        self.provisioner = VmProjectProvisioner(manager, disks, host=host)
        self.maintenance = VmProjectMaintenance(manager, disks, host=host)
        self.resizer = VmProjectResize(manager, disks, host=host)
        self._browser = None

    @property
    def browser(self):
        if self._browser is None:
            from .project_browser_vm import VmProjectBrowser
            self._browser = VmProjectBrowser(self)
        return self._browser

    async def operate(
        self, action, owner, project_id, *, generation, fence, reservation, quota_bytes
    ):
        options = dict(generation=generation, fence=fence, reservation=reservation)
        if action == "create":
            return await _journal(
                self.disks.create,
                owner,
                project_id,
                generation=generation,
                fence=fence,
                quota_bytes=quota_bytes,
            )
        if action == "delete":
            return await _journal(self.disks.delete, owner, project_id, **options)
        if action in {"reserve", "release"}:
            return await self.mounts.reserve(action, owner, project_id, **options)
        if action == "boot":
            return await self.provisioner.boot(owner, project_id, **options)
        if action == "resize":
            return await self.resizer.resize(owner, project_id, quota_bytes=quota_bytes, **options)
        if action in {"quiesce", "trim", "unmount", "stop", "remove"}:
            return await self.maintenance.operate(action, owner, project_id, **options)
        raise SandboxError("Unknown project VM operation")

    @staticmethod
    def _guest_receipt(output, binding, epoch, *, usage=False):
        try:
            if len(output) > 8192:
                raise ValueError()
            receipt = json.loads(output)
            expected = {
                k: binding.guest_config()[k]
                for k in (
                    "project_id",
                    "generation",
                    "filesystem_uuid",
                    "serial",
                    "quota_bytes",
                )
            }
            expected.update(epoch=epoch, mode="active", mounted=True, verified=True)
            if (
                not isinstance(receipt, dict)
                or set(receipt)
                != (
                    {*expected, "busy", "used_bytes"}
                    | ({"rebuildable_bytes", "warning"} if usage else set())
                )
                or any(
                    type(receipt.get(k)) is not type(v) or receipt[k] != v
                    for k, v in expected.items()
                )
                or type(receipt["busy"]) is not bool
                or type(receipt["used_bytes"]) is not int
                or not 0 <= receipt["used_bytes"] <= binding.quota_bytes
            ):
                raise ValueError()
            if usage:
                rebuildable = receipt["rebuildable_bytes"]
                if (
                    (
                        rebuildable is not None
                        and (
                            type(rebuildable) is not int
                            or not 0 <= rebuildable <= receipt["used_bytes"]
                        )
                    )
                    or not isinstance(receipt["warning"], str)
                    or len(receipt["warning"]) > 256
                    or (receipt["busy"] and rebuildable is not None)
                ):
                    raise ValueError()
            return receipt
        except (ValueError, TypeError):
            raise SandboxError(
                "Guest observation did not verify active project admission"
            ) from None

    async def inspect(self, owner, project_id, *, generation):
        return await self._inspect(owner, project_id, generation=generation)

    async def sample_usage(self, owner, project_id, *, generation, rebuild_command):
        if not isinstance(rebuild_command, str) or len(rebuild_command) > 4000:
            raise SandboxError("Invalid project accounting rebuild recipe")
        return await self._inspect(
            owner, project_id, generation=generation, rebuild_command=rebuild_command
        )

    async def _inspect(self, owner, project_id, *, generation, rebuild_command=None):
        usage = rebuild_command is not None
        # The same locks as provisioning/retirement protect the external read.
        await _journal(self.disks.status, owner, project_id, generation=generation)
        self.disks._private_directory(self.manager.root.absolute())
        async with self.manager.allocation_lock():
            observed = await _journal(
                self.disks.inspection_record, owner, project_id, generation=generation
            )
            boot, status = observed["boot"], observed["status"]
            if boot is None:
                if usage:
                    raise SandboxError(
                        "Project filesystem initialization has not been verified"
                    )
                if status["deleted"]:
                    raise SandboxError("Deleted project has no verified VM retirement")
                return {
                    **status,
                    "verified": True,
                    "state": "absent",
                    "retired": False,
                    "busy": False,
                }
            manager = self.provisioner._manager(boot)
            binding = boot["binding"]
            async with manager.io_lock(binding.sandbox_id, timeout=0):
                state = await self.provisioner._observe(manager, boot)
                guest, accounting = {}, {}
                if observed["retired"]:
                    directory = manager._directory(binding.sandbox_id)
                    if (
                        state is not None
                        or directory.exists()
                        or directory.is_symlink()
                        or await _journal(self.disks.host.consumers, binding.path)
                    ):
                        raise SandboxError(
                            "Retired project VM or its consumers reappeared"
                        )
                    state = "absent"
                else:
                    if state not in {"running", "shut off"} or not status.get(
                        "attached"
                    ):
                        raise SandboxError(
                            "Project VM or its disk attachment is absent"
                        )
                    await _journal(self.provisioner._verify_files, manager, boot)
                    if state == "running":
                        request = {"action": "inspect", "epoch": status["fence"]}
                        arguments = [HELPER_PATH]
                        if usage:
                            arguments = ["-I", FILES_PATH, "--usage"]
                            request = {
                                "identity": {
                                    k: binding.guest_config()[k]
                                    for k in (
                                        "project_id",
                                        "generation",
                                        "filesystem_uuid",
                                        "serial",
                                    )
                                },
                                "epoch": status["fence"],
                                "rebuild_command": rebuild_command,
                            }
                        code, output, _ = await manager._guest_exec_raw(
                            binding.sandbox_id,
                            "/usr/bin/python3",
                            arguments,
                            timeout=15 if usage else 45,
                            input_bytes=json.dumps(request).encode(),
                        )
                        if code:
                            raise SandboxError("Guest project inspection failed")
                        guest = self._guest_receipt(
                            output, binding, status["fence"], usage=usage
                        )
                    if await self.provisioner._observe(manager, boot) != state:
                        raise SandboxError("Project VM changed during inspection")
                    state = "running" if state == "running" else "stopped"
                if usage:
                    if state == "running":
                        accounting = {
                            k: guest[k]
                            for k in ("used_bytes", "rebuildable_bytes", "warning")
                        }
                    else:
                        accounting = await _journal(
                            self.disks.offline_usage,
                            owner,
                            project_id,
                            generation=generation,
                            fence=status["fence"],
                        )
                    actual = await self.provisioner._observe(manager, boot)
                    if (
                        actual
                        != {
                            "absent": None,
                            "stopped": "shut off",
                            "running": "running",
                        }[state]
                    ):
                        raise SandboxError("Project VM changed during accounting")
                    accounting.update(complete=True, sampled_at=int(time.time()))
                final = await _journal(
                    self.disks.inspection_record,
                    owner,
                    project_id,
                    generation=generation,
                )
                # File allocation can change during observation, unlike identity/admission.
                before = {k: v for k, v in status.items() if k != "allocated_bytes"}
                after = {
                    k: v for k, v in final["status"].items() if k != "allocated_bytes"
                }
                if (
                    before != after
                    or boot != final["boot"]
                    or observed["retired"] != final["retired"]
                ):
                    raise SandboxError("Project observation was superseded")
                browser_busy = not await _journal(self.disks.browsers.maintenance_ready, {
                    "instance_id": self.disks.instance_id, "owner": owner,
                    "project_id": project_id, "generation": generation, "fence": status["fence"],
                })
                return {
                    **final["status"],
                    "verified": True,
                    "state": state,
                    "retired": observed["retired"],
                    "busy": guest.get("busy", False) or browser_busy,
                    **accounting,
                    **({"guest": guest} if guest else {}),
                }
