"""Private content-addressed project objects, never a user workspace directory."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
from typing import BinaryIO
import unicodedata

MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_VERSION_FILES = 20000
GC_GRACE_SECONDS = 24 * 3600


def project_path(value: str) -> str:
    value = unicodedata.normalize("NFC", value)
    parts = value.split("/")
    if (
        not value
        or len(value.encode()) > 1024
        or len(parts) > 64
        or "\\" in value
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
        or any(
            part in {"", ".", ".."}
            or ":" in part
            or len(part.encode()) > 255
            or part.endswith((" ", "."))
            or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)
            for part in parts
        )
    ):
        raise ValueError("Invalid project-relative path")
    return str(PurePosixPath(value))


@dataclass(frozen=True)
class ProjectFile:
    path: str
    content: bytes | BinaryIO
    mode: int = 0o644


@dataclass(frozen=True)
class Blob:
    sha256: str
    byte_size: int
    storage_path: str


class ProjectBlobStore:
    """Call mutations under ProjectStore's database blob lock, including GC.

    Files published before a DB rollback are harmless unreferenced objects.
    The collector removes those only after the same 24-hour grace period.
    """

    def __init__(self, root: Path):
        self.root = root.absolute()
        self._directory(self.root)
        self._directory(self.root / "blobs")
        self._directory(self.root / "staging")

    @staticmethod
    def _directory(path):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not stat.S_ISDIR(path.lstat().st_mode):
            raise ValueError("Project storage directory must not be a symlink")
        # Persist new directory entries before a DB version can reference them.
        ProjectBlobStore._sync_directory(path.parent)

    @staticmethod
    def _sync_directory(path):
        directory = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def path(self, digest):
        if not re.fullmatch("[0-9a-f]{64}", digest):
            raise ValueError("Invalid blob digest")
        shard = self.root / "blobs" / digest[:2]
        if shard.exists() and not stat.S_ISDIR(shard.lstat().st_mode):
            raise ValueError("Unsafe blob shard")
        return shard / digest

    def put(self, content: bytes | BinaryIO) -> Blob:
        source = io.BytesIO(content) if isinstance(content, bytes) else content
        fd, temporary = tempfile.mkstemp(dir=self.root / "staging", prefix="upload-")
        try:
            digest, size = hashlib.sha256(), 0
            with os.fdopen(fd, "wb") as target:
                while block := source.read(1024 * 1024):
                    size += len(block)
                    if size > MAX_FILE_BYTES:
                        raise ValueError("Project file exceeds 256 MiB")
                    digest.update(block)
                    target.write(block)
                target.flush()
                os.fsync(target.fileno())
            sha = digest.hexdigest()
            path = self.path(sha)
            self._directory(path.parent)
            try:
                os.link(temporary, path)
                path.chmod(0o400)
                self._sync_directory(path.parent)
            except FileExistsError:
                self.verify(sha, size)
            return Blob(sha, size, str(path.relative_to(self.root)))
        finally:
            Path(temporary).unlink(missing_ok=True)

    def read(self, sha: str) -> bytes:
        fd = os.open(self.path(sha), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                raise ValueError("Invalid stored project object")
            data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES or hashlib.sha256(data).hexdigest() != sha:
            raise ValueError("Project object integrity check failed")
        return data

    def verify(self, sha: str, expected_size: int):
        if len(self.read(sha)) != expected_size:
            raise ValueError("Stored project object size changed")

    def remove(self, sha: str):
        path = self.path(sha)
        if path.is_symlink():
            raise ValueError("Refusing symlink object deletion")
        path.unlink(missing_ok=True)

    def collect_staging(self, cutoff: int, *, limit: int):
        """Caller holds the same blob lock as writers; recover interrupted uploads."""
        removed = 0
        for path in (self.root / "staging").iterdir():
            if removed >= limit:
                break
            info = path.lstat()
            if (
                re.fullmatch(r"upload-[a-z0-9_]{8}", path.name)
                and stat.S_ISREG(info.st_mode)
                and info.st_mtime <= cutoff
            ):
                path.unlink()
                removed += 1

    def orphan_candidates(self, cutoff: int):
        for shard in (self.root / "blobs").iterdir():
            if not re.fullmatch("[0-9a-f]{2}", shard.name) or not stat.S_ISDIR(
                shard.lstat().st_mode
            ):
                continue
            for path in shard.iterdir():
                info = path.lstat()
                if (
                    stat.S_ISREG(info.st_mode)
                    and re.fullmatch("[0-9a-f]{64}", path.name)
                    and path.name.startswith(shard.name)
                    and info.st_mtime <= cutoff
                ):
                    yield path.name
