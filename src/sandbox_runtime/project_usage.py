"""Read-only, identity-bound project accounting. Never accepts a host path."""

import os
import stat
import time

from .project_files import ProjectFiles
from .project_rebuild import BROWSER, GENERIC, classify
from .project_volume_journal import ProjectVolumeError


def rebuildable_bytes(root, command, *, deadline, max_nodes=1000000):
    files = ProjectFiles(None)
    spec, selected, _ = classify(files, root, command)
    if spec is None:
        return 0
    nodes, inodes = 0, {}
    device = os.fstat(root).st_dev

    def walk(fd, prefix="", inherited=False, depth=0):
        nonlocal nodes
        if depth > 64:
            raise ProjectVolumeError("Project usage traversal depth exceeded")
        before = os.fstat(fd)
        with os.scandir(fd) as entries:
            for entry in entries:
                nodes += 1
                if nodes > max_nodes or time.monotonic() > deadline:
                    raise ProjectVolumeError("Project usage scan exceeded its budget")
                path = prefix + entry.name
                info = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                if info.st_dev != device:
                    raise ProjectVolumeError("Project usage crossed a mount boundary")
                disposable = inherited or (
                    stat.S_ISDIR(info.st_mode)
                    and ".git" not in path.split("/")
                    and not path.startswith("lost+found/")
                    and (not path.startswith(".gaoji/") or path in BROWSER)
                    and (
                        path in spec["excluded"]
                        or path in BROWSER
                        or entry.name in GENERIC
                        or (not prefix and entry.name in selected)
                    )
                )
                identity = (info.st_dev, info.st_ino)
                size = info.st_blocks * 512
                old = inodes.get(identity)
                # A hard link into retained data makes the shared inode retained.
                inodes[identity] = (
                    size,
                    disposable and (old is None or old[1]),
                    info.st_nlink,
                    (old[3] + 1 if old else 1),
                )
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(
                        entry.name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=fd,
                    )
                    try:
                        opened = os.fstat(child)
                        if (opened.st_dev, opened.st_ino) != identity:
                            raise ProjectVolumeError(
                                "Project directory changed during accounting"
                            )
                        walk(child, path + "/", disposable, depth + 1)
                    finally:
                        os.close(child)
                    # Directory link counts are not file hard-link counts.
                    size, eligible, _, _ = inodes[identity]
                    inodes[identity] = (size, eligible, 1, 1)
        after = os.fstat(fd)
        if (before.st_ino, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_ino,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ProjectVolumeError("Project changed during accounting")

    walk(root)
    repeated, _, _ = classify(files, root, command)
    if repeated != spec:
        raise ProjectVolumeError("Rebuild inputs changed during accounting")
    return sum(
        size
        for size, disposable, links, observed in inodes.values()
        if disposable and links <= observed
    )


class ProjectUsage:
    def __init__(self, volumes):
        self.volumes = volumes

    def measure(
        self, owner, project_id, *, generation, rebuild_command="", reservation=None
    ):
        if self.volumes.storage_kind == "desktop-directory":
            return self.volumes.measure(owner, project_id, generation=generation,
                                        rebuild_command=rebuild_command, reservation=reservation)
        with self.volumes._locked(owner, project_id) as (directory, record):
            if record is None:
                raise ProjectVolumeError("Project volume does not exist")
            self.volumes._generation(record, generation)
            self.volumes._require_reservation(record, reservation)
            identity = {
                "project_id": project_id,
                "generation": generation,
                "fence": record["fence"],
                "quota_bytes": record["quota_bytes"],
                "quota_mode": "hard",
            }
            if record.get("deleted"):
                return {
                    **identity,
                    "used_bytes": 0,
                    "rebuildable_bytes": 0,
                    "allocated_bytes": 0,
                    "sampled_at": int(time.time()),
                    "complete": True,
                    "mounted": False,
                }
            if record["pending"] and reservation is None:
                raise ProjectVolumeError("Project measurement waits for maintenance")
            image, target, _, _, mount = self.volumes._observed(directory, record)
            rebuildable = None
            warning = ""
            if mount:
                with ProjectFiles.root(target) as root:
                    root_id = (os.fstat(root).st_dev, os.fstat(root).st_ino)
                    try:
                        rebuildable = rebuildable_bytes(
                            root, rebuild_command, deadline=time.monotonic() + 5
                        )
                    except (OSError, ValueError, ProjectVolumeError, RecursionError):
                        warning = "Rebuildable usage is unknown; no cleanup is authorized by this estimate"
                    used = self.volumes.host.used_bytes(
                        image, root, record["quota_bytes"]
                    )
                    if root_id != (target.stat().st_dev, target.stat().st_ino):
                        raise ProjectVolumeError(
                            "Project mount changed during accounting"
                        )
            else:
                used = self.volumes.host.used_bytes(image, None, record["quota_bytes"])
                warning = "Unmounted workspace; rebuildable usage is not scanned"
            _, _, info, _, verified_mount = self.volumes._observed(directory, record)
            if (
                mount != verified_mount
                or type(used) is not int
                or not 0 <= used <= record["quota_bytes"]
            ):
                raise ProjectVolumeError(
                    "Project accounting identity or capacity changed"
                )
            return {
                **identity,
                "used_bytes": used,
                "rebuildable_bytes": min(rebuildable, used)
                if rebuildable is not None
                else None,
                "allocated_bytes": info.st_blocks * 512,
                "sampled_at": int(time.time()),
                "complete": True,
                "mounted": bool(mount),
                "warning": warning,
            }
