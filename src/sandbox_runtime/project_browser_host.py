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
        self.pending = None

    async def exchange(self, frame, *, timeout=160):
        if self.pending is not None:
            raise ProtocolError("Previous browser frame has not been received")
        # Keep the same framed read alive after a caller times out/cancels. A
        # fresh read could mistake the middle of the old JSON body for a header.
        self.pending = asyncio.create_task(read_header(self.reader, limit=FRAME_LIMIT, timeout=1800))
        self.pending.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        await write_frame(self.writer, frame, timeout=10)
        return await self.receive(timeout=timeout)

    async def receive(self, *, timeout=10):
        if self.pending is None:
            raise ProtocolError("No original browser frame is pending")
        result = await asyncio.wait_for(asyncio.shield(self.pending), timeout)
        self.pending = None
        return result

    async def eof(self):
        self.writer.close()
        try:
            await self.writer.wait_closed()
        finally:
            if self.pending is not None:
                self.pending.cancel()
                await asyncio.gather(self.pending, return_exceptions=True)
                self.pending = None


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

    async def open(self, config, runtime, launch, *, launch_id=None):
        # Persist before the driver creates a process. An uncertain launch keeps
        # its session ID so a restarted driver can stop that exact incarnation.
        config, runtime = deepcopy(config), deepcopy(runtime)
        record = await _journal(self.journal.begin, config, runtime, launch_id=launch_id)
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

    async def execute(self, identity, session_id, sequence, method, args, *, persist=None, before_dispatch=None):
        """Without a durable commit callback, return an unacknowledged observation."""
        async with self._lock(session_id):
            frame, reply = await _journal(
                self.journal.prepare, identity, session_id, sequence, method, args
            )
            if frame is not None:
                try:
                    if before_dispatch is not None:
                        await before_dispatch()
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
        if persist is not None:
            await persist(session_id, sequence, deepcopy(reply))
        if persist is not None and reply["state"] == "completed":
            await _journal(self.journal.acknowledge, identity, session_id, sequence,
                           hashlib.sha256(encoded(reply)).hexdigest())
        return reply

    async def receive_pending(self, identity, session_id, *, persist=None, sequence=None):
        """Read a late response from the same pipe, never resend an uncertain action."""
        async with self._lock(session_id):
            record = await _journal(self.journal.current, identity, session_id)
            if record["last"] is None:
                raise ProtocolError("No browser action is pending")
            if sequence is not None and (type(sequence) is not int or sequence != record["sequence"]):
                raise ProtocolError("Browser receipt sequence changed; refusing another action's result")
            reply = record["last"]["response"]
            if reply is None:
                channel = self.channels.get(session_id)
                if channel is None:
                    raise ProtocolError("The original browser channel is unavailable")
                reply = await channel.receive(timeout=10)
                await _journal(self.journal.observe, identity, session_id, record["sequence"], reply)
            return await self._publish(identity, session_id, record["sequence"], reply, persist)

    async def acknowledge(self, identity, session_id, sequence, response_sha256):
        """Separate RPC used only after the application commits the exact response."""
        async with self._lock(session_id):
            await _journal(self.journal.acknowledge, identity, session_id, sequence, response_sha256)
            return await _journal(self.journal.current, identity, session_id)

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
