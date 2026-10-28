"""Bounded read-only project notes, also runnable inside an isolated VM guest."""

import codecs
from contextlib import ExitStack
import json
import os
import stat

NOTES_BYTES = 8192


def _identity(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mode,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_nlink,
    )


def read_notes(root):
    with ExitStack() as stack:

        def opened(path, flags, *, directory=None):
            fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            stack.callback(os.close, fd)
            return fd

        root_fd = opened(root, os.O_RDONLY | os.O_DIRECTORY)
        root_info = os.fstat(root_fd)
        try:
            parent = opened(".gaoji", os.O_RDONLY | os.O_DIRECTORY, directory=root_fd)
            parent_info = os.fstat(parent)
            if parent_info.st_dev != root_info.st_dev:
                raise ValueError("Project notes crossed a mount boundary")
            fd = opened("NOTES.md", os.O_RDONLY, directory=parent)
        except FileNotFoundError:
            return {"state": "absent_in_workspace"}
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_dev != root_info.st_dev
            or before.st_mode & (stat.S_ISUID | stat.S_ISGID)
        ):
            raise ValueError("Unsafe project notes")
        prefix = os.read(fd, NOTES_BYTES)
        after = os.fstat(fd)
        current = os.stat("NOTES.md", dir_fd=parent, follow_symlinks=False)
        current_parent = os.stat(".gaoji", dir_fd=root_fd, follow_symlinks=False)
        if (
            _identity(before) != _identity(after)
            or _identity(after) != _identity(current)
            or current_parent.st_ino != parent_info.st_ino
            or current_parent.st_dev != parent_info.st_dev
            or len(prefix) != min(before.st_size, NOTES_BYTES)
        ):
            raise ValueError("Project notes changed during reading")
        content = codecs.getincrementaldecoder("utf-8")().decode(
            prefix, final=before.st_size <= NOTES_BYTES
        )
        return {
            "state": "available",
            "content": content,
            "truncated": before.st_size > len(content.encode("utf-8")),
        }


if __name__ == "__main__":
    print(json.dumps(read_notes("/workspace"), ensure_ascii=False))
