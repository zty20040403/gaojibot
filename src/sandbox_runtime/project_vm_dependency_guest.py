"""Reserved guest dependency jobs, supervised independently of QGA clients."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import tempfile
import time
import uuid

from src.local_runtime.files import write_json
from src.project_capacity import quota_rejection
from src.project_content import manifest_bytes
from .project_files import ProjectFiles
from .project_rebuild import verify_dependency_tree
from .project_volume_journal import ProjectVolumeError

HELPER = "/usr/local/lib/gaoji/project_files.pyz"
UV_VERSION = "0.11.21"  # Same pinned installer as the repository CI.


def private_read(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_size > limit
        ):
            raise ProjectVolumeError("Unsafe guest dependency state")
        return stream.read(limit + 1)


class LinuxDependencyRuntime:
    @staticmethod
    def trusted_path(path):
        target = Path(path).resolve(strict=True)
        for item in (target, *target.parents):
            info = item.stat()
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise ProjectVolumeError(
                    "Dependency toolchain is not administrator-owned"
                )
        if not target.is_file():
            raise ProjectVolumeError("Dependency executable is not a regular file")
        return str(path)

    @staticmethod
    def boot_id():
        value = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if str(uuid.UUID(value)) != value:
            raise ProjectVolumeError("Guest boot identity is unavailable")
        return value

    @staticmethod
    def description(run):
        return (
            "gaoji-dependency:"
            + run["unit"]
            + ":"
            + run["arguments"]["manifest_sha256"]
        )

    @classmethod
    def start_command(cls, run):
        remaining = max(1, min(930, run["deadline"] - int(time.time()) + 30))
        return [
            "/usr/bin/systemd-run",
            "--quiet",
            f"--unit={run['unit']}",
            "--service-type=exec",
            "--uid=root",
            f"--description={cls.description(run)}",
            "--property=Restart=no",
            "--property=RemainAfterExit=yes",
            "--property=KillMode=control-group",
            "--property=TimeoutStopSec=10s",
            f"--property=RuntimeMaxSec={remaining}s",
            "--property=TasksMax=64",
            "--property=NoNewPrivileges=yes",
            "--property=ProtectControlGroups=yes",
            "--",
            "/usr/bin/python3",
            "-I",
            HELPER,
            "--dependency-worker",
            str(run["fence"]),
        ]

    def start(self, run):
        # PID 1 retains this unit after exit. A lost client reply does not cause
        # another installer invocation; the worker also journals before exec.
        subprocess.run(
            self.start_command(run),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=30,
        )

    def inspect(self, run):
        fields = (
            "Id",
            "LoadState",
            "ActiveState",
            "SubState",
            "Description",
            "User",
            "Result",
            "ExecMainStatus",
        )
        result = subprocess.run(
            [
                "/usr/bin/systemctl",
                "show",
                "--no-pager",
                "--property=" + ",".join(fields),
                run["unit"],
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
            text=True,
        )
        values = dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )
        if values.get("LoadState") == "not-found":
            return None
        if (
            result.returncode
            or set(values) != set(fields)
            or values["Id"] != run["unit"]
            or values["LoadState"] != "loaded"
            or values["Description"] != self.description(run)
            or values["User"] != "root"
            or values["ActiveState"]
            not in {"active", "activating", "inactive", "failed", "deactivating"}
        ):
            raise ProjectVolumeError(
                "Dependency supervisor identity or state is unverified"
            )
        if values["ActiveState"] in {"activating", "deactivating"} or values[
            "SubState"
        ] in {"running", "start"}:
            return "running"
        if (
            values["SubState"] == "exited"
            and values["ExecMainStatus"] == "0"
            and values["Result"] == "success"
        ):
            return "exited"
        if values["ActiveState"] in {"inactive", "failed"}:
            return "failed"
        raise ProjectVolumeError("Dependency supervisor has an ambiguous state")

    def stop(self, run):
        subprocess.run(
            ["/usr/bin/systemctl", "stop", run["unit"]],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=20,
        )

    @staticmethod
    def install_and_run(gate, run):
        recipe = shlex.split(run["command"])
        tool = recipe[0]
        tools = {
            "npm": ("nodejs", "npm"),
            "cargo": ("cargo", "rustc"),
            "uv": ("python3-venv",),
        }
        if tool not in tools:
            raise ProjectVolumeError("Unsupported dependency toolchain")
        tool_path = (
            "/opt/gaoji-dependency-tools/uv/bin/uv"
            if tool == "uv"
            else "/usr/bin/" + tool
        )

        def execute(argv):
            remaining = min(900, run["deadline"] - int(time.time()))
            if remaining <= 0:
                raise ProjectVolumeError("Dependency rebuild deadline expired")
            return gate.commands.run(argv, remaining, run["arguments"]["tasks"])

        clean = [
            "/usr/bin/env",
            "-i",
            "PATH=/usr/bin:/bin",
            "HOME=/root",
            "DEBIAN_FRONTEND=noninteractive",
        ]
        if not Path(tool_path).is_file():
            for argv in (
                [*clean, "/usr/bin/apt-get", "update", "-qq"],
                [
                    *clean,
                    "/usr/bin/apt-get",
                    "install",
                    "-y",
                    "-qq",
                    "--no-install-recommends",
                    *tools[tool],
                ],
            ):
                if execute(argv):
                    raise ProjectVolumeError("Guest toolchain installation failed")
            if tool == "uv":
                for argv in (
                    [
                        *clean,
                        "/usr/bin/python3",
                        "-m",
                        "venv",
                        "/opt/gaoji-dependency-tools/uv",
                    ],
                    [
                        *clean,
                        "/opt/gaoji-dependency-tools/uv/bin/python",
                        "-m",
                        "pip",
                        "--isolated",
                        "install",
                        "--disable-pip-version-check",
                        "--no-cache-dir",
                        "uv==" + UV_VERSION,
                    ],
                ):
                    if execute(argv):
                        raise ProjectVolumeError(
                            "Pinned uv toolchain installation failed"
                        )
        LinuxDependencyRuntime.trusted_path(tool_path)
        if tool == "uv":
            # Also repair a partially installed toolchain before user scripts run.
            python = "/opt/gaoji-dependency-tools/uv/bin/python"
            LinuxDependencyRuntime.trusted_path(python)
            if execute(
                [
                    *clean,
                    python,
                    "-m",
                    "pip",
                    "--isolated",
                    "install",
                    "--disable-pip-version-check",
                    "--no-cache-dir",
                    "uv==" + UV_VERSION,
                ]
            ):
                raise ProjectVolumeError("Pinned uv toolchain installation failed")
        command = "cd /workspace && exec " + shlex.join([tool_path, *recipe[1:]])
        return execute(
            [
                "/usr/sbin/runuser",
                "-u",
                "sandbox",
                "--",
                "/usr/bin/env",
                "-i",
                "HOME=/home/sandbox",
                "USER=sandbox",
                "PATH=/usr/bin:/bin",
                "/bin/sh",
                "-c",
                command,
            ]
        )


class GuestDependencies:
    def __init__(self, gate, volumes, *, runtime=None):
        self.gate, self.volumes = gate, volumes
        self.runtime = runtime or LinuxDependencyRuntime()

    @contextmanager
    def locked(self, fence, reservation=None):
        with self.volumes._locked(
            self.volumes.owner, self.gate.config["project_id"]
        ) as (directory, record):
            self.volumes._generation(record, self.gate.config["generation"])
            if reservation is not None:
                self.volumes._require_reservation(record, reservation)
            if not record.get("maintenance"):
                raise ProjectVolumeError("Dependency job has no active reservation")
            if (
                record["fence"] != fence
                or record["intent"]["operation"] != "dependencies"
            ):
                raise ProjectVolumeError("Dependency job was superseded")
            run = json.loads(private_read(directory / "dependency.json", 32768))
            if (
                not isinstance(run, dict)
                or type(run.get("fence")) is not int
                or run["fence"] != fence
                or run.get("identity") != self.gate.identity
                or run.get("arguments") != record["intent"]["arguments"]
                or run.get("phase")
                not in {
                    "prepared",
                    "starting",
                    "running",
                    "validated",
                    "completed",
                    "failed",
                }
                or not isinstance(run.get("unit"), str)
                or not re.fullmatch(
                    r"gaoji-dependency-[a-f0-9]{32}\.service", run["unit"]
                )
                or type(run.get("source_ready")) is not bool
                or (
                    run["phase"] != "prepared"
                    and (
                        run["source_ready"] is not True
                        or type(run.get("deadline")) is not int
                        or not isinstance(run.get("boot_id"), str)
                    )
                )
            ):
                raise ProjectVolumeError("Dependency journal identity changed")
            yield directory, record, run

    def update(self, fence, **changes):
        with self.locked(fence) as (directory, _, run):
            run.update(changes)
            write_json(directory / "dependency.json", run)
            return run

    def source(self, directory, record, run):
        encoded = private_read(
            directory / "dependency.manifest", run["arguments"]["manifest_size"]
        )
        if (
            len(encoded) != run["arguments"]["manifest_size"]
            or hashlib.sha256(encoded).hexdigest()
            != run["arguments"]["manifest_sha256"]
        ):
            raise ProjectVolumeError("Dependency manifest checksum changed")
        source = json.loads(encoded)
        if manifest_bytes(
            source, quota_bytes=record["quota_bytes"]
        ) != encoded or not source.get("rebuild"):
            raise ProjectVolumeError(
                "Dependency source is not a canonical locked version"
            )
        if run["source_ready"] and run["command"] != source["rebuild"]["command"]:
            raise ProjectVolumeError(
                "Dependency command differs from the accepted source"
            )
        return source

    def verify(self, directory, record, run):
        source = self.source(directory, record, run)
        files = ProjectFiles(self.volumes)
        with files.root(self.volumes.workspace_path) as root:
            verify_dependency_tree(files, root, record["quota_bytes"], source)
        return source

    @staticmethod
    def reply(record, run):
        state = run["phase"]
        if state == "validated" or (state == "completed" and record["pending"]):
            state = "running"
        return {
            **{k: record[k] for k in ("project_id", "generation", "fence", "pending")},
            "manifest_sha256": run["arguments"]["manifest_sha256"],
            "state": state,
            **{
                k: run.get(k)
                for k in (
                    "command",
                    "exit_code",
                    "error",
                    "created_at",
                    "deadline",
                    "finished_at",
                    "source_ready",
                )
            },
        }

    def prepare(self, action, fence, reservation, parameters, payload):
        with self.gate.maintenance(fence):
            self.volumes.admit(fence, reservation)
            if action == "dependency_begin":
                with self.volumes._locked(
                    self.volumes.owner, self.gate.config["project_id"]
                ) as (directory, record):
                    if fence > record["fence"]:
                        if record["pending"]:
                            raise ProjectVolumeError(
                                "Previous guest operation is unfinished"
                            )
                        run = {
                            "identity": self.gate.identity,
                            "fence": fence,
                            "arguments": dict(parameters),
                            "phase": "prepared",
                            "source_ready": False,
                            "command": "",
                            "created_at": int(time.time()),
                            "deadline": None,
                            "exit_code": None,
                            "error": "",
                            "unit": "gaoji-dependency-" + uuid.uuid4().hex + ".service",
                            "boot_id": None,
                        }
                        # Publish private staging files before admitting the fence.
                        # A crash here may repeat preparation, never an installer.
                        path = directory / "dependency.manifest"
                        if path.exists() or path.is_symlink():
                            private_read(path, 64 * 1024 * 1024)
                        fd, temporary = tempfile.mkstemp(
                            prefix=".dependency-", dir=directory
                        )
                        try:
                            os.fsync(fd)
                            os.replace(temporary, path)
                        finally:
                            os.close(fd)
                            if os.path.exists(temporary):
                                os.unlink(temporary)
                        write_json(directory / "dependency.json", run)
                        self.volumes._begin(
                            directory, record, "dependencies", fence, parameters
                        )
                    elif record["intent"] != {
                        "operation": "dependencies",
                        "arguments": parameters,
                    }:
                        raise ProjectVolumeError(
                            "Dependency preparation parameters changed"
                        )
            with self.locked(fence, reservation) as (directory, record, run):
                if parameters["manifest_sha256"] != run["arguments"]["manifest_sha256"]:
                    raise ProjectVolumeError(
                        "Dependency source belongs to another version"
                    )
                if action == "dependency_manifest":
                    if run["phase"] != "prepared":
                        raise ProjectVolumeError(
                            "Accepted dependency source cannot be changed"
                        )
                    path = directory / "dependency.manifest"
                    private_read(path, run["arguments"]["manifest_size"])
                    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
                    try:
                        ProjectFiles._append(
                            fd,
                            parameters["offset"],
                            payload,
                            run["arguments"]["manifest_size"],
                        )
                        complete = (
                            os.fstat(fd).st_size == run["arguments"]["manifest_size"]
                        )
                    finally:
                        os.close(fd)
                    if complete:
                        source = self.verify(directory, record, run)
                        run.update(
                            source_ready=True, command=source["rebuild"]["command"]
                        )
                        write_json(directory / "dependency.json", run)
                return self.reply(record, run)

    def operate(self, action, fence, reservation, parameters, payload=b""):
        with self.gate._lock("dependency-control"):
            if action in {"dependency_begin", "dependency_manifest"}:
                # A restarted host first replays preparation, even while the
                # installer owns the guest IO lock. Never block that worker.
                if action == "dependency_begin":
                    with self.volumes._locked(
                        self.volumes.owner, self.gate.config["project_id"]
                    ) as (_, record):
                        replay = record["fence"] == fence and record["intent"] == {
                            "operation": "dependencies",
                            "arguments": parameters,
                        }
                    if replay:
                        with self.locked(fence, reservation) as (_, record, run):
                            return self.reply(record, run)
                return self.prepare(action, fence, reservation, parameters, payload)
            with self.locked(fence, reservation) as (_, record, run):
                if (
                    parameters["manifest_sha256"] != run["arguments"]["manifest_sha256"]
                    or not run["source_ready"]
                ):
                    raise ProjectVolumeError("Dependency source has not been verified")
            state = self.runtime.inspect(run)
            if run["phase"] == "completed":
                if state == "running":
                    raise ProjectVolumeError(
                        "Completed dependency worker is running again"
                    )
                with (
                    self.gate.maintenance(fence),
                    self.locked(fence, reservation) as (directory, record, run),
                ):
                    self.verify(directory, record, run)
                    if run["exit_code"] != 0:
                        raise ProjectVolumeError(
                            "Dependency completion has no successful exit"
                        )
                    self.volumes._finish(directory, record)
                    return self.reply(record, run)
            if run["phase"] == "failed":
                if state is not None:
                    self.runtime.stop(run)
                    if self.runtime.inspect(run) == "running":
                        raise ProjectVolumeError(
                            "Failed dependency supervisor is still running"
                        )
                self.gate.quiesce(fence)
                return self.reply(record, run)
            if run["phase"] == "prepared":
                if state is not None:
                    raise ProjectVolumeError(
                        "Unjournalled dependency supervisor already exists"
                    )
                if action == "dependency_status":
                    return self.reply(record, run)
                with (
                    self.gate.maintenance(fence),
                    self.locked(fence, reservation) as (directory, record, run),
                ):
                    self.verify(directory, record, run)
                    usage = self.gate.workspace.verify()
                    rejected = quota_rejection(
                        usage["used_bytes"],
                        usage["quota_bytes"],
                        installing_dependencies=True,
                    )
                    if rejected:
                        raise ProjectVolumeError(rejected)
                    run.update(
                        phase="starting",
                        boot_id=self.runtime.boot_id(),
                        deadline=int(time.time()) + 900,
                    )
                    write_json(directory / "dependency.json", run)
            if (
                run["phase"] == "starting"
                and state is None
                and run["boot_id"] == self.runtime.boot_id()
                and int(time.time()) < run["deadline"]
            ):
                if action == "dependency_run":
                    self.runtime.start(run)
                    state = self.runtime.inspect(run)
            with self.locked(fence, reservation) as (_, record, run):
                pass
            if state == "running" and int(time.time()) < run["deadline"]:
                return self.reply(record, run)
            if state == "running":
                self.runtime.stop(run)
                if self.runtime.inspect(run) == "running":
                    raise ProjectVolumeError(
                        "Dependency timeout termination is unconfirmed"
                    )
            if run["phase"] == "validated":
                if state is not None:
                    self.runtime.stop(run)
                    if self.runtime.inspect(run) == "running":
                        raise ProjectVolumeError(
                            "Dependency supervisor is still running"
                        )
                with (
                    self.gate.maintenance(fence),
                    self.locked(fence, reservation) as (directory, record, run),
                ):
                    self.verify(directory, record, run)
                    if run["exit_code"] != 0:
                        raise ProjectVolumeError(
                            "Dependency completion has no successful exit"
                        )
                    run["phase"] = "completed"
                    write_json(directory / "dependency.json", run)
                    self.volumes._finish(directory, record)
                    return self.reply(record, run)
            if (
                run["phase"] == "starting"
                and state is None
                and run["boot_id"] == self.runtime.boot_id()
                and int(time.time()) < run["deadline"]
            ):
                return self.reply(record, run)
            # Reboot/lost worker is not permission to rerun possibly completed scripts.
            self.gate.quiesce(fence)
            run = self.update(
                fence,
                phase="failed",
                error="Dependency worker interrupted, expired or lost its result; command was not replayed",
                finished_at=int(time.time()),
            )
            return self.reply(record, run)

    def worker(self, fence):
        self.gate._epoch(fence)
        with self.gate.maintenance(fence):
            with self.locked(fence) as (directory, record, run):
                if (
                    run["phase"] != "starting"
                    or run["boot_id"] != self.runtime.boot_id()
                ):
                    raise ProjectVolumeError(
                        "Dependency worker cannot repeat this operation"
                    )
                if int(time.time()) >= run["deadline"]:
                    raise ProjectVolumeError("Dependency rebuild deadline expired")
                self.verify(directory, record, run)
                run["phase"] = "running"
                write_json(directory / "dependency.json", run)
            try:
                code = self.runtime.install_and_run(self.gate, run)
                if type(code) is not int or code != 0:
                    raise ProjectVolumeError(
                        f"Dependency installer exited with code {code}"
                    )
                if (
                    not self.gate.commands.empty()
                    or self.gate.commands.sandbox_processes()
                ):
                    raise ProjectVolumeError("Dependency child processes remain")
                with self.locked(fence) as (directory, record, run):
                    self.verify(directory, record, run)
                    run.update(
                        phase="validated", exit_code=0, finished_at=int(time.time())
                    )
                    write_json(directory / "dependency.json", run)
            except Exception as error:
                self.gate.commands.stop()
                if (
                    not self.gate.commands.empty()
                    or self.gate.commands.sandbox_processes()
                ):
                    raise ProjectVolumeError(
                        "Dependency worker could not drain its children"
                    ) from error
                self.update(
                    fence,
                    phase="failed",
                    error=str(error)[:1000],
                    finished_at=int(time.time()),
                )
                raise
