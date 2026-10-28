"""Root-only KVM browser lifecycle; the browser itself runs as sandbox UID 1000.

QGA carries only start/status/stop. Browser frames use a private virtio port,
never a guest TCP listener. A lost start acknowledgement cannot start again.
"""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

if __package__:
    from .project_vm_gate import ProjectGuestGate, _read_private, guest_lock, SLICE
    from .project_vm_guest import GuestDiskError, load_config
else:
    from project_gate import ProjectGuestGate, _read_private, guest_lock, SLICE
    from project_disk import GuestDiskError, load_config

HELPER_PATH = "/usr/local/lib/gaoji/project_browser_control.pyz"
WORKER_PATH = "/usr/local/lib/gaoji/project_browser.pyz"
REQUIREMENTS_PATH = "/usr/local/lib/gaoji/project_browser_requirements.txt"
PYTHON_PATH = "/opt/gaoji-project-browser/bin/python"
CHROMIUM_PATH = "/usr/bin/chromium"
CHANNEL_NAME = "org.gaoji.project-browser.0"
SOCKET_NAME = "browser.sock"
STATE_ROOT = Path("/var/lib/gaoji-project-browser")
SESSION = re.compile(r"[a-f0-9]{32}")


def _session(value):
    if not isinstance(value, str) or SESSION.fullmatch(value) is None:
        raise GuestDiskError("Invalid guest browser session")
    return value


def _unit(session):
    return "gaoji-project-browser-" + _session(session) + ".service"


def _description(session):
    return "Gaoji project browser " + _session(session)


def _trusted_file(path, *, executable=False):
    original = Path(path)
    for entry in (original, *original.parents):
        info = entry.lstat()
        if info.st_uid != os.geteuid() or (not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022):
            raise GuestDiskError("Browser toolchain path must be administrator-owned and non-writable")
    path = original.resolve(strict=True)
    for entry in (path, *path.parents):
        info = entry.stat()
        if (info.st_uid != os.geteuid() or info.st_mode & 0o022
                or not (stat.S_ISREG(info.st_mode) if entry == path else stat.S_ISDIR(info.st_mode))):
            raise GuestDiskError("Browser toolchain must be administrator-owned and non-writable")
    if executable and not os.access(path, os.X_OK):
        raise GuestDiskError("Guest browser executable is unavailable")
    return path


