"""Single-project browser process on inherited pipes, never a listening service.

The private host launcher owns bootstrap, authorization and durable receipts.
Replies here are observations, not proof of sandbox process quiescence. A new
worker cannot replay the old worker's uncertain actions.
"""

import asyncio
from copy import deepcopy
import hashlib
from pathlib import Path
import re
import stat
import sys
import uuid

from src.browser_runtime import BrowserManager
from .wire import ProtocolError, encoded, read_header, write_frame

FRAME_LIMIT = 256 * 1024
METHODS = {
    "browser_navigate": ("navigate", {"url"}, set()),
    "browser_snapshot": ("snapshot", set(), set()),
    "browser_click": ("click", {"ref"}, set()),
    "browser_type": ("type_text", {"ref", "text"}, {"submit"}),
    "browser_press_key": ("press_key", {"key"}, set()),
    "browser_scroll": ("scroll", {"amount"}, set()),
    "browser_wait_for": ("wait_for", {"text"}, {"timeout_seconds"}),
    "browser_close": ("close_session", set(), set()),
    "browser_clear": ("clear_profile", set(), set()),
}


def binding(value):
    if (not isinstance(value, dict)
            or set(value) != {"instance_id", "owner", "project_id", "generation", "fence"}
            or any(type(value[k]) is not int or not 1 <= value[k] < 2**53
                   for k in ("project_id", "generation", "fence"))
            or any(not isinstance(value[k], str) or not 1 <= len(value[k]) <= 200
                   or any(ord(c) < 32 for c in value[k]) for k in ("instance_id", "owner"))):
        raise ProtocolError("Invalid project browser identity")
    return dict(value)


def arguments(method, args):
    if not isinstance(method, str) or method not in METHODS or not isinstance(args, dict):
        raise ProtocolError("Unsupported project browser operation")
    _, required, optional = METHODS[method]
    if not required <= set(args) or set(args) - required - optional:
        raise ProtocolError("Unexpected project browser arguments")
    for key, maximum in (("url", 2000), ("ref", 32), ("text", 10000), ("key", 30)):
        if key in args and (not isinstance(args[key], str) or len(args[key]) > maximum
                            or "\0" in args[key]):
            raise ProtocolError("Invalid project browser text argument")
    if "ref" in args and re.fullmatch(r"b[1-9][0-9]*", args["ref"]) is None:
        raise ProtocolError("Invalid project browser element reference")
    if "submit" in args and type(args["submit"]) is not bool:
        raise ProtocolError("Invalid project browser submit flag")
    for key, lo, hi in (("amount", -5000, 5000), ("timeout_seconds", 1, 60)):
        if key in args and (type(args[key]) is not int or not lo <= args[key] <= hi):
            raise ProtocolError("Invalid project browser numeric argument")
    if method == "browser_wait_for" and (not args["text"] or len(args["text"]) > 500):
        raise ProtocolError("Invalid project browser wait text")
    return deepcopy(args)


