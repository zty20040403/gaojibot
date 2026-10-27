"""One fenced, inspectable dependency container; never replay an uncertain command."""

import os
import re
import secrets
import shlex
import stat
import time
from contextlib import contextmanager

from src.local_runtime.files import read_json, write_json
from .docker import SandboxError
from .project_files import ProjectFiles
from .project_rebuild import verify_dependency_tree
from .project_volumes import MIB, ProjectVolumeError


class DependencyJournal:
    def __init__(
        self, volumes, owner, project_id, generation, fence, reservation, digest
    ):
        self.volumes, self.owner, self.pid = volumes, owner, project_id
        self.generation, self.fence, self.reservation = generation, fence, reservation
        self.arguments = {"manifest_sha256": digest}

    @contextmanager
    def locked(self):
        with self.volumes._locked(self.owner, self.pid) as (directory, record):
            self.volumes._generation(record, self.generation)
            self.volumes._require_reservation(record, self.reservation)
            if not record or record.get("deleted") or not record.get("maintenance"):
                raise ProjectVolumeError("Dependency rebuild needs a reserved volume")
            binding = self.volumes._workspace_binding(
                directory, record, maintenance=True
            )
            if not binding["mounted"] or record.get("workspace"):
                raise ProjectVolumeError(
                    "Rebuild requires a mounted, unstarted workspace"
                )
            yield directory, record, binding

    def source(self, directory, record):
        if record.get("files", {}).get("sha256") != self.arguments["manifest_sha256"]:
            raise ProjectVolumeError(
                "Rebuild version differs from its retained manifest"
            )
        source = ProjectFiles._document(
            directory, {**record, "fence": record["files"]["fence"]}
        )["manifest"]
        if not source.get("rebuild"):
            raise ProjectVolumeError("Rebuild requires a locked retained recipe")
        return source

    def verify_tree(self, directory, record, binding):
        source = self.source(directory, record)
        files = ProjectFiles(self.volumes)
        with files.root(binding["path"]) as root:
            verify_dependency_tree(files, root, record["quota_bytes"], source)
        return source

    def read(self, directory, record):
        path = directory / "dependency-run.json"
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_size > 32768
        ):
            raise ProjectVolumeError("Unsafe dependency journal")
        run = read_json(path)
        if (
            run["generation"] != self.generation
            or run["fence"] != self.fence
            or run["arguments"] != self.arguments
            or record["fence"] != self.fence
            or record["intent"]
            != {"operation": "dependencies", "arguments": self.arguments}
        ):
            raise ProjectVolumeError("Dependency operation has been superseded")
        return run

    def prepare(self, image, *, check):
        with self.locked() as (directory, record, binding):
            if self.fence > record["fence"]:
                if check or record["pending"]:
                    raise ProjectVolumeError(
                        "No matching dependency operation is ready"
                    )
                if not isinstance(image, str) or not re.fullmatch(
                    r"(?:gaoji-app-sandbox:[a-f0-9]{64}|[a-z0-9][a-z0-9./:_-]*@sha256:[a-f0-9]{64})",
                    image,
                ):
                    raise ProjectVolumeError(
                        "Dependency rebuild requires a pinned image"
                    )
                source = self.verify_tree(directory, record, binding)
                info, now = os.stat(binding["path"]), int(time.time())
                run = {
                    "generation": self.generation,
                    "fence": self.fence,
                    "arguments": self.arguments,
                    "phase": "prepared",
                    "container_id": None,
                    "created_at": now,
                    "deadline": now + 900,
                    "exit_code": None,
                    "error": "",
                    "recipe": source["rebuild"]["command"],
                    "store_paths": None,
                    "workspace": {
                        "sandbox_id": "s" + secrets.token_hex(3),
                        "token": secrets.token_hex(16),
                        "image": image,
                        "root_identity": f"{info.st_dev}:{info.st_ino}",
                        "boot_id": binding["boot_id"],
                    },
                }
                write_json(directory / "dependency-run.json", run)
                self.volumes._begin(
                    directory, record, "dependencies", self.fence, self.arguments
                )
            else:
                run = self.read(directory, record)
            return run, {**binding, "workspace": run["workspace"]}

    def update(self, **changes):
        with self.locked() as (directory, record, _):
            run = self.read(directory, record)
            run.update(changes)
            write_json(directory / "dependency-run.json", run)
            return run

    def verify(self):
        with self.locked() as (directory, record, binding):
            run = self.read(directory, record)
            if (
                binding["boot_id"] != run["workspace"]["boot_id"]
                or f"{os.stat(binding['path']).st_dev}:{os.stat(binding['path']).st_ino}"
                != run["workspace"]["root_identity"]
            ):
                raise ProjectVolumeError(
                    "Dependency mount changed; recovery needs inspection"
                )
            self.verify_tree(directory, record, binding)

    def finish(self):
        with self.locked() as (directory, record, binding):
            run = self.read(directory, record)
            if run["phase"] not in {"validated", "completed"} or run["exit_code"] != 0:
                raise ProjectVolumeError("Dependency completion is not proven")
            self.verify_tree(directory, record, binding)
            run["phase"] = "completed"
            write_json(directory / "dependency-run.json", run)
            self.volumes._finish(directory, record)
            return run