def _write_private(path, value):
    fd, name = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class LinuxGuestBrowser:
    def command(self, session, tasks):
        return ["/usr/bin/systemd-run", "--quiet", "--collect", "--service-type=exec",
                f"--unit={_unit(session)}", f"--description={_description(session)}", f"--slice={SLICE}",
                "--property=KillMode=control-group", "--property=Restart=no",
                "--property=TimeoutStopSec=5s", "--property=RuntimeMaxSec=3600s",
                f"--property=TasksMax={tasks}", "--property=MemoryMax=1073741824",
                "--property=CPUQuota=100%", "--property=NoNewPrivileges=yes",
                "--property=CapabilityBoundingSet=CAP_SETUID CAP_SETGID",
                "--property=ProtectControlGroups=yes", "--property=Delegate=no",
                "--property=ProtectSystem=strict", "--property=PrivateTmp=yes", "--property=UMask=0077",
                f"--property=ReadWritePaths=/workspace {STATE_ROOT} /run/lock",
                "--property=StandardInput=null", "--property=StandardOutput=null",
                "--property=StandardError=null", "--",
                "/usr/bin/python3", "-I", HELPER_PATH, "--worker", session]

    def inspect(self, session):
        try:
            result = subprocess.run(["/usr/bin/systemctl", "show", _unit(session), "--no-pager",
                "--property=LoadState,ActiveState,SubState,Transient,Description,Slice,Restart,KillMode"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=10, check=False)
            fields = dict(line.split("=", 1) for line in result.stdout.decode().splitlines())
        except (ValueError, OSError, subprocess.TimeoutExpired):
            raise GuestDiskError("Guest browser unit observation failed") from None
        if fields.get("LoadState") == "not-found" and fields.get("ActiveState") == "inactive":
            return {"present": False, "active": False}
        if (result.returncode or fields.get("LoadState") != "loaded" or fields.get("Transient") != "yes"
                or fields.get("Description") != _description(session) or fields.get("Slice") != SLICE
                or fields.get("Restart") != "no" or fields.get("KillMode") != "control-group"
                or fields.get("ActiveState") not in {"active", "inactive", "activating", "deactivating", "failed"}):
            raise GuestDiskError("Guest browser unit ownership or policy changed")
        return {"present": True, "active": fields["ActiveState"] in {"active", "activating", "deactivating"}}

    def prepare(self):
        # Dependencies are provided by the trusted, pinned toolchain installer,
        # not by model arguments or an unverified file in the project workspace.
        for path in ("/usr/bin/python3", PYTHON_PATH, CHROMIUM_PATH):
            _trusted_file(path, executable=True)
        for path in (HELPER_PATH, WORKER_PATH):
            _trusted_file(path)
        _trusted_file(Path(PYTHON_PATH).parents[1] / "pyvenv.cfg")
        self.check_dependencies()

    def check_dependencies(self):
        root = Path(PYTHON_PATH).parents[1].resolve(strict=True)
        count = 0
        def failed(error):
            raise GuestDiskError("Guest browser toolchain inventory is unreadable") from error
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=failed):
            for name in (*dirs, *files):
                entry = Path(directory) / name
                info = entry.lstat()
                count += 1
                if count > 50000 or info.st_uid != os.geteuid():
                    raise GuestDiskError("Guest browser toolchain inventory is not trusted")
                if stat.S_ISLNK(info.st_mode):
                    if entry.is_dir():
                        if entry != root / "lib64" or entry.resolve() != root / "lib":
                            raise GuestDiskError("Unexpected guest browser library directory link")
                    else:
                        _trusted_file(entry)
                elif info.st_mode & 0o022 or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                    raise GuestDiskError("Guest browser dependencies must not be user-writable")

    def start(self, session, tasks):
        # Do not abandon a queued PID 1 job with a subprocess timeout. Runtime
        # limits are enforced by systemd independently of the QGA caller.
        result = subprocess.run(self.command(session, tasks), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if result.returncode:
            raise GuestDiskError("Guest browser launch is unconfirmed")

    def worker(self):
        path = Path("/dev/virtio-ports") / CHANNEL_NAME
        target = path.resolve(strict=True)
        info = target.stat()
        if (target.parent != Path("/dev") or not stat.S_ISCHR(info.st_mode)
                or info.st_uid != 0 or info.st_mode & 0o077
                or (Path("/sys/class/virtio-ports") / target.name / "name").read_text().strip() != CHANNEL_NAME):
            raise GuestDiskError("Guest browser needs its private root-owned virtio port")
        descriptor = os.open(target, os.O_RDWR | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "r+b", buffering=0) as channel:
            opened = os.fstat(channel.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_rdev) != (info.st_dev, info.st_ino, info.st_rdev):
                raise GuestDiskError("Guest browser device changed during open")
            result = subprocess.run([PYTHON_PATH, "-I", WORKER_PATH],
                stdin=channel, stdout=channel, stderr=subprocess.DEVNULL,
                cwd="/workspace", user=1000, group=1000, extra_groups=(),
                env={"HOME": "/tmp", "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, check=False)
            return result.returncode


class ProjectGuestBrowser:
    def __init__(self, gate, *, root=STATE_ROOT, commands=None):
        self.gate, self.root = gate, Path(root)
        self.commands = commands or LinuxGuestBrowser()

    def _directory(self):
        self.root.mkdir(mode=0o700, exist_ok=True)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise GuestDiskError("Unsafe guest browser journal directory")

    def _lock(self):
        self._directory()
        return guest_lock(self.root / "control.lock")

    def _state(self):
        try:
            state = _read_private(self.root / "state.json")
        except FileNotFoundError:
            return None
        expected = {*self.gate.identity, "epoch", "session_id", "tasks", "state"}
        if (not isinstance(state, dict) or set(state) != expected
                or any(type(state[k]) is not type(v) or state[k] != v for k, v in self.gate.identity.items())
                or type(state["tasks"]) is not int or not 32 <= state["tasks"] <= 4096
                or not isinstance(state["state"], str)
                or state["state"] not in {"starting", "running", "closing", "closed"}):
            raise GuestDiskError("Guest browser journal belongs to another disk or contract")
        self.gate._epoch(state["epoch"])
        _session(state["session_id"])
        return state

    def _save(self, state):
        _write_private(self.root / "state.json", state)

    def _consume(self, epoch, session, tasks, *, cancel=False):
        path = self.root / ("session-" + _session(session) + ".json")
        intent = {**self.gate.identity, "epoch": epoch, "tasks": tasks, "session_id": session}
        try:
            old = _read_private(path)
        except FileNotFoundError:
            _write_private(path, intent)
        else:
            if (not cancel or not isinstance(old, dict) or old != intent
                    or any(type(old.get(k)) is not type(v) for k, v in intent.items())):
                raise GuestDiskError("Guest browser launch was already consumed")

    def _matching(self, epoch, session):
        self.gate._epoch(epoch)
        _session(session)
        state = self._state()
        if state is None or state["epoch"] != epoch or state["session_id"] != session:
            raise GuestDiskError("Guest browser belongs to another session or epoch")
        return state

    def _observe(self, state):
        session = state["session_id"]
        unit = self.commands.inspect(session)
        empty = self.gate.commands.empty(_unit(session))
        gate = self.gate._state()
        admitted = gate["epoch"] == state["epoch"] and gate["mode"] == "active"
        if admitted:
            self.gate.workspace.verify()
        return {**state, "unit": _unit(session), "unit_present": unit["present"],
                "unit_active": unit["active"], "cgroup_empty": empty,
                "admission_active": admitted, "verified": True}

    def provision(self, epoch):
        self.gate._epoch(epoch)
        with self.gate._lock("control"), self._lock(), self.gate._lock("io", timeout=0):
            gate, state = self.gate._state(), self._state()
            if gate["epoch"] != epoch or gate["mode"] != "active" or (state and state["state"] != "closed"):
                raise GuestDiskError("Browser preparation requires an idle active project epoch")
            self.gate.workspace.verify()
            if not self.gate.commands.empty() or self.gate.commands.sandbox_processes():
                raise GuestDiskError("Other guest writers prevent browser preparation")
            code = self.gate.commands.run(
                ["/usr/bin/python3", "-I", HELPER_PATH, "--install-worker"], 360, 128)
            if code:
                raise GuestDiskError("Guest browser toolchain preparation failed")
            self.commands.prepare()
            return {**self.gate.identity, "epoch": epoch, "toolchain_ready": True, "verified": True}

    def start(self, epoch, session, tasks):
        self.gate._epoch(epoch)
        _session(session)
        if type(tasks) is not int or not 32 <= tasks <= 4096:
            raise GuestDiskError("Invalid guest browser process quota")
        with self.gate._lock("control"), self._lock():
            state = self._state()
            if state and state["session_id"] == session:
                self._matching(epoch, session)
                if state["tasks"] != tasks:
                    raise GuestDiskError("Guest browser retry changed its quota")
                return self._observe(state)
            if state and state["state"] != "closed":
                raise GuestDiskError("Reconcile the previous guest browser before another launch")
            if state:
                observed = self._observe(state)
                if observed["unit_active"] or not observed["cgroup_empty"]:
                    raise GuestDiskError("Closed guest browser still has live processes")
            with self.gate._lock("io", timeout=0):
                gate = self.gate._state()
                if gate["epoch"] != epoch or gate["mode"] != "active":
                    raise GuestDiskError("Guest browser requires the current active epoch")
                self.gate.workspace.verify()
                if not self.gate.commands.empty() or self.gate.commands.sandbox_processes():
                    raise GuestDiskError("Other guest writers prevent browser launch")
                if self.commands.inspect(session)["present"]:
                    raise GuestDiskError("Refusing to adopt a preexisting guest browser unit")
                self.commands.prepare()
                state = {**self.gate.identity, "epoch": epoch, "session_id": session,
                         "tasks": tasks, "state": "starting"}
                self._consume(epoch, session, tasks)
                self._save(state)
                self.commands.start(session, tasks)
                # The worker takes the same locks after systemd has exec'd its
                # trusted wrapper. Only the pipe handshake establishes readiness.
                return self._observe(state)

    def status(self, epoch, session):
        with self.gate._lock("control"), self._lock():
            return self._observe(self._matching(epoch, session))

    def stop(self, epoch, session, tasks=None):
        self.gate._epoch(epoch)
        _session(session)
        if tasks is not None and (type(tasks) is not int or not 32 <= tasks <= 4096):
            raise GuestDiskError("Invalid guest browser process quota")
        with self.gate._lock("control"), self._lock():
            state = self._state()
            if (state is None or state["session_id"] != session) and tasks is not None:
                if state:
                    observed = self._observe(state)
                    if state["state"] != "closed" or observed["unit_active"] or not observed["cgroup_empty"]:
                        raise GuestDiskError("Another guest browser still owns this workspace")
                if self.commands.inspect(session)["present"] or not self.gate.commands.empty(_unit(session)):
                    raise GuestDiskError("Refusing to adopt an unjournalled guest browser unit")
                # A stop before the delayed start must consume that start, not
                # merely observe absence while QGA still has a queued request.
                state = {**self.gate.identity, "epoch": epoch, "session_id": session,
                         "tasks": tasks, "state": "closing"}
                self._consume(epoch, session, tasks, cancel=True)
                self._save(state)
            else:
                state = self._matching(epoch, session)
                if tasks is not None and state["tasks"] != tasks:
                    raise GuestDiskError("Guest browser stop changed its quota")
            self.commands.inspect(session)
            state["state"] = "closing"
            self._save(state)
            self.gate.commands.stop(_unit(session))
            observed = self._observe(state)
            if observed["unit_active"] or not observed["cgroup_empty"]:
                raise GuestDiskError("Guest browser termination has not been verified")
            state["state"] = "closed"
            self._save(state)
            return self._observe(state)

    def worker(self, session):
        _session(session)
        with ExitStack() as lifetime:
            with self.gate._lock("control"), self._lock():
                state = self._state()
                if state is None or state["session_id"] != session or state["state"] != "starting":
                    raise GuestDiskError("Browser worker launch is stale or already consumed")
                lifetime.enter_context(self.gate._lock("io", timeout=0))
                gate = self.gate._state()
                if gate["epoch"] != state["epoch"] or gate["mode"] != "active":
                    raise GuestDiskError("Browser worker launch was revoked")
                self.gate.workspace.verify()
                if self.gate.commands.sandbox_processes():
                    raise GuestDiskError("Unaccounted user processes prevent browser launch")
                self.commands.prepare()
                state["state"] = "running"
                self._save(state)
            # The trusted wrapper, not the unprivileged browser, owns this lock.
            # PID 1 terminates the whole unit if its host or wrapper disappears.
            return self.commands.worker()


def main():
    if os.geteuid() != 0:
        raise GuestDiskError("Guest browser lifecycle requires the administrator")
    browser = ProjectGuestBrowser(ProjectGuestGate(load_config()))
    if sys.argv[1:] == ["--install-worker"]:
        from src.sandbox_runtime.project_vm_browser_install import install
        install()
        return
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        sys.exit(browser.worker(sys.argv[2]))
    if len(sys.argv) != 1:
        raise GuestDiskError("Unsupported guest browser entrypoint")
    raw = sys.stdin.buffer.read(8193)
    if len(raw) > 8192:
        raise GuestDiskError("Guest browser request exceeds its limit")
    request = json.loads(raw)
    if not isinstance(request, dict):
        raise GuestDiskError("Invalid guest browser request")
    action = request.get("action")
    if action == "prepare" and set(request) == {"action", "epoch"}:
        result = browser.provision(request["epoch"])
    elif action == "start" and set(request) == {"action", "epoch", "session_id", "tasks"}:
        result = browser.start(request["epoch"], request["session_id"], request["tasks"])
    elif action == "stop" and set(request) == {"action", "epoch", "session_id", "tasks"}:
        result = browser.stop(request["epoch"], request["session_id"], request["tasks"])
    elif action in {"status", "stop"} and set(request) == {"action", "epoch", "session_id"}:
        result = getattr(browser, action)(request["epoch"], request["session_id"])
    else:
        raise GuestDiskError("Unsupported guest browser action")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