class ProjectBrowserWorker:
    def __init__(self, identity, manager):
        self.identity = binding(identity)
        if manager.bound_owner != self.identity["owner"]:
            raise ProtocolError("Browser manager is not bound to this project owner")
        self.manager = manager
        self.worker_id = uuid.uuid4().hex
        self.sequence = 0
        self.last = None
        self.faulted = False
        self.closed = False
        self.lock = asyncio.Lock()

    def ready(self):
        return {"schema": 1, "worker_id": self.worker_id,
                "binding": dict(self.identity), "state": "ready"}

    async def dispatch(self, request):
        if (not isinstance(request, dict)
                or set(request) != {"schema", "worker_id", "sequence", "method", "args"}
                or type(request["schema"]) is not int or request["schema"] != 1
                or request["worker_id"] != self.worker_id
                or type(request["sequence"]) is not int
                or not 1 <= request["sequence"] < 2**53
                or len(encoded(request)) > FRAME_LIMIT):
            raise ProtocolError("Project browser request belongs to another worker or protocol")
        method, seq = request["method"], request["sequence"]
        args = arguments(method, request["args"])
        digest = hashlib.sha256(encoded({"method": method, "args": args})).hexdigest()
        async with self.lock:
            if self.closed:
                raise ProtocolError("Project browser worker is closed")
            if seq == self.sequence and self.last is not None:
                if self.last["arguments_sha256"] != digest:
                    raise ProtocolError("Project browser sequence was reused with different arguments")
                return deepcopy(self.last)
            if self.faulted or seq != self.sequence + 1:
                raise ProtocolError("Project browser sequence is stale, skipped or uncertain")
            self.sequence = seq
            result = {**self.ready(), "sequence": seq, "arguments_sha256": digest,
                      "state": "needs_attention", "error": "browser_outcome_unknown"}
            # Reserve the sequence before any browser effect. Cancellation after
            # a click is not permission for the host to repeat that click.
            self.last, self.faulted = result, True
            try:
                if method == "browser_wait_for":
                    args.setdefault("timeout_seconds", 10)
                value = await getattr(self.manager, METHODS[method][0])(
                    self.identity["owner"], **args
                )
                reply = {k: v for k, v in result.items() if k != "error"}
                reply.update(state="completed", result=value)
                if len(encoded(reply)) > FRAME_LIMIT:
                    raise ProtocolError("Project browser observation exceeds its limit")
                self.last, self.faulted = reply, False
            except asyncio.CancelledError:
                raise
            except Exception:
                # Exceptions may contain typed passwords or private page data.
                # The host keeps the action uncertain; do not leak diagnostics.
                pass
            return deepcopy(self.last)

    async def close(self):
        async with self.lock:
            self.closed = True
            async with asyncio.timeout(45):
                await self.manager.close()


def configured_worker(config):
    """Bootstrap is supplied only by the trusted launcher, never by an LLM."""
    if (not isinstance(config, dict)
            or set(config) != {"schema", "binding", "workspace", "executable_path",
                               "allow_private_network", "max_profile_bytes", "cache_bytes"}
            or type(config["schema"]) is not int or config["schema"] != 1
            or type(config["allow_private_network"]) is not bool
            or not isinstance(config["executable_path"], str)
            or (config["executable_path"] and not Path(config["executable_path"]).is_absolute())
            or "\0" in config["executable_path"]
            or not isinstance(config["workspace"], str)):
        raise ProtocolError("Invalid project browser bootstrap")
    identity = binding(config["binding"])
    workspace = Path(config["workspace"])
    if (not workspace.is_absolute() or workspace.resolve(strict=True) != workspace
            or not stat.S_ISDIR(workspace.lstat().st_mode)):
        raise ProtocolError("Project browser needs its real mounted workspace")
    parent = workspace / ".gaoji"
    parent.mkdir(mode=0o700, exist_ok=True)
    if not stat.S_ISDIR(parent.lstat().st_mode):
        raise ProtocolError("Project browser metadata directory is not real")
    return ProjectBrowserWorker(identity, BrowserManager(
        parent / "browser", bound_owner=identity["owner"],
        executable_path=config["executable_path"],
        allow_private_network=config["allow_private_network"],
        max_profile_bytes=config["max_profile_bytes"], cache_bytes=config["cache_bytes"],
    ))


async def serve(reader, writer, *, idle_seconds=1800, factory=configured_worker):
    """One inherited channel, one host-bound worker, bounded waits and frames."""
    worker = None
    try:
        config = await read_header(reader, limit=FRAME_LIMIT)
        worker = factory(config)
        await write_frame(writer, worker.ready())
        while True:
            try:
                request = await read_header(reader, limit=FRAME_LIMIT, timeout=idle_seconds)
            except asyncio.IncompleteReadError as exc:
                if exc.partial:
                    raise ProtocolError("Truncated project browser request") from None
                break
            except TimeoutError:
                break
            async with asyncio.timeout(150):
                reply = await worker.dispatch(request)
            await write_frame(writer, reply)
    finally:
        if worker is not None:
            await worker.close()


async def stdio():
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=FRAME_LIMIT)
    read_transport, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )
    writer = None
    try:
        write_transport, protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, sys.stdout.buffer
        )
        writer = asyncio.StreamWriter(write_transport, protocol, None, loop)
        await serve(reader, writer)
    finally:
        read_transport.close()
        if writer is not None:
            writer.close()


def main():
    try:
        asyncio.run(stdio())
    except (Exception, KeyboardInterrupt):
        print("Project browser stopped; unacknowledged actions need reconciliation", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