class ProjectDependencies:
    def __init__(self, workspaces):
        self.workspaces, self.manager = workspaces, workspaces.manager

    def command(self, run):
        identity = run["workspace"]["root_identity"]
        recipe = shlex.join(shlex.split(run["recipe"]))
        command = (
            f"test \"$(stat -c '%d:%i' /workspace)\" = '{identity}' || exit 78; "
            f"remaining=$(({run['deadline']} - $(date +%s))); "
            '[ "$remaining" -gt 0 ] || exit 124; '
            '[ "$remaining" -le 900 ] || remaining=900; '
            f'exec timeout --kill-after=5s "$remaining" {recipe}'
        )
        return self.manager._wrap_packages(tuple(run["store_paths"] or ()), command)

    def validate(self, owner, binding, run, container):
        self.workspaces._validate(owner, binding, container, command=self.command(run))
        try:
            if (
                container["Config"]["WorkingDir"] != "/workspace"
                or container["Config"]["Labels"].get("gaoji.app.rebuild-fence")
                != str(run["fence"])
                or container["HostConfig"]["RestartPolicy"]["Name"] != "no"
                or container["HostConfig"].get("AutoRemove") is not False
                or (run["container_id"] and container["Id"] != run["container_id"])
            ):
                raise ValueError()
            state = container["State"]
            if (
                state["Status"] not in {"created", "running", "exited"}
                or type(state["ExitCode"]) is not int
                or type(state.get("OOMKilled")) is not bool
                or not isinstance(state["StartedAt"], str)
                or not isinstance(state["FinishedAt"], str)
                or bool(state["Running"]) != (state["Status"] == "running")
            ):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise SandboxError(
                "Dependency container identity or state is unverified"
            ) from None

    async def create(self, owner, binding, run):
        manager, workspace = self.manager, run["workspace"]
        await self.workspaces._capacity(owner)
        await manager._verify_network_policy()
        await manager._ensure_nix_volume(workspace["image"])
        labels = {
            **self.workspaces.labels(binding),
            "gaoji.app.sandbox": "true",
            "gaoji.app.instance": manager.namespace,
            "gaoji.app.resource": "sandbox",
            "gaoji.app.rebuild-fence": str(run["fence"]),
            "qqbot.id": workspace["sandbox_id"],
            "qqbot.owner": manager._owner_hash(owner),
            "qqbot.runtime": "debian",
            "qqbot.purpose": "project",
        }
        command = [
            "docker",
            "create",
            "--name",
            manager._container_name(workspace["sandbox_id"]),
            "--pull=never",
            "--entrypoint",
            "sh",
            "--user",
            "1000:1000",
            "--read-only",
            "--workdir",
            "/workspace",
            "--restart",
            "no",
            "--no-healthcheck",
            "--memory",
            manager._memory_limit(),
            "--memory-swap",
            manager._memory_limit(),
            "--cpus",
            str(manager.cpus),
            "--pids-limit",
            str(manager.pids_limit),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--network",
            manager._network_name,
            "--sysctl",
            "net.ipv6.conf.all.disable_ipv6=1",
            "--log-driver",
            "local",
            "--log-opt",
            "max-size=10m",
            "--log-opt",
            "max-file=2",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=128m",
            "--tmpfs",
            "/home/sandbox:rw,nosuid,nodev,size=256m,uid=1000,gid=1000,mode=700",
            "--env",
            "HOME=/home/sandbox",
            "--env",
            "USER=sandbox",
            "--mount",
            self.workspaces.mount_spec(binding),
            "--mount",
            f"type=volume,source={manager.nix_cache_volume},target=/nix,readonly",
        ]
        for key, value in labels.items():
            command.extend(("--label", key + "=" + value))
        command.extend((workspace["image"], "-lc", self.command(run)))
        result = await manager._run(*command, timeout=60)
        if result.returncode:
            raise SandboxError(
                "Dependency container creation is unconfirmed; reconcile this operation"
            )

    @staticmethod
    def reply(journal, run):
        return {
            "project_id": journal.pid,
            "generation": journal.generation,
            "fence": journal.fence,
            **journal.arguments,
            "pending": run["phase"] != "completed",
            "state": run["phase"],
            "command": run["recipe"],
            "exit_code": run["exit_code"],
            "error": run["error"],
            "created_at": run["created_at"],
            "deadline": run["deadline"],
            "finished_at": run.get("finished_at"),
        }

    async def operate(
        self,
        action,
        owner,
        project_id,
        *,
        generation,
        fence,
        reservation,
        manifest_sha256,
    ):
        if action not in {"run", "status"}:
            raise SandboxError("Unknown dependency operation")
        journal = DependencyJournal(
            self.workspaces.volumes,
            owner,
            project_id,
            generation,
            fence,
            reservation,
            manifest_sha256,
        )
        call = self.workspaces._volume
        async with self.manager._allocation_lock:
            run, binding = await call(
                journal.prepare, self.manager.image, check=action == "status"
            )
            container = await self.workspaces._inspect(run["workspace"]["sandbox_id"])
            if container:
                self.validate(owner, binding, run, container)
            if action == "status":
                if run["phase"] == "completed":
                    if binding["pending"]:
                        raise SandboxError(
                            "Dependency completion journal still needs reconciliation"
                        )
                    if container is not None:
                        raise SandboxError(
                            "Completed dependency container was not removed"
                        )
                    await call(journal.verify)
                return self.reply(journal, run)
            if run["phase"] in {"validated", "completed", "failed"}:
                return await self.settle(journal, run, container)
            if container is None:
                if run["phase"] != "prepared" or run["container_id"]:
                    raise SandboxError(
                        "Dependency container disappeared; refusing to replay its command"
                    )
                await call(journal.verify)
                from src.project_capacity import quota_rejection
                from .project_usage import ProjectUsage

                usage = await call(
                    ProjectUsage(self.workspaces.volumes).measure,
                    owner, project_id, generation=generation, reservation=reservation,
                )
                rejected = quota_rejection(usage["used_bytes"], usage["quota_bytes"], installing_dependencies=True)
                if rejected:
                    raise SandboxError(rejected + ": clean dependencies, prune old versions, or ask an administrator to increase quota before rebuilding")
                await call(self.workspaces.volumes._admit, 256 * MIB)
                if time.time() >= run["deadline"]:
                    run = await call(
                        journal.update,
                        phase="failed",
                        error="Rebuild deadline expired before dispatch",
                    )
                    return self.reply(journal, run)
                if run["store_paths"] is None:
                    if self.manager.image != run["workspace"]["image"]:
                        raise SandboxError(
                            "Rebuild image changed before toolchain preparation"
                        )
                    packages = {"npm": (), "uv": ("uv",), "cargo": ("cargo", "rustc")}
                    paths = await self.manager._prepare_packages(
                        packages[shlex.split(run["recipe"])[0]], timeout_seconds=300
                    )
                    run = await call(journal.update, store_paths=list(paths))
                await self.create(owner, binding, run)
                container = await self.workspaces._inspect(
                    run["workspace"]["sandbox_id"]
                )
                if container is None:
                    raise SandboxError(
                        "Dependency container creation could not be verified"
                    )
                self.validate(owner, binding, run, container)
            if run["container_id"] is None:
                run = await call(
                    journal.update, container_id=container["Id"], phase="created"
                )
            state = container["State"]
            if state["Status"] == "created":
                if not state["StartedAt"].startswith("0001-"):
                    raise SandboxError(
                        "Dependency container has ambiguous execution history"
                    )
                await call(journal.verify)
                if time.time() >= run["deadline"]:
                    run = await call(
                        journal.update,
                        phase="failed",
                        error="Rebuild deadline expired before start",
                    )
                    return await self.settle(journal, run, container)
                await self.workspaces._capacity(owner)
                run = await call(journal.update, phase="starting")
                await self.workspaces._command("start", container)
                container = await self.workspaces._inspect(
                    run["workspace"]["sandbox_id"]
                )
                if container is None:
                    raise SandboxError("Dependency container disappeared after start")
                self.validate(owner, binding, run, container)
                state = container["State"]
            if state["Running"]:
                if time.time() < run["deadline"]:
                    run = await call(journal.update, phase="running")
                    return self.reply(journal, run)
                run = await call(journal.update, error="Dependency rebuild timed out")
                await self.workspaces._command("stop", container)
                container = await self.workspaces._inspect(
                    run["workspace"]["sandbox_id"]
                )
                if container is None:
                    raise SandboxError("Timed-out dependency execution is unconfirmed")
                self.validate(owner, binding, run, container)
                state = container["State"]
            if (
                state["Status"] != "exited"
                or state["Running"]
                or state["StartedAt"].startswith("0001-")
                or state["FinishedAt"].startswith("0001-")
            ):
                raise SandboxError(
                    "Dependency execution has no verified terminal result"
                )
            error = run["error"] or (
                "Dependency rebuild was killed by the memory limit"
                if state["OOMKilled"]
                else f"Dependency installer exited with code {state['ExitCode']}"
                if state["ExitCode"]
                else ""
            )
            if not error:
                try:
                    await call(journal.verify)
                except (ValueError, ProjectVolumeError):
                    error = "Rebuild inputs or retained files failed final verification"
            run = await call(
                journal.update,
                phase="failed" if error else "validated",
                error=error,
                exit_code=state["ExitCode"],
                finished_at=int(time.time()),
            )
            return await self.settle(journal, run, container)

    async def settle(self, journal, run, container):
        if container is not None:
            if container["State"]["Running"]:
                raise SandboxError(
                    "Dependency process restarted after its recorded outcome"
                )
            await self.workspaces._command("rm", container)
            if (
                await self.workspaces._inspect(run["workspace"]["sandbox_id"])
                is not None
            ):
                raise SandboxError("Dependency container removal is unconfirmed")
        if run["phase"] != "failed":
            await self.workspaces._volume(journal.verify)
            run = await self.workspaces._volume(journal.finish)
        return self.reply(journal, run)
