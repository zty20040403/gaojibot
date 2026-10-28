"""Standalone guest execution gate; only the trusted host may call this as root.

The epoch survives reboot. Closing admission precedes draining command cgroups;
failure leaves admission closed, never a success-shaped snapshot permission.
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import uuid

if __package__:
    from .project_vm_guest import GuestDiskError, ProjectGuestWorkspace, load_config, validate_config
else:
    from project_disk import GuestDiskError, ProjectGuestWorkspace, load_config, validate_config

HELPER_PATH = "/usr/local/lib/gaoji/project_gate.py"
STATE_PATH = Path("/var/lib/gaoji-project-gate.json")
LOCK_ROOT = Path("/run/lock")
SLICE = "gaojiproject.slice"
MAX_REQUEST_BYTES = 1024 * 1024


@contextmanager
def guest_lock(path, *, shared=False, timeout=30):
    import fcntl

    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            raise GuestDiskError("Unsafe guest operation lock")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(stream, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Guest project operation is busy") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _read_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > 8192):
            raise GuestDiskError("Unsafe project execution journal")
        try:
            return json.load(stream)
        except ValueError:
            raise GuestDiskError("Invalid project execution journal") from None


class LinuxGuestCommands:
    cgroup_root = Path("/sys/fs/cgroup")

    def require_cgroups(self):
        if not (self.cgroup_root / "cgroup.controllers").is_file():
            raise GuestDiskError("Project execution requires unified cgroup v2")

    def empty(self, unit=None):
        self.require_cgroups()
        path = self.cgroup_root / SLICE
        if unit is not None:
            path /= unit
        try:
            events = dict(line.split() for line in (path / "cgroup.events").read_text().splitlines())
        except FileNotFoundError:
            return not path.exists()
        except (ValueError, OSError):
            raise GuestDiskError("Cannot verify the project command cgroup") from None
        if events.get("populated") not in {"0", "1"}:
            raise GuestDiskError("Missing cgroup population evidence")
        return events["populated"] == "0"

    def stop(self, unit=SLICE):
        # A collected unit may already be absent. Population is checked separately.
        try:
            subprocess.run(["/usr/bin/systemctl", "stop", unit], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise GuestDiskError("Project command termination did not complete") from None

    def sandbox_processes(self):
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                lines = (entry / "status").read_text().splitlines()
            except FileNotFoundError:
                continue
            except OSError:
                raise GuestDiskError("Cannot verify sandbox process inventory") from None
            values = dict(line.split(":", 1) for line in lines if ":" in line)
            if "Uid" not in values:
                raise GuestDiskError("Process identity is unavailable")
            if "1000" in values["Uid"].split():
                return True
        return False

    @staticmethod
    def command(argv, timeout, tasks, unit):
        # $$ is literal $ in systemd's ExecStart parser, including systemd <254.
        return ["/usr/bin/systemd-run", "--quiet", "--wait", "--pipe", "--collect",
                f"--unit={unit}", f"--slice={SLICE}", "--service-type=exec",
                "--property=KillMode=control-group", "--property=TimeoutStopSec=5s",
                f"--property=RuntimeMaxSec={timeout}s", f"--property=TasksMax={tasks}",
                "--property=NoNewPrivileges=yes", "--property=ProtectControlGroups=yes",
                "--property=Delegate=no", "--", *[arg.replace("$", "$$") for arg in argv]]

    def run(self, argv, timeout, tasks):
        self.require_cgroups()
        unit = "gaoji-project-op-" + uuid.uuid4().hex + ".service"
        try:
            # RuntimeMaxSec belongs to PID 1, not the host QGA polling process.
            # No local timeout may abandon a pending systemd start request.
            result = subprocess.run(self.command(argv, timeout, tasks, unit),
                                    stdin=subprocess.DEVNULL, check=False)
            return result.returncode
        finally:
            self.stop(unit)
            if not self.empty(unit):
                raise GuestDiskError("Project command descendants are still running")


class ProjectGuestGate:
    def __init__(self, config, *, state_path=STATE_PATH, lock_root=LOCK_ROOT, commands=None, workspace=None):
        self.config = validate_config(config)
        if self.config["initialize"]:
            raise GuestDiskError("Project initialization is not sealed")
        self.identity = {key: self.config[key] for key in ("project_id", "generation", "filesystem_uuid", "serial")}
        self.state_path, self.lock_root = Path(state_path), Path(lock_root)
        self.commands = commands or LinuxGuestCommands()
        self.workspace = workspace or ProjectGuestWorkspace(config)

    def _lock(self, name, **kwargs):
        return guest_lock(self.lock_root / f"gaoji-project-{name}.lock", **kwargs)

    def _require_settled_disk(self):
        if __package__:
            from .project_vm_resize_guest import require_settled
        else:
            from project_resize import require_settled
        require_settled(self)

    def _state(self):
        try:
            value = _read_private(self.state_path)
        except FileNotFoundError:
            return {**self.identity, "epoch": 0, "mode": "quiesced"}
        if (not isinstance(value, dict) or set(value) != {*self.identity, "epoch", "mode"}
                or any(type(value[key]) is not type(expected) or value[key] != expected
                       for key, expected in self.identity.items())
                or type(value["epoch"]) is not int or not 1 <= value["epoch"] < 2**53
                or not isinstance(value["mode"], str) or value["mode"] not in {"active", "closing", "quiesced"}):
            raise GuestDiskError("Project execution journal belongs to another epoch or disk")
        return value

    def _save(self, state):
        fd, temporary = tempfile.mkstemp(prefix=".gaoji-gate-", dir=self.state_path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(state, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
            directory = os.open(self.state_path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _epoch(value):
        if type(value) is not int or not 1 <= value < 2**53:
            raise GuestDiskError("Invalid project execution epoch")
        return value

    def _receipt(self, state):
        return {**state, "verified": True}

    @contextmanager
    def observation(self, epoch):
        """Keep admission stable while a trusted read observes the project."""
        self._epoch(epoch)
        with self._lock("control"), self._lock("io", timeout=0):
            self._require_settled_disk()
            state = self._state()
            if state["epoch"] != epoch or state["mode"] != "active":
                raise GuestDiskError("Project admission is not active at the host epoch")
            mounted = self.workspace.verify()
            self.commands.require_cgroups()
            yield {**self._receipt(state), "mounted": True,
                    "used_bytes": mounted["used_bytes"], "quota_bytes": self.config["quota_bytes"],
                    "busy": not self.commands.empty() or self.commands.sandbox_processes()}

    def inspect(self, epoch):
        """Observe admission and the mounted disk without changing either."""
        with self.observation(epoch) as receipt:
            return receipt

    def activate(self, epoch):
        self._epoch(epoch)
        with self._lock("control"), self._lock("io"):
            self._require_settled_disk()
            state = self._state()
            if epoch == state["epoch"] and state["mode"] == "active":
                self.workspace.verify()
                return self._receipt(state)
            if epoch <= state["epoch"] or state["mode"] != "quiesced":
                raise GuestDiskError("Execution admission needs a newer epoch and completed quiescence")
            self.commands.require_cgroups()
            if not self.commands.empty() or self.commands.sandbox_processes():
                raise GuestDiskError("Unaccounted guest writers prevent activation")
            self.workspace.verify()
            state = {**self.identity, "epoch": epoch, "mode": "active"}
            self._save(state)
            return self._receipt(state)

    def quiesce(self, epoch, *, timeout=30):
        self._epoch(epoch)
        with self._lock("control"):
            state = self._state()
            if epoch < state["epoch"] or (epoch == state["epoch"] and state["mode"] == "active"):
                raise GuestDiskError("Stale project quiescence request")
            state = {**self.identity, "epoch": epoch, "mode": "closing"}
            self._save(state)
            deadline = time.monotonic() + timeout
            while True:
                self.commands.stop()
                try:
                    with self._lock("io", timeout=0):
                        # A start may have raced the first stop while holding the IO lock.
                        self.commands.stop()
                        if not self.commands.empty() or self.commands.sandbox_processes():
                            raise GuestDiskError("Guest writers remain; project stays closed")
                        state["mode"] = "quiesced"
                        self._save(state)
                        return self._receipt(state)
                except TimeoutError:
                    if time.monotonic() >= deadline:
                        raise GuestDiskError("Guest operations did not drain; project stays closed") from None
                    time.sleep(0.05)

    def run(self, epoch, argv, timeout, tasks):
        self._epoch(epoch)
        if (not isinstance(argv, list) or not 1 <= len(argv) <= 128
                or any(not isinstance(arg, str) or "\0" in arg for arg in argv)
                or not argv[0].startswith("/") or sum(len(arg.encode()) for arg in argv) > MAX_REQUEST_BYTES
                or type(timeout) is not int or not 1 <= timeout <= 360
                or type(tasks) is not int or not 32 <= tasks <= 4096):
            raise GuestDiskError("Invalid project command contract")
        with self._lock("io"):
            self._require_settled_disk()
            state = self._state()
            if state["mode"] != "active" or state["epoch"] != epoch:
                raise GuestDiskError("Project command belongs to a closed or stale epoch")
            self.workspace.verify()
            return self.commands.run(argv, timeout, tasks)

    @contextmanager
    def maintenance(self, epoch):
        """Serialize trusted file operations without reopening user execution."""
        self._epoch(epoch)
        with self._lock("control"), self._lock("io"):
            self._require_settled_disk()
            state = self._state()
            if state["mode"] != "quiesced" or not 0 < state["epoch"] <= epoch:
                raise GuestDiskError("File maintenance requires completed writer quiescence")
            self.commands.require_cgroups()
            if not self.commands.empty() or self.commands.sandbox_processes():
                raise GuestDiskError("Guest writers prevent file maintenance")
            self.workspace.verify()
            # The file journal owns operation fences inside this reservation.
            # Do not supersede a pending transfer before it validates the call.
            yield
            self.workspace.verify()

    def disk_operation(self, action, epoch):
        """Disk maintenance stays closed, including retries after unmount."""
        self._epoch(epoch)
        if action not in {"trim", "unmount"}:
            raise GuestDiskError("Unsupported project disk maintenance")
        with self._lock("control"), self._lock("io"), self._lock("disk"):
            self._require_settled_disk()
            state = self._state()
            if state["mode"] != "quiesced" or not 0 < state["epoch"] <= epoch:
                raise GuestDiskError("Disk maintenance requires completed writer quiescence")
            self.commands.require_cgroups()
            if not self.commands.empty() or self.commands.sandbox_processes():
                raise GuestDiskError("Guest writers prevent disk maintenance")
            # unmount performs its own absent-mount check; verify() would reject
            # an already successful operation after its acknowledgement was lost.
            receipt = getattr(self.workspace, action)()
            return {**receipt, "epoch": epoch, "mode": "quiesced"}


def main():
    if os.geteuid() != 0:
        raise GuestDiskError("Project execution gate requires the guest administrator")
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise GuestDiskError("Project request is too large")
    request = json.loads(raw)
    if not isinstance(request, dict):
        raise GuestDiskError("Invalid project request")
    gate = ProjectGuestGate(load_config())
    action = request.get("action")
    if action in {"activate", "quiesce", "inspect"} and set(request) == {"action", "epoch"}:
        print(json.dumps(getattr(gate, action)(request["epoch"]), sort_keys=True))
    elif action in {"trim", "unmount"} and set(request) == {"action", "epoch"}:
        print(json.dumps(gate.disk_operation(action, request["epoch"]), sort_keys=True))
    elif action == "resize" and set(request) == {"action", "phase", "epoch", "from_quota_bytes", "quota_bytes"}:
        if __package__:
            from .project_vm_resize_guest import ProjectGuestResize
        else:
            from project_resize import ProjectGuestResize
        print(json.dumps(ProjectGuestResize(gate).operate(request["phase"], request["epoch"],
            request["from_quota_bytes"], request["quota_bytes"]), sort_keys=True))
    elif action == "run" and set(request) == {"action", "epoch", "argv", "timeout", "tasks"}:
        sys.exit(gate.run(request["epoch"], request["argv"], request["timeout"], request["tasks"]))
    else:
        raise GuestDiskError("Unsupported project execution request")


if __name__ == "__main__":
    main()
