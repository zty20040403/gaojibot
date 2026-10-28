"""Private host orchestration for browser pipes and persistent receipts.

Launch/stop callbacks belong to the actual OCI/KVM driver. This module neither
launches a browser on the Bot host nor treats worker stdout as process evidence.
The caller must persist the session/sequence mapping before invoking an action.
"""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib

from .project_browser_journal import HEX_ID
from .project_browser_worker import FRAME_LIMIT
from .project_vm_io import _journal, host_lock
from .wire import ProtocolError, encoded, read_header, write_frame


class BrowserPipe:
    """One already-isolated process; no shell command or reconnect fallback."""
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer

    async def exchange(self, frame, *, timeout=160):
        async with asyncio.timeout(timeout):
            await write_frame(self.writer, frame, timeout=10)
            return await read_header(self.reader, limit=FRAME_LIMIT, timeout=timeout)

    async def eof(self):
        self.writer.close()
        await self.writer.wait_closed()


class ProjectBrowserHost:
    def __init__(self, journal):
        self.journal = journal
        self.channels = {}
        self.locks = {}

    @asynccontextmanager
    async def _lock(self, session_id):
        if not isinstance(session_id, str) or HEX_ID.fullmatch(session_id) is None:
            raise ProtocolError("Invalid browser session identity")
        async with self.locks.setdefault(session_id, asyncio.Lock()):
            # Cross-process exclusion also covers launch and shutdown. Otherwise
            # a second controller could confirm absence while launch is in flight.
            async with host_lock(self.journal.root / f"{session_id}.pipe.lock", timeout=0):
                yield

    async def open(self, config, runtime, launch):
        # Persist before the driver creates a process. An uncertain launch keeps
        # its session ID so a restarted driver can stop that exact incarnation.
        config, runtime = deepcopy(config), deepcopy(runtime)
        record = await _journal(self.journal.begin, config, runtime)
        identity, sid = record["binding"], record["session_id"]
        async with self._lock(sid):
            try:
                current = await _journal(self.journal.current, identity, sid)
                if current["state"] != "starting":
                    raise ProtocolError("Browser launch was cancelled before process creation")
                channel = await launch(deepcopy(record))
                self.channels[sid] = channel
                ready = await channel.exchange(config, timeout=30)
                await _journal(self.journal.attach, identity, sid, ready)
            except BaseException:
                await _journal(self.journal.uncertain, identity, sid)
                raise
        return await _journal(self.journal.current, identity, sid)

    async def execute(self, identity, session_id, sequence, method, args, *, persist):
        """persist must be an idempotent, durable application-side commit callback."""
        async with self._lock(session_id):
            frame, reply = await _journal(
                self.journal.prepare, identity, session_id, sequence, method, args
            )
            if frame is not None:
                try:
                    channel = self.channels.get(session_id)
                    if channel is None:
                        raise ProtocolError("Browser channel was lost; do not relaunch its action")
                    reply = await channel.exchange(frame)
                    await _journal(self.journal.observe, identity, session_id, sequence, reply)
                except BaseException:
                    await _journal(self.journal.uncertain, identity, session_id)
                    raise
            if reply is None:
                raise ProtocolError("Browser action is unconfirmed; a retry cannot execute it again")
            return await self._publish(identity, session_id, sequence, reply, persist)

    async def _publish(self, identity, session_id, sequence, reply, persist):
        # An exception/lost acknowledgement here retains the host receipt. The
        # next replay runs only this idempotent publication, not the browser.
        await persist(session_id, sequence, deepcopy(reply))
        if reply["state"] == "completed":
            await _journal(self.journal.acknowledge, identity, session_id, sequence,
                           hashlib.sha256(encoded(reply)).hexdigest())
        return reply

    async def receive_pending(self, identity, session_id, *, persist):
        """Read a late response from the same pipe, never resend an uncertain action."""
        async with self._lock(session_id):
            record = await _journal(self.journal.current, identity, session_id)
            if record["last"] is None:
                raise ProtocolError("No browser action is pending")
            reply = record["last"]["response"]
            if reply is None:
                channel = self.channels.get(session_id)
                if channel is None:
                    raise ProtocolError("The original browser channel is unavailable")
                reply = await read_header(channel.reader, limit=FRAME_LIMIT, timeout=10)
                await _journal(self.journal.observe, identity, session_id, record["sequence"], reply)
            return await self._publish(identity, session_id, record["sequence"], reply, persist)

    async def drain(self, identity, session_id, stop_and_observe, *, cancel_pending=False):
        """Driver stops its owned cgroup/container and independently observes it."""
        async with self._lock(session_id):
            record = await _journal(self.journal.closing, identity, session_id,
                                    cancel_pending=cancel_pending)
            if record["state"] == "closed":
                return record
            try:
                evidence = await stop_and_observe(deepcopy(record), self.channels.get(session_id))
                await _journal(self.journal.closed, identity, session_id, evidence)
            except BaseException:
                await _journal(self.journal.uncertain, identity, session_id)
                raise
            self.channels.pop(session_id, None)
            return await _journal(self.journal.current, identity, session_id)
