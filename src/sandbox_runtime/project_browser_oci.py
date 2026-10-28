"""Project browser sidecar on the owning Docker daemon, never on the Bot host.

The sidecar shares only the project's quota-backed workspace and read-only Nix
closure. Its own immutable container can be stopped without killing project
commands. Application routing and durable result publication are separate.
"""

import asyncio
import base64
from copy import deepcopy
import hashlib
import json
import logging
import os
import re

from src.local_runtime.files import read_json, write_json
from .docker import SandboxError
from .project_browser_bundle import build_bundle
from .project_browser_host import BrowserPipe, ProjectBrowserHost
from .project_browser_journal import HEX_ID, ProjectBrowserJournal
from .project_vm_io import _journal


def worker_command(python, bundle, root_identity):
    # Source is host supplied, not project supplied. Temporary source disappears
    # with this container; no credentials/config are bundled or interpolated.
    code = (
        "import base64,os,runpy,tempfile\n"
        "s=os.stat('/workspace',follow_symlinks=False)\n"
        f"if str(s.st_dev)+':'+str(s.st_ino)!={root_identity!r}: raise SystemExit(78)\n"
        "with tempfile.TemporaryDirectory(prefix='gaoji-browser-') as root:\n"
        " path=os.path.join(root,'worker.pyz')\n"
        f" with open(path,'wb') as stream: stream.write(base64.b64decode({base64.b64encode(bundle).decode()!r}))\n"
        " runpy.run_path(path,run_name='__main__')\n"
    )
    return [python, "-I", "-c", code]


