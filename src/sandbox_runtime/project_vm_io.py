"""Cancellation-safe host locks shared by VM provisioning, IO and maintenance."""

import asyncio
from contextlib import asynccontextmanager
import os
import stat

from src.local_runtime.files import file_lock


async def _journal(method, *args, **kwargs):
    worker = asyncio.create_task(asyncio.to_thread(method, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
        worker.result()
        raise


@asynccontextmanager
async def host_lock(path, *, timeout=10):
    # The parent is private to the runtime. Reject special files before opening.
    try:
        info = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            raise ValueError("Unsafe VM operation lock")
    context = file_lock(path, timeout=timeout)
    entering = asyncio.create_task(asyncio.to_thread(context.__enter__))
    try:
        await asyncio.shield(entering)
    except asyncio.CancelledError:
        while not entering.done():
            try:
                await asyncio.shield(entering)
            except asyncio.CancelledError:
                continue
        if entering.exception() is None:
            await _journal(context.__exit__, None, None, None)
        raise
    try:
        yield
    finally:
        await _journal(context.__exit__, None, None, None)
