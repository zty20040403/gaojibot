"""Bounded regular-file reads executed inside the existing guest only."""
from __future__ import annotations

import inspect


def bounded_regular_file(root, relative_path, limit):
    import os
    import stat

    if type(limit) is not int or not 1 <= limit <= 256 * 1024 * 1024:
        raise ValueError('invalid_export_limit')
    if (not isinstance(relative_path, str) or not relative_path or relative_path.startswith('/')
            or '\\' in relative_path or '\x00' in relative_path
            or any(part in {'', '.', '..'} for part in relative_path.split('/'))):
        raise ValueError('invalid_export_path')
    parts = relative_path.split('/')
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    fd = None
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= limit:
            raise ValueError('invalid_export_file_or_size')
        blocks, received = [], 0
        while block := os.read(fd, min(65536, limit - received + 1)):
            received += len(block)
            if received > limit:
                raise ValueError('export_limit_exceeded')
            blocks.append(block)
        after = os.fstat(fd)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                or received != before.st_size):
            raise ValueError('export_file_changed')
        return b''.join(blocks)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory)


# This is the exact source handed to Docker/VM. No injected host path or helper
# invocation from model input; guest root is fixed, path/limit are argv values.
EXPORT_FILE_SCRIPT = inspect.getsource(bounded_regular_file) + '''
import sys
try:
    content = bounded_regular_file('/workspace', sys.argv[1], int(sys.argv[2]))
except (ValueError, OSError):
    sys.stderr.write('bounded_export_failed')
    raise SystemExit(1)
sys.stdout.buffer.write(content)
'''
