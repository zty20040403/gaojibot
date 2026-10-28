"""Desktop volume-subpath bindings and fail-closed soft-quota enforcement."""

import asyncio
from contextlib import asynccontextmanager
import logging

from .docker import SandboxError
from .project_workspaces import ProjectWorkspaces

log = logging.getLogger(__name__)


class DesktopProjectWorkspaces(ProjectWorkspaces):
    def mount_spec(self, binding):
        host = self.volumes.host
        return (
            f"type=volume,source={host.volume},target=/workspace,volume-nocopy,"
            f"volume-subpath={host.subpath(binding['path'])}"
        )

    def _workspace_mount_matches(self, binding, work, host):
        mounts = [
            row for row in host.get("Mounts", []) if row.get("Target") == "/workspace"
        ]
        options = mounts[0].get("VolumeOptions", {}) if len(mounts) == 1 else {}
        return (
            len(work) == 1
            and work[0]["Type"] == "volume"
            and work[0].get("Name") == self.volumes.host.volume
            and work[0]["RW"] is True
            and len(mounts) == 1
            and mounts[0].get("Type") == "volume"
            and mounts[0].get("Source") == self.volumes.host.volume
            and not mounts[0].get("ReadOnly", False)
            and options.get("NoCopy") is True
            and options.get("Subpath") == self.volumes.host.subpath(binding["path"])
            and not options.get("DriverConfig")
            and not (set(options) - {"NoCopy", "Subpath", "DriverConfig"})
        )

    async def operate(self, action, owner, project_id, **options):
        if action == "start":
            await self._volume(
                self.volumes.require_quota,
                owner,
                project_id,
                generation=options["generation"],
                maintenance=True,
            )
        return await super().operate(action, owner, project_id, **options)

    async def browser_admission(self, owner, binding, *, dependencies=False):
        await self._volume(self.volumes.require_quota, owner, binding["project_id"],
                           generation=binding["generation"], dependencies=dependencies)

    @asynccontextmanager
    async def write_guard(self, owner, sandbox_id, *, incoming=0, dependencies=False):
        binding = await self.for_sandbox(owner, sandbox_id)
        if binding is not None:
            await self._volume(
                self.volumes.require_quota,
                owner,
                binding["project_id"],
                generation=binding["generation"],
                incoming=incoming,
                dependencies=dependencies,
            )
        yield
        if binding is not None:
            # A short command can fill the project between periodic inspections.
            await self.enforce(
                owner, binding["project_id"], binding["generation"], raise_full=True
            )

    async def enforce(self, owner, project_id, generation, *, raise_full=False):
        async with self.manager._allocation_lock:
            binding, run = await self._volume(
                self.volumes.guard_binding, owner, project_id, generation=generation
            )

            async def observe():
                if run is None:
                    return await self._observe(owner, binding)
                from .project_dependencies import ProjectDependencies

                container = await self._inspect(run["workspace"]["sandbox_id"])
                if container is not None:
                    ProjectDependencies(self).validate(owner, binding, run, container)
                return container

            container = await observe()
            identity = {"instance_id": self.volumes.instance_id, "owner": owner,
                        "project_id": project_id, "generation": generation, "fence": binding["fence"]}
            browser = await self._volume(self.volumes.browsers.current, identity)
            running = bool(container and container["State"]["Running"])
            if not running and (browser is None or browser["state"] == "closed"):
                return
            if running:
                await self._verify_running(binding, container)
            try:
                await self._volume(
                    self.volumes.require_quota,
                    owner,
                    project_id,
                    generation=generation,
                    maintenance=True,
                )
            except (OSError, ValueError, SandboxError) as error:
                # Do not wait for the execution lock held by the offending writer.
                # Stop both independent process groups, even if one stop fails.
                try:
                    await self.browser.stop_for_quota(identity)
                finally:
                    if running:
                        await self._command("stop", container)
                        current = await observe()
                        if current is None or current["State"]["Running"]:
                            raise SandboxError("Soft quota stop is unverified") from error
                log.warning("Stopped soft-quota project p%s: %s", project_id, error)
                if raise_full:
                    raise SandboxError(str(error)) from error

    async def watch(self):
        while True:
            try:
                projects = await self._volume(self.volumes.projects)
                for owner, project_id, generation in projects:
                    try:
                        await self.enforce(owner, project_id, generation)
                    except Exception:
                        log.exception(
                            "Cannot verify soft-quota project p%s", project_id
                        )
            except Exception:
                log.exception("Cannot inspect soft-quota project inventory")
            await asyncio.sleep(30)
