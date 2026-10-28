"""Bounded reads of accepted task snapshots, not arbitrary host paths."""

import hashlib
import os
import re
import stat

from src.project_content import MAX_FILE_BYTES


def read_update_artifact(state_dir, task_id, artifact):
    digest, size = artifact.get("snapshot"), artifact.get("size")
    if (type(task_id) is not int or task_id <= 0
            or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or type(size) is not int or not 0 < size <= MAX_FILE_BYTES):
        raise ValueError("Invalid project artifact identity or size")
    # Walk with no-follow directory descriptors: even the parent task directory
    # must not redirect an otherwise well-formed digest outside the artifact root.
    root = os.open(state_dir / "subagent_artifacts", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        directory = os.open(str(task_id), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        try:
            fd = os.open(digest, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)
    finally:
        os.close(root)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            raise ValueError("Project artifact is not the accepted regular file")
        content = source.read(size + 1)
    if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("Project artifact bytes changed after acceptance")
    return content