class OciProjectBrowser:
    def __init__(self, workspaces):
        self.workspaces, self.manager = workspaces, workspaces.manager
        self.journal = workspaces.volumes.browsers
        self.host = ProjectBrowserHost(self.journal)
        self.processes = {}
        self.sessions = {}

    def _path(self, record):
        sid = record["session_id"]
        if not isinstance(sid, str) or HEX_ID.fullmatch(sid) is None:
            raise SandboxError("Invalid browser session")
        return self.journal.root / f"oci-{sid}.json"

    def _save(self, record, spec):
        write_json(self._path(record), spec, compact=True)

    def _load(self, record):
        path = self._path(record)
        ProjectBrowserJournal._private_file(path, limit=256 * 1024)
        spec = read_json(path)
        if (spec.get("schema") != 1 or spec.get("binding") != record["binding"]
                or spec.get("runtime") != record["runtime"]
                or spec.get("session_id") != record["session_id"]
                or spec.get("phase") not in {"create_pending", "created", "starting", "removing", "removed"}
                or (spec.get("container_id") is not None
                    and (not isinstance(spec["container_id"], str)
                         or re.fullmatch(r"[a-f0-9]{64}", spec["container_id"]) is None))):
            raise SandboxError("Browser container journal differs from its launch intent")
        return spec

    async def _inspect(self, target):
        # Container configuration includes the source zipapp. The tool-display
        # character limiter would truncate its JSON and hide a real container.
        stdout, stderr, code = await self.manager._run_bytes("docker", "inspect", target, timeout=20)
        if code:
            if any(s in (stderr or stdout).lower() for s in (b"no such container", b"no such object")):
                return None
            raise SandboxError("Browser container absence is unverified")
        try:
            if len(stdout) > 256 * 1024:
                raise ValueError()
            rows = json.loads(stdout)
            if (not isinstance(rows, list) or len(rows) != 1
                    or re.fullmatch(r"[a-f0-9]{64}", rows[0]["Id"]) is None):
                raise ValueError()
            return rows[0]
        except (ValueError, TypeError, KeyError):
            raise SandboxError("Invalid browser container observation") from None

    def _spec(self, record, binding, python, bundle):
        sid = record["session_id"]
        labels = {
            "gaoji.app.instance": self.manager.namespace, "gaoji.app.resource": "helper",
            "gaoji.app.browser": "true", "gaoji.app.browser-session": sid,
            "gaoji.app.project": str(binding["project_id"]),
            "gaoji.app.generation": str(binding["generation"]),
            "gaoji.app.parent-container": record["runtime"]["runtime_id"],
            "gaoji.app.browser-source": hashlib.sha256(bundle).hexdigest(),
            "qqbot.owner": self.manager._owner_hash(record["binding"]["owner"]),
        }
        return {"schema": 1, "binding": record["binding"], "runtime": record["runtime"],
            "session_id": sid, "phase": "create_pending", "container_id": None,
            "name": f"gaoji-app-{self.manager.namespace}-helper-{sid[:16]}",
            "labels": labels, "image": binding["workspace"]["image"],
            "command": worker_command(python, bundle, binding["workspace"]["root_identity"]),
            "mount": self.workspaces.mount_spec(binding), "workspace": deepcopy(binding),
            "memory": min(1024, self.manager.memory_mib) * 1024**2,
            "network": self.manager._network_name, "cpus": 1, "pids": min(128, self.manager.pids_limit),
            "nix_volume": self.manager.nix_cache_volume}

    def _create_command(self, spec):
        args = ["docker", "create", "--pull=never", "--name", spec["name"], "-i",
            "--init", "--read-only", "--user", "1000:1000", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--network", spec["network"],
            "--memory", str(spec["memory"]), "--memory-swap", str(spec["memory"]),
            "--cpus", str(spec["cpus"]), "--pids-limit", str(spec["pids"]),
            "--sysctl", "net.ipv6.conf.all.disable_ipv6=1", "--restart", "no",
            "--log-driver", "none", "--workdir", "/workspace", "--env", "HOME=/home/sandbox",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=128m,mode=1777",
            "--tmpfs", "/home/sandbox:rw,nosuid,nodev,size=256m,uid=1000,gid=1000,mode=700",
            "--mount", spec["mount"], "--mount",
            f"type=volume,source={spec['nix_volume']},target=/nix,readonly",
            "--entrypoint", spec["command"][0]]
        for key, value in spec["labels"].items():
            args.extend(("--label", key + "=" + value))
        return [*args, spec["image"], *spec["command"][1:]]

    def _validate(self, spec, container):
        try:
            config, host, state = container["Config"], container["HostConfig"], container["State"]
            mounts = container["Mounts"]
            work = [m for m in mounts if m["Destination"] == "/workspace"]
            nix = [m for m in mounts if m["Destination"] == "/nix"]
            if (container["Name"] != "/" + spec["name"]
                    or (spec["container_id"] and container["Id"] != spec["container_id"])
                    or any(config["Labels"].get(k) != v for k, v in spec["labels"].items())
                    or config["Image"] != spec["image"] or config["User"] != "1000:1000"
                    or config["Entrypoint"] != [spec["command"][0]] or config["Cmd"] != spec["command"][1:]
                    or config["WorkingDir"] != "/workspace" or config["Tty"] or not config["OpenStdin"]
                    or host["Privileged"] or not host["ReadonlyRootfs"] or not host["Init"]
                    or host.get("CapAdd") or "ALL" not in host["CapDrop"]
                    or not {"no-new-privileges", "no-new-privileges=true"}.intersection(host["SecurityOpt"])
                    or host["NetworkMode"] != spec["network"] or host.get("PortBindings")
                    or host["Memory"] != spec["memory"] or host["MemorySwap"] != spec["memory"]
                    or host["NanoCpus"] != spec["cpus"] * 10**9 or host["PidsLimit"] != spec["pids"]
                    or host["RestartPolicy"]["Name"] != "no" or host["LogConfig"]["Type"] != "none"
                    or host["Sysctls"].get("net.ipv6.conf.all.disable_ipv6") != "1"
                    or type(state["Running"]) is not bool or state.get("Restarting") or state.get("Paused")
                    or not self.workspaces._workspace_mount_matches(spec["workspace"], work, host)
                    or len(nix) != 1 or nix[0]["Type"] != "volume"
                    or nix[0]["Name"] != spec["nix_volume"] or nix[0]["RW"] is not False
                    or any(m["Destination"] not in {"/workspace", "/nix", "/tmp", "/home/sandbox"}
                           or (m["Destination"] in {"/tmp", "/home/sandbox"} and m["Type"] != "tmpfs")
                           for m in mounts)):
                raise ValueError()
        except (KeyError, ValueError, TypeError, AttributeError):
            raise SandboxError("Browser container ownership, mount or isolation changed") from None

    async def _spawn(self, record, spec):
        environment = {k: v for k, v in os.environ.items() if k not in {"DOCKER_HOST", "DOCKER_CONTEXT"}}
        environment["DOCKER_HOST"] = self.manager.docker_host
        starting = asyncio.create_task(asyncio.create_subprocess_exec(
            *self.manager._docker_command(("docker", "start", "-a", "-i", spec["container_id"])),
            env=environment, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL))
        try:
            process = await asyncio.shield(starting)
        except asyncio.CancelledError:
            while not starting.done():
                try:
                    await asyncio.shield(starting)
                except asyncio.CancelledError:
                    continue
            process = starting.result()
            self.processes[record["session_id"]] = process
            process.stdin.close()
            raise
        self.processes[record["session_id"]] = process
        return BrowserPipe(process.stdout, process.stdin)

    async def _admit_open(self, owner, project_id, generation, fence):
        """Caller holds allocation lock; recheck after acquiring the session lock."""
        manager, workspaces = self.manager, self.workspaces
        binding = await workspaces._volume(workspaces.volumes.workspace, owner, project_id, generation=generation)
        if (binding["fence"] != fence or not binding["workspace"]
                or binding["workspace"]["image"] != manager.image):
            raise SandboxError("Browser workspace epoch or pinned toolchain changed")
        main = await workspaces._observe(owner, binding)
        await workspaces._verify_running(binding, main)
        sid = binding["workspace"]["sandbox_id"]
        manager._ensure_idle(sid)
        lock = manager._exec_locks.setdefault(sid, asyncio.Lock())
        if lock.locked():
            raise SandboxError("Project has an active command or file transfer")
        await workspaces._capacity(owner)
        await workspaces.browser_admission(owner, binding, dependencies=True)
        await manager._verify_network_policy()
        return binding, main, lock

    async def open(self, owner, project_id, *, generation, fence, launch_id=None):
        manager = self.manager
        # Package preparation writes only the shared closure cache, never a
        # project. Admission/mounts are rechecked after this bounded preparation.
        paths = await manager._prepare_packages(("chromium", "python3Packages.playwright"), timeout_seconds=300)
        if len(paths) != 2 or any(re.fullmatch(r"/nix/store/[a-z0-9]{32}-[A-Za-z0-9+._?=-]+", p) is None for p in paths):
            raise SandboxError("Browser dependencies did not produce pinned Nix closures")
        async with manager._allocation_lock:
            binding, main, _ = await self._admit_open(owner, project_id, generation, fence)
        identity = {"instance_id": manager.namespace, "owner": owner, "project_id": project_id,
                    "generation": generation, "fence": fence}
        runtime = {"backend": "oci", "sandbox_id": binding["workspace"]["sandbox_id"], "runtime_id": main["Id"]}
        config = {"schema": 1, "binding": identity, "workspace": "/workspace",
            "executable_path": paths[0] + "/bin/chromium", "allow_private_network": False,
            "max_profile_bytes": 256 * 1024**2, "cache_bytes": 32 * 1024**2}
        bundle = build_bundle()

        async def launch(record):
            self.sessions[record["session_id"]] = deepcopy(record)
            # The host already owns the session lock. Never hold allocation
            # while waiting for it: a concurrent drain uses session -> allocation.
            async with manager._allocation_lock:
                fresh, current, lock = await self._admit_open(owner, project_id, generation, fence)
                if fresh != binding or current["Id"] != runtime["runtime_id"]:
                    raise SandboxError("Browser workspace changed before process creation")
                async with lock:
                    spec = self._spec(record, binding, paths[1] + "/bin/python3", bundle)
                    await _journal(self._save, record, spec)
                    if await self._inspect(spec["name"]) is not None:
                        raise SandboxError("Browser container name already exists; refusing adoption")
                    result = await manager._run(*self._create_command(spec), timeout=60)
                    if result.returncode:
                        raise SandboxError("Browser container creation unconfirmed; reconcile this session")
                    container = await self._inspect(spec["name"])
                    if container is None or container["State"]["Running"]:
                        raise SandboxError("Browser container was not observed in its unstarted state")
                    self._validate(spec, container)
                    spec.update(container_id=container["Id"], phase="created")
                    await _journal(self._save, record, spec)
                    spec["phase"] = "starting"
                    await _journal(self._save, record, spec)
                    return await self._spawn(record, spec)

        result = await self.host.open(config, runtime, launch, launch_id=launch_id)
        try:
            await self._verify_sidecar(result)
        except BaseException:
            await _journal(self.journal.uncertain, identity, result["session_id"])
            raise
        return result

    async def _verify_sidecar(self, record):
        spec = await _journal(self._load, record)
        container = await self._inspect(spec["container_id"])
        if container is None or container["State"]["Running"] is not True:
            raise SandboxError("Browser process did not remain running")
        self._validate(spec, container)
        await self.workspaces._verify_running(spec["workspace"], container)

    async def execute(self, identity, session_id, sequence, method, args, *, persist=None, admission=None):
        async def validate():
            if admission is not None:
                admission()
            async with self.manager._allocation_lock:
                record = await _journal(self.journal.current, identity, session_id)
                if record["state"] != "ready":
                    raise SandboxError("Browser was stopped or fenced before dispatch")
                binding = await self.workspaces._volume(
                    self.workspaces.volumes.workspace, identity["owner"], identity["project_id"],
                    generation=identity["generation"])
                if binding["fence"] != identity["fence"]:
                    raise SandboxError("Project fence changed before browser dispatch")
                main = await self.workspaces._observe(identity["owner"], binding)
                if main is None or main["Id"] != record["runtime"]["runtime_id"]:
                    raise SandboxError("Browser parent container changed")
                await self.workspaces._verify_running(binding, main)
                await self.manager._verify_network_policy()
                await self._verify_sidecar(record)
                await self.workspaces.browser_admission(identity["owner"], binding)

        # Persisted receipt replay does not need a live container. Only a new
        # dispatch revalidates the current project, daemon and immutable targets.
        return await self.host.execute(identity, session_id, sequence, method, args,
                                       persist=persist, before_dispatch=validate)

    async def receive_pending(self, identity, session_id, *, persist=None, sequence=None):
        return await self.host.receive_pending(identity, session_id, persist=persist, sequence=sequence)

    async def acknowledge(self, identity, session_id, sequence, response_sha256):
        return await self.host.acknowledge(identity, session_id, sequence, response_sha256)

    async def stop_for_quota(self, identity):
        """Caller holds allocation lock. Do not wait for a blocked browser action."""
        record = await _journal(self.journal.current, identity)
        if record is None or record["state"] == "closed":
            return
        await _journal(self.journal.uncertain, identity, record["session_id"])
        await self._stop(record, self.host.channels.get(record["session_id"]))
        # Keep the session uncertain. Normal drain/publication reconciles any
        # in-flight response; killing the writer is not acknowledgement of it.

    async def _stop(self, record, channel):
        spec = await _journal(self._load, record)
        target = spec["container_id"] or spec["name"]
        container = await self._inspect(target)
        if container is None and spec["phase"] not in {"removing", "removed"}:
            raise SandboxError("Browser creation/removal outcome is unknown; absence alone is insufficient")
        if container is not None:
            self._validate(spec, container)
            spec["container_id"] = container["Id"]
            await _journal(self._save, record, spec)
            if channel is not None:
                try:
                    await asyncio.wait_for(channel.eof(), 5)
                except (OSError, TimeoutError):
                    pass
            # Stop addresses only this immutable browser sidecar, not its parent
            # project, other tasks or any name that may have been replaced.
            if container["State"]["Running"]:
                result = await self.manager._run("docker", "stop", "--time", "45", container["Id"], timeout=55)
                if result.returncode:
                    raise SandboxError("Browser process shutdown not confirmed")
            container = await self._inspect(spec["container_id"])
            if container is None or container["State"]["Running"] or container["State"].get("Pid") != 0:
                raise SandboxError("Browser container still owns processes or disappeared unexpectedly")
            self._validate(spec, container)
            spec["phase"] = "removing"
            await _journal(self._save, record, spec)
            result = await self.manager._run("docker", "rm", spec["container_id"], timeout=30)
            if result.returncode:
                raise SandboxError("Browser container removal unconfirmed; reconcile without restarting")
        if await self._inspect(spec["name"]) is not None:
            raise SandboxError("Browser name is still occupied; shutdown not confirmed")
        spec["phase"] = "removed"
        await _journal(self._save, record, spec)
        process = self.processes.pop(record["session_id"], None)
        if process is not None:
            await self._reap(process)
        return {"binding": record["binding"], "runtime": record["runtime"],
                "session_id": record["session_id"], "processes_stopped": True}

    async def drain(self, identity, session_id, *, cancel_pending=False):
        async def stop(record, channel):
            async with self.manager._allocation_lock:
                return await self._stop(record, channel)

        # Same order as dispatch: session -> allocation. Taking allocation first
        # can deadlock a dispatch already waiting to revalidate its container.
        result = await self.host.drain(identity, session_id, stop, cancel_pending=cancel_pending)
        self.sessions.pop(session_id, None)
        return result

    @staticmethod
    async def _reap(process):
        # This is the local Docker attach client, not evidence that the sidecar
        # has stopped. Drain stdout as well; a pending frame can fill its pipe.
        if process.stdin is not None:
            process.stdin.close()
        pending = asyncio.create_task(process.communicate())
        try:
            await asyncio.wait_for(asyncio.shield(pending), 5)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await asyncio.wait_for(asyncio.shield(pending), 10)
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async def close(self):
        """Graceful runner shutdown drains only launches owned by this process."""
        failures = []
        for sid, record in tuple(self.sessions.items()):
            try:
                await self.drain(record["binding"], sid, cancel_pending=True)
            except Exception:
                failures.append(sid)
                logging.getLogger(__name__).warning("Project browser shutdown unconfirmed: %s", sid)
            finally:
                channel = self.host.channels.pop(sid, None)
                if channel is not None:
                    try:
                        await asyncio.wait_for(channel.eof(), 5)
                    except (OSError, TimeoutError):
                        pass
                process = self.processes.pop(sid, None)
                if process is not None:
                    await self._reap(process)
                self.sessions.pop(sid, None)
        return failures
