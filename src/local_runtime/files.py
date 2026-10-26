from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import tempfile
import time
from typing import Iterator


def status_directory(root: Path, area: str) -> Path:
    """Publish only fixed, redacted status areas despite a private worker umask."""
    if area not in {"updates", "lifecycles"}:
        raise ValueError("Unknown public status area")
    for path in (root / "status", root / "status" / area):
        path.mkdir(mode=0o755, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ValueError("Public status directory ownership is invalid")
        path.chmod(0o755)
    return root / "status" / area


def read_json(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError("Refusing a symlinked instance file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Instance file must contain a JSON object")
    return value


def write_json(path: Path, value: dict, *, mode: int = 0o600) -> None:
    if mode not in {0o600, 0o644}:
        raise ValueError("Unsupported instance file permissions")
    if path.is_symlink():
        raise ValueError("Refusing a symlinked instance file")
    descriptor, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            os.chmod(temporary, mode)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def file_lock(path: Path, timeout: float = 10.0) -> Iterator[None]:
    if path.is_symlink():
        raise ValueError("Refusing a symlinked instance lock")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "r+b", buffering=0) as stream:
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"\0")
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("Another process is changing this instance") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_UN)
