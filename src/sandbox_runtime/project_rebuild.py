"""Offline rebuild classification. Project commands never execute on the host."""

import hashlib
import json
import os
import shlex
import stat
import time
import tomllib

from src.local_runtime.files import read_json, write_json
from src.project_content import manifest_bytes, manifest_digest, project_path, rebuild_spec
from src.project_content import BROWSER_CACHE_DIRS as BROWSER
from .project_volume_journal import ProjectVolumeError

CONTROL = ".gaoji/rebuildable"
GENERIC = frozenset({"__pycache__", ".cache", ".npm", ".pnpm-store"})


def verify_dependency_tree(files, root, quota, source):
    """Locked inputs and retained bytes must survive installation unchanged."""
    spec, defaults, warnings = classify(files, root, source["rebuild"]["command"])
    if warnings or spec is None or spec["locks"] != source["rebuild"]["locks"]:
        raise ProjectVolumeError("Dependency rebuild inputs changed or are unsupported")
    roots = {}
    actual, _ = files.scan(root, quota, excluded_identities=roots, select=lambda path: (
        path in spec["excluded"] or path in defaults or path.split("/")[-1] in GENERIC or path in BROWSER
    ))
    actual.update(format=2, rebuild={**spec, "excluded": sorted(roots)})
    manifest_bytes(actual, quota_bytes=quota)
    if actual["files"] != source["files"]:
        raise ProjectVolumeError("Dependency rebuild changed retained project files")
    return source


def read_input(files, root, path, limit):
    try:
        with files.parent(root, path) as (parent, name):
            fd = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
            try:
                info = files._regular(fd, root)
                if info.st_size > limit:
                    raise ProjectVolumeError(
                        "Project rebuild input exceeds its size limit"
                    )
                data = os.pread(fd, limit + 1, 0)
                after = files._regular(fd, root)
                if (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ) or len(data) != info.st_size:
                    raise ProjectVolumeError("Project rebuild input changed")
                return data
            finally:
                os.close(fd)
    except FileNotFoundError:
        return None


