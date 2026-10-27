"""Reserved, offline project file transfer. No caller-supplied host path.

Restore stays pending until every expected byte and the complete tree match.
Partial files are confined to the unstarted project volume, not another cache.
"""

from contextlib import contextmanager
import hashlib
import json
import os
import re
import stat
import time

from src.local_runtime.files import read_json, write_json
from src.project_content import (
    CHUNK_BYTES,
    MAX_FILE_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_VERSION_FILES,
    manifest_bytes,
    manifest_digest,
    project_path,
)
from .project_volumes import ProjectVolumeError


def signature(info):
    return [
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
    ]


class ProjectFiles:
    def __init__(self, volumes):
        self.volumes = volumes

    @staticmethod
    @contextmanager
    def root(target):
        fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def _regular(fd, root, *, size=None):
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_dev != os.fstat(root).st_dev
            or info.st_size > MAX_FILE_BYTES
            or info.st_mode & (stat.S_ISUID | stat.S_ISGID)
            or (size is not None and info.st_size != size)
        ):
            raise ProjectVolumeError("Unsafe, oversized or changed project file")
        return info

    @staticmethod
    @contextmanager
    def parent(root, path, *, create=False):
        if project_path(path) != path or path.split("/")[0] == "lost+found":
            raise ProjectVolumeError("Unsafe or reserved project path")
        fd = os.dup(root)
        try:
            for part in path.split("/")[:-1]:
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=fd)
                        if os.geteuid() == 0:
                            owner = os.fstat(root)
                            os.chown(
                                part,
                                owner.st_uid,
                                owner.st_gid,
                                dir_fd=fd,
                                follow_symlinks=False,
                            )
                        os.fsync(fd)
                    except FileExistsError:
                        pass
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
                os.close(fd)
                fd = child
                if os.fstat(fd).st_dev != os.fstat(root).st_dev:
                    raise ProjectVolumeError(
                        "Project directory crossed a mount boundary"
                    )
            yield fd, path.split("/")[-1]
        finally:
            os.close(fd)

    @staticmethod
    def _hash(fd, size):
        digest, offset = hashlib.sha256(), 0
        while offset < size:
            data = os.pread(fd, min(CHUNK_BYTES, size - offset), offset)
            if not data:
                raise ProjectVolumeError("Project file changed while reading")
            digest.update(data)
            offset += len(data)
        return digest.hexdigest()

    def scan(self, root, quota):
        files, identities, seen = [], [], set()
        nodes, total, deadline = 0, 0, time.monotonic() + 300

        def walk(directory, prefix):
            nonlocal nodes, total
            names = []
            with os.scandir(directory) as entries:
                for entry in entries:
                    nodes += 1
                    if nodes > MAX_VERSION_FILES * 2 or time.monotonic() > deadline:
                        raise ProjectVolumeError("Project traversal limit exceeded")
                    names.append(entry.name)
            for name in sorted(names):
                path = prefix + name
                if project_path(path) != path or path.casefold() in seen:
                    raise ProjectVolumeError(
                        "Noncanonical or conflicting project paths"
                    )
                seen.add(path.casefold())
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(
                        name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=directory,
                    )
                    try:
                        if os.fstat(child).st_dev != os.fstat(root).st_dev:
                            raise ProjectVolumeError(
                                "Nested project mount is not retained data"
                            )
                        if path == "lost+found":
                            with os.scandir(child) as recovery:
                                if next(recovery, None) is not None:
                                    raise ProjectVolumeError(
                                        "Filesystem recovery files need inspection"
                                    )
                        else:
                            walk(child, path + "/")
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode):
                    if len(files) >= MAX_VERSION_FILES:
                        raise ProjectVolumeError(
                            "Project version exceeds its file count"
                        )
                    fd = os.open(
                        name,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=directory,
                    )
                    try:
                        before = self._regular(fd, root)
                        total += before.st_size
                        if total > quota or time.monotonic() > deadline:
                            raise ProjectVolumeError(
                                "Project snapshot capacity or time limit exceeded"
                            )
                        digest = self._hash(fd, before.st_size)
                        after = self._regular(fd, root)
                        if signature(before) != signature(after):
                            raise ProjectVolumeError(
                                "Project file changed during snapshot"
                            )
                        files.append(
                            {
                                "path": path,
                                "sha256": digest,
                                "size": before.st_size,
                                "mode": 0o755 if before.st_mode & 0o111 else 0o644,
                            }
                        )
                        identities.append(signature(after))
                    finally:
                        os.close(fd)
                else:
                    raise ProjectVolumeError(
                        "Symlinks and special project files require explicit handling"
                    )

        walk(root, "")
        ordered = sorted(zip(files, identities), key=lambda pair: pair[0]["path"])
        manifest = {"format": 1, "files": [pair[0] for pair in ordered]}
        manifest_bytes(manifest, quota_bytes=quota)
        return manifest, [pair[1] for pair in ordered]

    @staticmethod
    def _document(directory, record):
        path = directory / "files.json"
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or info.st_mode & 0o077
            or info.st_size > MAX_MANIFEST_BYTES * 2
        ):
            raise ProjectVolumeError("Unsafe project file journal")
        document = read_json(path)
        if (
            document.get("fence") != record["fence"]
            or document.get("generation") != record["generation"]
            or manifest_digest(document["manifest"], quota_bytes=record["quota_bytes"])
            != record["files"]["sha256"]
        ):
            raise ProjectVolumeError(
                "Project manifest journal does not match its fence"
            )
        return document

    @staticmethod
    def _reply(record, **extra):
        return {
            "project_id": record["project_id"],
            "generation": record["generation"],
            "fence": record["fence"],
            "pending": record["pending"],
            "manifest_sha256": record["files"]["sha256"],
            **extra,
        }

    def operate(
        self,
        action,
        owner,
        project_id,
        *,
        generation,
        fence,
        reservation,
        payload=b"",
        index=0,
        offset=0,
        manifest_sha256=None,
        manifest_size=None,
        max_bytes=CHUNK_BYTES,
    ):
        with self.volumes._locked(owner, project_id) as (directory, record):
            self.volumes._generation(record, generation)
            self.volumes._require_reservation(record, reservation)
            if not record or record.get("deleted") or not record.get("maintenance"):
                raise ProjectVolumeError("File transfer requires a reserved project")
            binding = self.volumes._workspace_binding(
                directory, record, maintenance=True
            )
            if not binding["mounted"] or (
                binding["workspace"] and not binding["mount_matches"]
            ):
                raise ProjectVolumeError(
                    "Project mount is not ready for offline transfer"
                )
            with self.root(binding["path"]) as root:
                if action == "snapshot":
                    if self.volumes._begin(
                        directory, record, "files_snapshot", fence, {}
                    ):
                        manifest, identities = self.scan(root, record["quota_bytes"])
                        self.volumes._admit(MAX_MANIFEST_BYTES * 2)
                        digest = manifest_digest(
                            manifest, quota_bytes=record["quota_bytes"]
                        )
                        write_json(
                            directory / "files.json",
                            {
                                "fence": fence,
                                "generation": generation,
                                "manifest": manifest,
                                "identities": identities,
                            },
                        )
                        record["files"] = {"sha256": digest, "fence": fence}
                        self.volumes._finish(directory, record)
                    document = self._document(directory, record)
                    return self._reply(
                        record, file_count=len(document["manifest"]["files"])
                    ), b""
                if action == "restore_begin":
                    if (
                        not isinstance(manifest_sha256, str)
                        or not re.fullmatch(r"[a-f0-9]{64}", manifest_sha256)
                        or type(manifest_size) is not int
                        or not 1 <= manifest_size <= MAX_MANIFEST_BYTES
                    ):
                        raise ProjectVolumeError("Invalid restore manifest identity")
                    return self._restore_begin(
                        directory, record, root, fence, manifest_sha256, manifest_size
                    ), b""
                if (
                    record["fence"] != fence
                    or record.get("files", {}).get("fence") != fence
                ):
                    raise ProjectVolumeError("Project file operation was superseded")
                operation = record["intent"]["operation"]
                if action in {
                    "restore_manifest",
                    "restore_file",
                    "restore_finish",
                    "restore_check",
                }:
                    if (
                        operation != "files_restore"
                        or record["files"]["sha256"] != manifest_sha256
                    ):
                        raise ProjectVolumeError(
                            "No matching project restore is pending"
                        )
                    if (
                        action in {"restore_manifest", "restore_file"}
                        and not record["pending"]
                    ):
                        raise ProjectVolumeError("Finished restore cannot be modified")
                    if action == "restore_check" and record["pending"]:
                        raise ProjectVolumeError("Restore has not finished")
                    if action == "restore_manifest":
                        self._restore_manifest(directory, record, offset, payload)
                    elif action == "restore_file":
                        document = self._document(directory, record)
                        self._restore_file(
                            root, self._item(document, index), offset, payload
                        )
                    else:
                        document = self._document(directory, record)
                        actual, _ = self.scan(root, record["quota_bytes"])
                        if actual != document["manifest"]:
                            raise ProjectVolumeError(
                                "Restored tree is incomplete or changed"
                            )
                        if action == "restore_finish":
                            self.volumes._finish(directory, record)
                            (directory / "manifest.upload").unlink(missing_ok=True)
                    return self._reply(record), b""
                if (
                    operation != "files_snapshot"
                    or record["pending"]
                    or record["files"]["sha256"] != manifest_sha256
                ):
                    raise ProjectVolumeError("No matching completed snapshot exists")
                document = self._document(directory, record)
                if action == "page":
                    files = document["manifest"]["files"]
                    if index > len(files):
                        raise ProjectVolumeError("Snapshot page exceeds its manifest")
                    page = files[index : index + 128]
                    return self._reply(
                        record,
                        files=page,
                        next_index=index + len(page),
                        file_count=len(files),
                    ), b""
                if action == "check":
                    actual, _ = self.scan(root, record["quota_bytes"])
                    if actual != document["manifest"]:
                        raise ProjectVolumeError("Project changed after its snapshot")
                    return self._reply(record), b""
                if action != "read":
                    raise ProjectVolumeError("Unknown project file operation")
                item = self._item(document, index)
                if offset > item["size"]:
                    raise ProjectVolumeError("File offset exceeds its size")
                with self.parent(root, item["path"]) as (parent, name):
                    fd = os.open(
                        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
                    )
                    try:
                        before = self._regular(fd, root, size=item["size"])
                        if signature(before) != document["identities"][index]:
                            raise ProjectVolumeError("Snapshot file identity changed")
                        data = os.pread(
                            fd,
                            min(max_bytes, CHUNK_BYTES, item["size"] - offset),
                            offset,
                        )
                        if signature(self._regular(fd, root)) != signature(before):
                            raise ProjectVolumeError(
                                "Snapshot file changed during transfer"
                            )
                    finally:
                        os.close(fd)
                return self._reply(record, index=index, offset=offset), data

    @staticmethod
    def _item(document, index):
        if not 0 <= index < len(document["manifest"]["files"]):
            raise ProjectVolumeError("Invalid retained-file index")
        return document["manifest"]["files"][index]

    def _restore_begin(self, directory, record, root, fence, digest, size):
        arguments = {"manifest_sha256": digest, "manifest_size": size}
        if fence > record["fence"]:
            if record.get("workspace"):
                raise ProjectVolumeError("Restore requires a new, unstarted workspace")
            manifest, _ = self.scan(root, record["quota_bytes"])
            if manifest["files"]:
                raise ProjectVolumeError(
                    "Restore cannot overwrite existing project files"
                )
            self.volumes._admit(MAX_MANIFEST_BYTES * 2)
        pending = self.volumes._begin(
            directory, record, "files_restore", fence, arguments
        )
        if record.get("files", {}).get("fence") != fence:
            fd = os.open(
                directory / "manifest.upload",
                os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
            )
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077
                ):
                    raise ProjectVolumeError("Unsafe manifest upload")
                os.ftruncate(fd, 0)
                os.fsync(fd)
            finally:
                os.close(fd)
            record["files"] = {"sha256": digest, "size": size, "fence": fence}
            self.volumes._save(directory, record)
        return self._reply(record, complete=not pending)

    @staticmethod
    def _append(fd, offset, data, size):
        current = os.fstat(fd).st_size
        if offset > current or offset + len(data) > size or current > size:
            raise ProjectVolumeError("Out-of-order or oversized project transfer")
        overlap = min(len(data), current - offset)
        if os.pread(fd, overlap, offset) != data[:overlap]:
            raise ProjectVolumeError("Retried project chunk has different bytes")
        written = overlap
        while written < len(data):
            count = os.pwrite(fd, data[written:], offset + written)
            if not count:
                raise ProjectVolumeError("Incomplete project write")
            written += count
        os.fsync(fd)

    def _restore_manifest(self, directory, record, offset, data):
        fd = os.open(
            directory / "manifest.upload", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        )
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.geteuid()
            ):
                raise ProjectVolumeError("Unsafe manifest upload")
            self._append(fd, offset, data, record["files"]["size"])
            if os.fstat(fd).st_size == record["files"]["size"]:
                encoded = os.pread(fd, record["files"]["size"], 0)
                if hashlib.sha256(encoded).hexdigest() != record["files"]["sha256"]:
                    raise ProjectVolumeError("Restore manifest checksum failed")
                manifest = json.loads(encoded)
                if (
                    manifest_bytes(manifest, quota_bytes=record["quota_bytes"])
                    != encoded
                ):
                    raise ProjectVolumeError("Restore manifest is not canonical")
                if any(
                    item["path"].split("/")[0] == "lost+found"
                    for item in manifest["files"]
                ):
                    raise ProjectVolumeError(
                        "Restore manifest uses a filesystem recovery path"
                    )
                write_json(
                    directory / "files.json",
                    {
                        "fence": record["fence"],
                        "generation": record["generation"],
                        "manifest": manifest,
                    },
                )
        finally:
            os.close(fd)

    def _restore_file(self, root, item, offset, data):
        with self.parent(root, item["path"], create=True) as (parent, name):
            fd = os.open(
                name,
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=parent,
            )
            try:
                self._regular(fd, root)
                self._append(fd, offset, data, item["size"])
                if os.geteuid() == 0:
                    owner = os.fstat(root)
                    os.fchown(fd, owner.st_uid, owner.st_gid)
                if os.fstat(fd).st_size == item["size"]:
                    if self._hash(fd, item["size"]) != item["sha256"]:
                        raise ProjectVolumeError(
                            "Restored project file checksum failed"
                        )
                    os.fchmod(fd, item["mode"])
                os.fsync(fd)
                os.fsync(parent)
            finally:
                os.close(fd)
