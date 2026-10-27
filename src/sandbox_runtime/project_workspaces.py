"""Fenced Docker workspaces over private helper-owned project volumes.

An unfinished operation reserves the volume until reconciliation. Docker names
identify attempts; mutations of an existing container use its immutable ID.
"""

from __future__ import annotations

import asyncio
import json
import re

from .docker import SandboxError
from .project_volumes import LinuxVolumeHost


class ProjectWorkspaces:
    def __init__(self, manager, volumes):
        if not manager.namespace or volumes.instance_id != manager.namespace:
            raise ValueError("Project workspaces require the owning instance driver")
        if any(character in str(volumes.root) for character in ",\0\r\n"):
            raise ValueError(
                "Project volume root cannot be represented as a Docker mount"
            )
        if isinstance(volumes.host, LinuxVolumeHost) and (
            volumes.host.docker != manager.docker_executable
            or volumes.host.docker_host != manager.docker_host
        ):
            raise ValueError(
                "Project volume and container observations must use the same Docker daemon"
            )
        self.manager, self.volumes = manager, volumes

    async def _volume(self, method, *args, **kwargs):
        worker = asyncio.create_task(asyncio.to_thread(method, *args, **kwargs))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # A journal write or host probe must finish before releasing admission.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
            worker.result()
            raise

    @staticmethod
    def labels(binding):
        workspace = binding["workspace"]
        return {
            "gaoji.app.project": str(binding["project_id"]),
            "gaoji.app.generation": str(binding["generation"]),
            "gaoji.app.workspace-token": workspace["token"],
        }

    @staticmethod
    def mount_spec(binding):
        return f"type=bind,source={binding['path']},target=/workspace,bind-propagation=rprivate"

    @staticmethod
    def startup_command(binding):
        identity = binding["workspace"]["root_identity"]
        return f"test \"$(stat -c '%d:%i' /workspace)\" = '{identity}' || exit 78; exec sleep infinity"

    async def _inspect(self, sandbox_id):
        name = self.manager._container_name(sandbox_id)
        result = await self.manager._run("docker", "inspect", name, timeout=20)
        if result.returncode:
            if any(
                marker in (result.stderr or result.stdout).lower()
                for marker in ("no such object", "no such container")
            ):
                return None
            raise SandboxError(
                "Cannot observe project container; absence is not confirmed"
            )
        try:
            records = json.loads(result.stdout)
            if (
                not isinstance(records, list)
                or len(records) != 1
                or not isinstance(records[0], dict)
            ):
                raise ValueError()
            record = records[0]
            if not re.fullmatch(r"[a-f0-9]{64}", record.get("Id", "")):
                raise ValueError()
            return record
        except (TypeError, ValueError):
            raise SandboxError("Malformed project container observation") from None

    def _validate(self, owner, binding, container):
        workspace = binding["workspace"]
        try:
            config, host, state = (
                container["Config"],
                container["HostConfig"],
                container["State"],
            )
            expected = {
                **self.labels(binding),
                "gaoji.app.instance": self.manager.namespace,
                "gaoji.app.resource": "sandbox",
                "gaoji.app.sandbox": "true",
                "qqbot.id": workspace["sandbox_id"],
                "qqbot.purpose": "project",
                "qqbot.owner": self.manager._owner_hash(owner),
            }
            mounts = container["Mounts"]
            work = [item for item in mounts if item["Destination"] == "/workspace"]
            nix = [item for item in mounts if item["Destination"] == "/nix"]
            if (
                any(
                    config["Labels"].get(key) != value
                    for key, value in expected.items()
                )
                or config["Image"] != workspace["image"]
                or config["User"] != "1000:1000"
                or config["Entrypoint"] != ["sh"]
                or config["Cmd"] != ["-lc", self.startup_command(binding)]
                or host["ReadonlyRootfs"] is not True
                or host["Privileged"] is not False
                or host.get("CapAdd")
                or "ALL" not in host["CapDrop"]
                or not {"no-new-privileges", "no-new-privileges=true"}.intersection(
                    host["SecurityOpt"]
                )
                or host["NetworkMode"] != self.manager._network_name
                or host["Memory"] != self.manager.memory_mib * 1024**2
                or host["MemorySwap"] != host["Memory"]
                or host["NanoCpus"] != self.manager.cpus * 10**9
                or host["PidsLimit"] != self.manager.pids_limit
                or type(state["Running"]) is not bool
                or state.get("Paused")
                or state.get("Restarting")
                or len(work) != 1
                or work[0]["Type"] != "bind"
                or work[0]["Source"] != binding["path"]
                or work[0]["RW"] is not True
                or work[0]["Propagation"] != "rprivate"
                or len(nix) != 1
                or nix[0]["Type"] != "volume"
                or nix[0]["Name"] != self.manager.nix_cache_volume
                or nix[0]["RW"] is not False
                or any(
                    item["Destination"]
                    not in {"/workspace", "/nix", "/tmp", "/home/sandbox"}
                    or (
                        item["Destination"] in {"/tmp", "/home/sandbox"}
                        and item["Type"] != "tmpfs"
                    )
                    for item in mounts
                )
            ):
                raise ValueError()
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SandboxError(
                "Project container ownership, mount or isolation differs from its journal"
            ) from None

    async def _observe(self, owner, binding):
        if binding["workspace"] is None:
            return None
        container = await self._inspect(binding["workspace"]["sandbox_id"])
        if container is not None:
            self._validate(owner, binding, container)
        return container

    async def _verify_running(self, binding, container):
        if container is None or container["State"]["Running"] is not True:
            raise SandboxError(
                "Project workspace is not running; completion is unverified"
            )
        observed = await self.manager._run(
            "docker",
            "exec",
            container["Id"],
            "stat",
            "-c",
            "%d:%i",
            "/workspace",
            timeout=20,
        )
        if (
            observed.returncode
            or observed.stdout.strip() != binding["workspace"]["root_identity"]
        ):
            raise SandboxError(
                "Docker cannot see the same project mount; refusing an empty or foreign workspace"
            )

    async def _capacity(self, owner):
        owned = await self.manager.list(owner)
        all_workspaces = await self.manager._list_by_label(
            f"{self.manager._sandbox_label}=true"
        )
        if (
            sum(self.manager._status_is_running(item) for item in owned)
            >= self.manager.max_per_owner
            or sum(self.manager._status_is_running(item) for item in all_workspaces)
            >= self.manager.max_total
        ):
            raise SandboxError(
                "Project workspace would exceed the running sandbox limit"
            )

    async def operate(
        self, action, owner, project_id, *, generation, fence, reservation=None
    ):
        if action not in {"start", "stop", "remove", "reserve", "release"}:
            raise SandboxError("Unsupported project workspace operation")
        async with self.manager._allocation_lock:
            # A deleted volume can still retain a reservation until its owning
            # archive job acknowledges all physical effects.
            if action == "release":
                volume = await self._volume(
                    self.volumes.status, owner, project_id, generation=generation
                )
                if volume.get("deleted"):
                    return await self._volume(
                        self.volumes.reserve,
                        owner,
                        project_id,
                        generation=generation,
                        fence=fence,
                        action=action,
                        reservation=reservation,
                    )
            binding = await self._volume(
                self.volumes.workspace,
                owner,
                project_id,
                generation=generation,
                allow_pending=True,
                maintenance=True,
            )
            current = await self._observe(owner, binding)
            if (
                action == "start"
                and not binding["mount_matches"]
                and current
                and current["State"]["Running"]
            ):
                raise SandboxError(
                    "Stop the old project container before recovering its mount"
                )
            if action == "remove" and current and current["State"]["Running"]:
                raise SandboxError("Stop the project workspace before removal")
            if action == "stop" and current is None:
                raise SandboxError(
                    "Missing project container cannot be reported as stopped"
                )
            if action == "start" and (
                current is None or not current["State"]["Running"]
            ):
                await self._capacity(owner)
            if binding["workspace"]:
                sandbox_id = binding["workspace"]["sandbox_id"]
                self.manager._ensure_idle(sandbox_id)
                if self.manager._exec_locks.setdefault(
                    sandbox_id, asyncio.Lock()
                ).locked():
                    raise SandboxError(
                        "Project workspace has an active command or file transfer"
                    )
            if action in {"reserve", "release"}:

                async def change_reservation():
                    return await self._volume(
                        self.volumes.reserve,
                        owner,
                        project_id,
                        generation=generation,
                        fence=fence,
                        action=action,
                        reservation=reservation,
                    )

                if binding["workspace"]:
                    async with self.manager._exec_locks[sandbox_id]:
                        return await change_reservation()
                return await change_reservation()
            binding = await self._volume(
                self.volumes.begin_workspace,
                owner,
                project_id,
                generation=generation,
                fence=fence,
                action=action,
                image=self.manager.image,
                reservation=reservation,
            )
            sandbox_id = (binding["workspace"] or {}).get("sandbox_id")
            if sandbox_id is None:
                await self._volume(
                    self.volumes.finish_workspace,
                    owner,
                    project_id,
                    generation=generation,
                    fence=fence,
                    action=action,
                    reservation=reservation,
                )
                return await self.status(owner, project_id, generation=generation)
            async with self.manager._exec_locks.setdefault(sandbox_id, asyncio.Lock()):
                current = await self._observe(owner, binding)
                # Replaying an acknowledged fence only checks its result. It
                # must not silently re-execute a start after an external stop.
                if binding["pending"]:
                    if action == "start":
                        if not binding["mount_matches"]:
                            previous = binding["workspace"]
                            if (
                                not previous
                                or not previous.get("boot_id")
                                or previous["boot_id"] == binding["boot_id"]
                            ):
                                raise SandboxError(
                                    "Mount recovery requires proof of a host reboot"
                                )
                            if current is not None:
                                if current["State"]["Running"]:
                                    raise SandboxError(
                                        "Old project container is still running"
                                    )
                                await self._command("rm", current)
                            if await self._observe(owner, binding) is not None:
                                raise SandboxError(
                                    "Old container removal was not confirmed"
                                )
                            binding = await self._volume(
                                self.volumes.recover_workspace,
                                owner,
                                project_id,
                                generation=generation,
                                fence=fence,
                                reservation=reservation,
                            )
                            current = await self._observe(owner, binding)
                        if current is None:
                            await self.manager._create(
                                owner, "debian", purpose="project", project=binding
                            )
                        elif not current["State"]["Running"]:
                            await self._command("start", current)
                    elif action == "stop":
                        if current is None:
                            raise SandboxError(
                                "Project container disappeared before stop"
                            )
                        if current["State"]["Running"]:
                            await self._command("stop", current)
                    elif current is not None:
                        if current["State"]["Running"]:
                            raise SandboxError(
                                "Project container restarted; refusing removal"
                            )
                        await self._command("rm", current)
                current = await self._observe(owner, binding)
                if action == "start":
                    await self._verify_running(binding, current)
                elif action == "stop" and (
                    current is None or current["State"]["Running"]
                ):
                    raise SandboxError("Project stop was not confirmed")
                elif action == "remove" and current is not None:
                    raise SandboxError("Project container removal was not confirmed")
                await self._volume(
                    self.volumes.finish_workspace,
                    owner,
                    project_id,
                    generation=generation,
                    fence=fence,
                    action=action,
                    reservation=reservation,
                )
                return await self.status(owner, project_id, generation=generation)

    async def _command(self, action, container):
        options = ("--time", "5") if action == "stop" else ()
        result = await self.manager._run(
            "docker", action, *options, container["Id"], timeout=30
        )
        if result.returncode:
            raise SandboxError(
                f"Project {action} did not confirm success; reconcile the existing operation"
            )

    async def status(self, owner, project_id, *, generation):
        binding = await self._volume(
            self.volumes.workspace,
            owner,
            project_id,
            generation=generation,
            allow_pending=True,
            maintenance=True,
        )
        container = await self._observe(owner, binding)
        state = (
            ("missing" if binding["workspace"] else "absent")
            if container is None
            else "running"
            if container["State"]["Running"]
            else "stopped"
        )
        if not binding["mount_matches"] and binding["workspace"]:
            state = "needs_recovery"
        elif state == "running":
            await self._verify_running(binding, container)
        return {
            "project_id": project_id,
            "generation": generation,
            "fence": binding["fence"],
            "sandbox_id": (binding["workspace"] or {}).get("sandbox_id"),
            "state": state,
            "pending": binding["pending"],
            "verified": not binding["pending"]
            and state not in {"missing", "needs_recovery"},
            "mounted": binding["mounted"],
            "maintenance_active": binding["maintenance_active"],
        }

    async def for_sandbox(self, owner, sandbox_id):
        container = await self._inspect(sandbox_id)
        if container is None:
            raise SandboxError("Project container does not exist")
        try:
            labels = container["Config"]["Labels"]
            if labels.get("qqbot.purpose") != "project":
                return None
            binding = await self._volume(
                self.volumes.workspace,
                owner,
                int(labels["gaoji.app.project"]),
                generation=int(labels["gaoji.app.generation"]),
            )
            if (
                binding["workspace"] is None
                or binding["workspace"]["sandbox_id"] != sandbox_id
            ):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise SandboxError(
                "Project container has no current workspace binding"
            ) from None
        self._validate(owner, binding, container)
        await self._verify_running(binding, container)
        return binding

    async def authorize(self, owner, sandbox_id, labels, *, require_running):
        binding = await self.for_sandbox(owner, sandbox_id)
        if binding is None or self.labels(binding) != {
            key: labels.get(key) for key in self.labels(binding)
        }:
            raise SandboxError("Project workspace changed during admission")
        container = await self._observe(owner, binding)
        if container is None or (require_running and not container["State"]["Running"]):
            raise SandboxError("Project workspace is unavailable")
        return container["Id"]