def classify(files, root, command):
    """Unknown commands/missing lockfiles retain everything, never guess cleanup."""
    if not command:
        return None, set(), ["No rebuild command; dependency directories are retained"]
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = []
    recipes = {
        ("npm", "ci"): (
            "package.json",
            "package-lock.json",
            {"node_modules"},
            {
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "--offline",
                "--prefer-offline",
            },
        ),
        ("uv", "sync"): (
            "pyproject.toml",
            "uv.lock",
            {".venv"},
            {"--frozen", "--offline", "--no-dev", "--all-extras"},
        ),
        ("cargo", "build"): (
            "Cargo.toml",
            "Cargo.lock",
            {"target"},
            {"--locked", "--offline", "--release"},
        ),
    }
    recipe = recipes.get(tuple(argv[:2]))
    if (
        recipe is None
        or any(value not in recipe[3] for value in argv[2:])
        or (argv[:2] == ["uv", "sync"] and "--frozen" not in argv)
        or (argv[:2] == ["cargo", "build"] and "--locked" not in argv)
    ):
        return (
            None,
            set(),
            [
                "Rebuild command has no supported locked recipe; dependencies are retained"
            ],
        )
    metadata, lock, dependencies, _ = recipe
    inputs = {}
    for name in (metadata, lock):
        data = read_input(files, root, name, 8 * 1024 * 1024)
        if data is None:
            return None, set(), [f"Missing {name}; dependency directories are retained"]
        inputs[name] = data
    try:
        if metadata == "package.json":
            package, locked = json.loads(inputs[metadata]), json.loads(inputs[lock])
            if (
                not isinstance(package, dict)
                or not isinstance(locked, dict)
                or type(locked.get("lockfileVersion")) is not int
                or locked["lockfileVersion"] not in (1, 2, 3)
            ):
                raise ValueError()
        else:
            package, locked = (
                tomllib.loads(inputs[name].decode()) for name in (metadata, lock)
            )
            if (
                not package
                or type(locked.get("version")) is not int
                or locked["version"] <= 0
            ):
                raise ValueError()
    except (ValueError, UnicodeError):
        return (
            None,
            set(),
            [
                "Invalid dependency metadata/lockfile; dependency directories are retained"
            ],
        )
    custom = read_input(files, root, CONTROL, 8192)
    paths = set()
    if custom is not None:
        inputs[CONTROL] = custom
        for line in custom.decode("utf-8").splitlines():
            path = line.strip()
            if not path or path.startswith("#"):
                continue
            if project_path(path) != path or any(ch in path for ch in "*?[]"):
                raise ProjectVolumeError(
                    "Rebuildable entries must be exact relative directory paths"
                )
            paths.add(path)
        if len(paths) > 64:
            raise ProjectVolumeError("Too many custom rebuildable paths")
    spec = {
        "command": command,
        "locks": [
            {"path": name, "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in sorted(inputs.items())
        ],
        "excluded": sorted(paths),
    }
    rebuild_spec(spec)
    return spec, dependencies | GENERIC, []


def prune(
    files, directory, record, root, fence, digest, snapshot_fence, *, check=False
):
    """Resume deletion of exactly the roots frozen by an accepted snapshot."""
    arguments = {"manifest_sha256": digest, "snapshot_fence": snapshot_fence}
    path = directory / "prune.json"
    if check and (
        record["pending"]
        or record["fence"] != fence
        or record["intent"] != {"operation": "files_prune", "arguments": arguments}
    ):
        raise ProjectVolumeError("Cleanup has not completed at this fence")
    if fence > record["fence"]:
        if (
            record["intent"]["operation"] != "files_snapshot"
            or record["pending"]
            or record["fence"] != snapshot_fence
            or record["files"]["sha256"] != digest
        ):
            raise ProjectVolumeError("Cleanup requires the completed source snapshot")
        source = files._document(directory, record)
        files.check_tree(root, record["quota_bytes"], source["manifest"], exclude=True)
        document = {
            "fence": fence,
            "generation": record["generation"],
            "arguments": arguments,
            "manifest": source["manifest"],
            "roots": source.get("excluded_identities", {}),
        }
        write_json(path, document)
    else:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_size > 64 * 1024 * 1024
        ):
            raise ProjectVolumeError("Unsafe project cleanup journal")
        document = read_json(path)
    if (
        document["fence"] != fence
        or document["generation"] != record["generation"]
        or document["arguments"] != arguments
        or manifest_digest(document["manifest"], quota_bytes=record["quota_bytes"])
        != digest
    ):
        raise ProjectVolumeError("Cleanup journal does not match its operation")
    pending = (
        False
        if check
        else files.volumes._begin(directory, record, "files_prune", fence, arguments)
    )
    files.check_tree(root, record["quota_bytes"], document["manifest"], exclude=True)
    spec = document["manifest"].get("rebuild")
    paths = spec["excluded"] if spec else []
    if set(paths) != set(document["roots"]):
        raise ProjectVolumeError(
            "Cleanup roots were not observed in the source snapshot"
        )
    if pending:
        # Check every root before deleting anything, and recheck before each tree.
        for name in paths:
            _root_identity(files, root, name, document["roots"][name])
        budget = [200000, time.monotonic() + 300]
        for name in paths:
            if not _root_identity(files, root, name, document["roots"][name]):
                continue
            with files.parent(root, name) as (parent, leaf):
                _delete_tree(parent, leaf, os.fstat(root).st_dev, budget)
    for name in paths:
        if _root_identity(files, root, name, document["roots"][name]):
            raise ProjectVolumeError("Rebuildable directory is still present")
    files.check_tree(root, record["quota_bytes"], document["manifest"], exclude=True)
    if not check:
        files.volumes._finish(directory, record)
    return {
        "project_id": record["project_id"],
        "generation": record["generation"],
        "fence": fence,
        "pending": False,
        "manifest_sha256": digest,
        "pruned_roots": len(paths),
    }


def _root_identity(files, root, path, identity):
    try:
        with files.parent(root, path) as (parent, name):
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(info.st_mode) or [info.st_dev, info.st_ino] != identity:
        raise ProjectVolumeError(
            "Cleanup target was replaced; no new files will be deleted"
        )
    return True


def _delete_tree(parent, name, device, budget, depth=0):
    budget[0] -= 1
    if budget[0] < 0 or time.monotonic() > budget[1] or depth > 128:
        raise ProjectVolumeError(
            "Cleanup traversal limit reached; retry the existing operation"
        )
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if info.st_dev != device:
        raise ProjectVolumeError("Cleanup crossed a filesystem boundary")
    if stat.S_ISDIR(info.st_mode):
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != (info.st_dev, info.st_ino):
                raise ProjectVolumeError("Cleanup directory changed")
            with os.scandir(fd) as entries:
                for entry in entries:
                    _delete_tree(fd, entry.name, device, budget, depth + 1)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rmdir(name, dir_fd=parent)
    elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        os.unlink(name, dir_fd=parent)
    else:
        raise ProjectVolumeError("Special files need inspection before cleanup")
    os.fsync(parent)
