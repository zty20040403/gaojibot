"""Lazy browser toolchain inside a disposable Debian project system disk.

Called only by the root guest gate inside its bounded systemd cgroup. Python
wheels are hash-locked at VM creation; the apt candidate is frozen before use.
No model command, environment or project file participates in installation.
"""

from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

from .project_vm_browser_guest import (
    CHROMIUM_PATH, PYTHON_PATH, REQUIREMENTS_PATH, STATE_ROOT, LinuxGuestBrowser, _trusted_file,
    _write_private as _save,
)
from .project_vm_gate import _read_private
from .project_vm_guest import GuestDiskError


def _run(argv, *, output=False):
    result = subprocess.run(argv, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if output else subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "HOME": "/root", "LANG": "C.UTF-8",
             "DEBIAN_FRONTEND": "noninteractive"},
        timeout=300, check=False)
    if result.returncode or (output and len(result.stdout) > 128 * 1024):
        raise GuestDiskError("Guest browser dependency command failed: " + Path(argv[0]).name)
    return result.stdout.decode() if output else None


def _requirements():
    path = _trusted_file(REQUIREMENTS_PATH)
    raw = path.read_bytes()
    if not 0 < len(raw) <= 128 * 1024:
        raise GuestDiskError("Guest browser requirements are invalid")
    versions = {}
    for line in raw.decode().splitlines():
        if re.fullmatch(r"[a-z0-9-]+==[0-9]+(?:\.[0-9]+)+(?: --hash=sha256:[a-f0-9]{64})+", line) is None:
            raise GuestDiskError("Guest browser requirements must use exact versions and hashes")
        name, version = line.split(" ", 1)[0].split("==")
        if name in versions:
            raise GuestDiskError("Duplicate guest browser requirement")
        versions[name] = version
    if "playwright" not in versions or len(versions) > 16:
        raise GuestDiskError("Guest browser dependency closure is invalid")
    return hashlib.sha256(raw).hexdigest(), versions


def _verify(plan):
    LinuxGuestBrowser().prepare()
    observed = json.loads(_run([PYTHON_PATH, "-I", "-c",
        "import importlib.metadata as m,json; from playwright.sync_api import sync_playwright; "
        "print(json.dumps({p.metadata['Name'].lower().replace('_','-'):p.version for p in m.distributions()}))"], output=True))
    if any(observed.get(name) != version for name, version in plan["versions"].items()):
        raise GuestDiskError("Guest browser Python dependencies differ from their lock")
    if _run(["/usr/bin/dpkg-query", "-W", "-f=${Version}", "chromium"], output=True).strip() != plan["chromium"]:
        raise GuestDiskError("Guest browser Chromium package differs from its recorded version")
    _run([CHROMIUM_PATH, "--version"], output=True)


def _require_guest():
    release = platform.freedesktop_os_release()
    if (os.geteuid() != 0 or release.get("ID") != "debian"
            or release.get("VERSION_ID") != "13" or sys.version_info < (3, 12)):
        raise GuestDiskError("Project browser preparation requires the pinned Debian 13 guest")


def install():
    _require_guest()
    digest, versions = _requirements()
    path = STATE_ROOT / "toolchain.json"
    try:
        plan = _read_private(path)
    except FileNotFoundError:
        plan = None
    if plan is not None and (not isinstance(plan, dict)
            or set(plan) != {"requirements_sha256", "versions", "chromium", "state"}
            or plan["requirements_sha256"] != digest or plan["versions"] != versions
            or not isinstance(plan["state"], str) or plan["state"] not in {"prepared", "ready"}
            or not isinstance(plan["chromium"], str)
            or re.fullmatch(r"[0-9][a-zA-Z0-9.+:~\-]*", plan["chromium"]) is None):
        raise GuestDiskError("Guest browser toolchain journal does not match this bootstrap")
    if plan and plan["state"] == "ready":
        _verify(plan)
        return
    if shutil.disk_usage("/opt").free < 1024**3:
        raise GuestDiskError("Guest system disk needs at least 1 GiB free for browser preparation")
    if plan is None:
        _run(["/usr/bin/apt-get", "update", "-qq"])
        candidate = Parser().parsestr(_run(["/usr/bin/apt-cache", "show", "--no-all-versions", "chromium"], output=True))
        version = candidate.get("Version", "")
        if re.fullmatch(r"[0-9][a-zA-Z0-9.+:~\-]*", version) is None:
            raise GuestDiskError("No signed-repository Chromium candidate was found")
        plan = {"requirements_sha256": digest, "versions": versions, "chromium": version, "state": "prepared"}
        _save(path, plan)
    # These package-manager operations are idempotent system-disk preparation,
    # not browser effects. Interrupted dpkg configuration is reconciled first.
    _run(["/usr/bin/dpkg", "--configure", "-a"])
    _run(["/usr/bin/apt-get", "install", "-y", "-qq", "--no-remove", "--no-install-recommends",
          "python3-venv", "chromium=" + plan["chromium"]])
    root = Path(PYTHON_PATH).parents[1]
    root.mkdir(mode=0o755, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != os.geteuid() or root.stat().st_mode & 0o022:
        raise GuestDiskError("Unsafe guest browser environment directory")
    LinuxGuestBrowser().check_dependencies()
    _run(["/usr/bin/python3", "-I", "-m", "venv", str(root)])
    _trusted_file(PYTHON_PATH, executable=True)
    _run([PYTHON_PATH, "-I", "-m", "pip", "--isolated", "install", "--disable-pip-version-check",
          "--no-cache-dir", "--only-binary=:all:", "--require-hashes", "--no-deps",
          "--index-url", "https://pypi.org/simple", "-r", REQUIREMENTS_PATH])
    _verify(plan)
    _save(path, {**plan, "state": "ready"})
