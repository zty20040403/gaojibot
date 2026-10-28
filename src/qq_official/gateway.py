"""QQ gateway transport with explicit durable dispatch acknowledgement.

``on_dispatch`` must return only after the envelope is durably stored (including
ignored-event markers). Exceptions stop the connection without moving the resume
point. Deduplication and transactions belong to the caller, not this module.

Contract: QQ docs and tencent-connect/qqbot-nodejs 429a0f1. For 4009 and
4914/4915 the documented behavior wins over conflicting SDK constant names.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import inspect
import math
import random as random_module
import time
from typing import Any

import aiohttp

from .credentials import validate_gateway_url
from .errors import QQAPIError

GROUP_AND_C2C = 1 << 25


class GatewayError(Exception):
    """Safe diagnostics: never include peer reason, payload, URL, or token."""


class GatewayDisconnected(GatewayError):
    def __init__(self, code: int = 1006):
        self.code = code
        super().__init__(f"gateway closed ({code})")


class GatewayCallbackError(GatewayError):
    pass


class GatewayBackpressureError(GatewayError):
    pass


class GatewaySequenceGap(GatewayError):
    """Locally observed discontinuity; the server has not rejected Resume."""

    def __init__(self):
        super().__init__("noncontiguous dispatch; resume from durable checkpoint")


@dataclass(frozen=True)
class GatewayCheckpoint:
    session_id: str = field(repr=False)
    seq: int

    def __post_init__(self):
        if not self.session_id or type(self.seq) is not int or self.seq < 0:
            raise ValueError("invalid gateway checkpoint")


@dataclass(frozen=True)
class ClosePolicy:
    action: str
    reason: str
    gap: bool = False
    minimum_delay: float = 0


def classify_close_code(code: int) -> ClosePolicy:
    if code == 4004:
        return ClosePolicy("refresh", "authentication_failed", True)
    if code in (4006, 4007):
        return ClosePolicy("identify", "resume_checkpoint_rejected", True)
    if code == 4009:
        return ClosePolicy("resume", "session_timeout_try_resume")
    if code == 4008:
        return ClosePolicy("resume", "gateway_rate_limited", minimum_delay=60)
    if 4900 <= code <= 4913:
        return ClosePolicy("identify", "server_requested_new_session", True)
    if code in (4001, 4002, 4010, 4011, 4012, 4013, 4014):
        return ClosePolicy("stop", "protocol_shard_or_intent_configuration")
    if code in (4914, 4915):
        return ClosePolicy("stop", "platform_status_check_required_docs_sdk_disagree")
    return ClosePolicy("resume", "remote_disconnect")


@dataclass
class IdentifyBudget:
    """One shard uses bucket 0 regardless of the server's concurrency count."""
    remaining: int = 0
    reset_at: float = 0
    max_concurrency: int = 1
    next_identify_at: float = 0
    initialized: bool = False

    def update(self, raw: Mapping[str, Any], now: float) -> None:
        try:
            remaining = raw["remaining"]
            reset_ms = raw["reset_after"]
            concurrency = raw["max_concurrency"]
            if (type(remaining) is not int or remaining < 0
                    or type(concurrency) is not int or concurrency < 1
                    or isinstance(reset_ms, bool) or not isinstance(reset_ms, (int, float))
                    or not math.isfinite(reset_ms) or reset_ms < 0):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise GatewayError("invalid session_start_limit") from None
        # A stale REST observation cannot restore a locally consumed Identify.
        if self.initialized and now < self.reset_at:
            self.remaining = min(self.remaining, remaining)
            self.reset_at = min(self.reset_at, now + reset_ms / 1000)
        else:
            self.remaining = remaining
            self.reset_at = now + reset_ms / 1000
        self.max_concurrency = concurrency
        self.initialized = True

    def delay(self, now: float) -> float:
        if not self.initialized:
            raise GatewayError("session_start_limit required for Identify")
        if self.remaining <= 0:
            # Zero reset_after must not cause a tight REST polling loop.
            return max(1.0, self.reset_at - now)
        return max(0.0, self.next_identify_at - now)

    def consume(self, now: float) -> None:
        if self.remaining <= 0 or now < self.next_identify_at:
            raise GatewayError("Identify attempted outside server budget")
        self.remaining -= 1
        self.next_identify_at = now + 5.0


class _AiohttpSocket:
    def __init__(self, ws):
        self.ws = ws

    async def send(self, payload: dict) -> None:
        await self.ws.send_json(payload)

    async def receive(self) -> dict:
        message = await self.ws.receive()
        if message.type == aiohttp.WSMsgType.TEXT:
            try:
                return message.json()
            except (ValueError, TypeError):
                raise GatewayError("invalid gateway JSON") from None
        if message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSING):
            code = message.data if type(message.data) is int else self.ws.close_code
            raise GatewayDisconnected(code or 1006)
        if message.type == aiohttp.WSMsgType.ERROR:
            raise GatewayDisconnected(self.ws.close_code or 1006)
        raise GatewayError("unsupported gateway frame")

    async def close(self) -> None:
        await self.ws.close()


class GatewayConnection:
    def __init__(
        self, rest_client, *, clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random: Callable[[], float] = random_module.random,
        checkpoint: GatewayCheckpoint | None = None,
        on_checkpoint: Callable[[GatewayCheckpoint | None], Awaitable[None]] | None = None,
        connector=None, max_reconnects: int = 8, max_auth_failures: int = 2,
        hello_timeout: float = 30, max_frame_bytes: int = 2 * 1024 * 1024,
        max_dispatch_queue: int = 64,
    ):
        if (max_reconnects < 0 or max_auth_failures < 1 or hello_timeout <= 0
                or type(max_dispatch_queue) is not int or not 1 <= max_dispatch_queue <= 1024):
            raise ValueError("invalid gateway retry settings")
        self.rest_client = rest_client
        self.clock, self.sleep, self.random = clock, sleep, random
        self.checkpoint, self.on_checkpoint = checkpoint, on_checkpoint
        self.connector = connector or self._connect
        self.max_reconnects, self.max_auth_failures = max_reconnects, max_auth_failures
        self.hello_timeout, self.max_frame_bytes = hello_timeout, max_frame_bytes
        self.max_dispatch_queue = max_dispatch_queue
        self._dispatch_queue: asyncio.Queue | None = None
        self._dispatch_task: asyncio.Task | None = None
        self.state = "closed"
        self.reason = "not_started"
        self.last_close_code: int | None = None
        self.last_rest_error: dict | None = None
        self.message_gap = False
        self.budget = IdentifyBudget()
        self._stop = asyncio.Event()
        self._socket = None
        self._authenticated_token: str | None = None
        self._running = False
        self._awaiting_ack = False
        self._heartbeat_sent_at: float | None = None
        self._established = asyncio.Event()
        self._session_id = checkpoint.session_id if checkpoint else None
        self._observed_seq = checkpoint.seq if checkpoint else None
        self._ready_at: float | None = None

    @property
    def health(self) -> dict:
        return {"state": self.state, "reason": self.reason,
                "close_code": self.last_close_code, "rest_error": self.last_rest_error,
                "message_gap": self.message_gap,
                "durable_seq": self.checkpoint.seq if self.checkpoint else None,
                "dispatch_queue_depth": self._dispatch_queue.qsize() if self._dispatch_queue else 0,
                "dispatch_queue_limit": self.max_dispatch_queue,
                "identify_remaining": self.budget.remaining,
                "identify_max_concurrency": self.budget.max_concurrency}

    def _validate_url(self, url: str) -> str:
        return validate_gateway_url(
            url, allow_test_server=self.rest_client.allow_test_server,
            credentials=self.rest_client.token_manager.credentials)

    @asynccontextmanager
    async def _connect(self, url):
        # No auth is sent in the upgrade; refuse redirects before Identify.
        trace = aiohttp.TraceConfig()

        async def reject_redirect(*args):
            raise GatewayError("gateway redirects are not allowed")

        trace.on_request_redirect.append(reject_redirect)
        async with aiohttp.ClientSession(trace_configs=[trace], trust_env=False,
                                         timeout=aiohttp.ClientTimeout(total=self.hello_timeout)) as session:
            async with session.ws_connect(
                url, autoping=True, heartbeat=None, max_msg_size=self.max_frame_bytes,
            ) as ws:
                yield _AiohttpSocket(ws)

    async def stop(self) -> None:
        self._stop.set()
        if asyncio.current_task() is self._dispatch_task:
            # Let the durable callback return and publish its acknowledgement
            # before session cleanup cancels the independent socket reader.
            return
        if self._socket is not None:
            await self._socket.close()

    async def _pause(self, delay: float) -> None:
        sleeper = asyncio.create_task(self.sleep(max(0.0, delay)))
        stopper = asyncio.create_task(self._stop.wait())
        try:
            await asyncio.wait((sleeper, stopper), return_when=asyncio.FIRST_COMPLETED)
            if sleeper.done():
                await sleeper
        finally:
            for task in (sleeper, stopper):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sleeper, stopper, return_exceptions=True)

    async def _hello(self, socket) -> dict:
        receiver = asyncio.create_task(socket.receive())
        timer = asyncio.create_task(self.sleep(self.hello_timeout))
        try:
            done, _ = await asyncio.wait((receiver, timer), return_when=asyncio.FIRST_COMPLETED)
            if receiver not in done:
                raise GatewayDisconnected(1006)
            return await receiver
        finally:
            for task in (receiver, timer):
                if not task.done():
                    task.cancel()
            await asyncio.gather(receiver, timer, return_exceptions=True)

    async def _clear_checkpoint(self) -> None:
        if self.checkpoint is not None:
            self.message_gap = True
        if self.on_checkpoint is not None:
            try:
                await self.on_checkpoint(None)
            except Exception:
                raise GatewayCallbackError("checkpoint invalidation failed") from None
        self.checkpoint = None
        self._session_id = None
        self._observed_seq = None

    async def _gateway(self) -> str:
        data = await self.rest_client.gateway()
        if not isinstance(data, dict) or not isinstance(data.get("url"), str):
            raise GatewayError("invalid gateway discovery response")
        url = self._validate_url(data["url"])
        limit = data.get("session_start_limit")
        if isinstance(limit, Mapping):
            self.budget.update(limit, self.clock())
        elif self.checkpoint is None:
            raise GatewayError("session_start_limit required for Identify")
        while self.checkpoint is None and not self._stop.is_set():
            delay = self.budget.delay(self.clock())
            if delay <= 0:
                break
            self.reason = "waiting_identify_budget"
            await self._pause(delay)
            if self._stop.is_set():
                break
            # Re-fetch at budget reset rather than invent a daily allowance.
            if self.budget.remaining <= 0:
                data = await self.rest_client.gateway()
                self.budget.update(data.get("session_start_limit", {}), self.clock())
                url = self._validate_url(data["url"])
        return url

    async def _send_heartbeat(self, socket) -> None:
        if not self._awaiting_ack:
            self._awaiting_ack = True
            self._heartbeat_sent_at = self.clock()
        # A second server request cannot extend an already outstanding deadline.
        await socket.send({"op": 1, "d": self.checkpoint.seq if self.checkpoint else None})

    async def _heartbeat(self, socket, interval: float) -> None:
        delay = interval
        while not self._stop.is_set():
            await self.sleep(delay)
            if self._stop.is_set():
                return
            if self._awaiting_ack:
                remaining = interval - (self.clock() - self._heartbeat_sent_at)
                if remaining <= 0:
                    raise GatewayDisconnected(1006)
                # A server-requested heartbeat can occur just before this
                # periodic tick. Wait until its actual ACK deadline.
                delay = remaining
                continue
            await self._send_heartbeat(socket)
            delay = interval

    async def _dispatch(self, frame: dict, on_dispatch) -> None:
        seq, event = frame.get("s"), frame.get("t")
        if type(seq) is not int or seq < 0 or not isinstance(event, str):
            raise GatewayError("invalid dispatch envelope")
        if event == "READY":
            if self.state != "identifying" or self.checkpoint is not None:
                raise GatewayError("unexpected READY for current session")
            data = frame.get("d")
            if not isinstance(data, dict) or not isinstance(data.get("session_id"), str) or not data["session_id"]:
                raise GatewayError("READY missing session")
            self._session_id = data["session_id"]
            self._observed_seq = None
        elif self._session_id is None:
            raise GatewayError("dispatch before READY")
        elif event == "RESUMED" and self.state != "resuming":
            raise GatewayError("unexpected RESUMED for current session")
        # A duplicate is still handed to the durable inbox, which owns dedup.
        previous = self.checkpoint.seq if self.checkpoint else None
        if previous is not None and seq > previous + 1:
            self.message_gap = True
            self.reason = "noncontiguous_dispatch"
            raise GatewaySequenceGap()
        try:
            await on_dispatch(frame)
            next_checkpoint = GatewayCheckpoint(self._session_id, max(previous or 0, seq))
            if self.on_checkpoint is not None:
                await self.on_checkpoint(next_checkpoint)
        except Exception:
            raise GatewayCallbackError("durable dispatch acknowledgement failed") from None
        self.checkpoint = next_checkpoint
        self._observed_seq = seq
        if event in ("READY", "RESUMED"):
            self.state, self.reason = "ready", "connected"
            self._ready_at = self.clock()
            self._established.set()

    async def _ready_watchdog(self) -> None:
        timer = asyncio.create_task(self.sleep(self.hello_timeout))
        established = asyncio.create_task(self._established.wait())
        try:
            await asyncio.wait((timer, established), return_when=asyncio.FIRST_COMPLETED)
            if not self._established.is_set():
                raise GatewayDisconnected(1006)
        finally:
            for task in (timer, established):
                if not task.done():
                    task.cancel()
            await asyncio.gather(timer, established, return_exceptions=True)

    async def _dispatch_worker(self, queue: asyncio.Queue, on_dispatch) -> None:
        while not self._stop.is_set():
            frame = await queue.get()
            try:
                await self._dispatch(frame, on_dispatch)
            finally:
                queue.task_done()

    async def _receive(self, socket, queue: asyncio.Queue) -> None:
        while not self._stop.is_set():
            frame = await socket.receive()
            if not isinstance(frame, dict) or type(frame.get("op")) is not int:
                raise GatewayError("invalid gateway envelope")
            op = frame["op"]
            if op == 0:
                try:
                    queue.put_nowait(frame)
                except asyncio.QueueFull:
                    # Never block control/ACK processing behind an unbounded
                    # persistence backlog. Stop without acknowledging the frame.
                    raise GatewayBackpressureError("dispatch queue overflow") from None
                if frame.get("t") in {"READY", "RESUMED"}:
                    # The peer has completed its handshake. Durable readiness is
                    # still published only by the sequential dispatch worker.
                    self._established.set()
            elif op == 11:
                self._awaiting_ack = False
                self._heartbeat_sent_at = None
            elif op == 1:
                await self._send_heartbeat(socket)
            elif op == 7:
                raise GatewayDisconnected(1000)
            elif op == 9:
                # Invalid session always obtains a fresh Identify budget.
                raise GatewayDisconnected(4006)
            else:
                raise GatewayError("unexpected gateway opcode")

    async def _session(self, url: str, on_dispatch) -> None:
        async with self.connector(url) as socket:
            self._socket = socket
            try:
                hello = await self._hello(socket)
                data = hello.get("d") if isinstance(hello, dict) else None
                interval = data.get("heartbeat_interval") if isinstance(data, dict) else None
                if (not isinstance(hello, dict) or hello.get("op") != 10
                        or isinstance(interval, bool) or not isinstance(interval, (int, float))
                        or not math.isfinite(interval) or interval <= 0):
                    raise GatewayError("invalid Hello heartbeat interval")
                token = await self.rest_client.token_manager.get()
                self._authenticated_token = token
                if self.checkpoint:
                    self.state = "resuming"
                    await socket.send({"op": 6, "d": {"token": f"QQBot {token}",
                        "session_id": self.checkpoint.session_id, "seq": self.checkpoint.seq}})
                else:
                    self.budget.consume(self.clock())
                    self.state = "identifying"
                    await socket.send({"op": 2, "d": {"token": f"QQBot {token}",
                        "intents": GROUP_AND_C2C, "shard": [0, 1]}})
                self._awaiting_ack = False
                self._heartbeat_sent_at = None
                self._established.clear()
                queue = asyncio.Queue(maxsize=self.max_dispatch_queue)
                self._dispatch_queue = queue
                worker = asyncio.create_task(self._dispatch_worker(queue, on_dispatch))
                self._dispatch_task = worker
                heartbeat = asyncio.create_task(self._heartbeat(socket, interval / 1000))
                receiver = asyncio.create_task(self._receive(socket, queue))
                watchdog = asyncio.create_task(self._ready_watchdog())
                tasks = (worker, receiver, heartbeat, watchdog)
                failure = None
                try:
                    pending = set(tasks)
                    while pending:
                        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                        # Fixed order, plus the cleanup audit below, ensures a
                        # persistence failure cannot be masked by an EOF/timeout.
                        for task in tasks:
                            if task in done:
                                try:
                                    await task
                                except Exception as exc:
                                    if failure is None or isinstance(exc, GatewayCallbackError):
                                        failure = exc
                        if failure is not None or any(task in done for task in (worker, receiver, heartbeat)):
                            break
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
                    for outcome in outcomes:
                        if isinstance(outcome, GatewayCallbackError):
                            failure = outcome
                    self._dispatch_queue = None
                    self._dispatch_task = None
                if failure is not None:
                    raise failure
            finally:
                self._socket = None

    async def run(self, on_dispatch: Callable[[dict], Awaitable[None]]) -> None:
        if self._running:
            raise GatewayError("gateway already running")
        self._running = True
        self._stop.clear()
        attempts = auth_failures = 0
        try:
            while not self._stop.is_set():
                self.state = "connecting"
                self._ready_at = None
                policy = None
                try:
                    url = await self._gateway()
                    if self._stop.is_set():
                        break
                    await self._session(url, on_dispatch)
                    if self._stop.is_set():
                        break
                    failure = GatewayDisconnected(1000)
                except QQAPIError as exc:
                    self.last_rest_error = {"category": exc.category, "status": exc.status,
                                            "code": exc.code, "stage": exc.stage}
                    self.last_close_code = None
                    retry_delay = 0.0
                    if (exc.category == "rate_limited"
                            and isinstance(exc.retry_after, (int, float))
                            and not isinstance(exc.retry_after, bool)
                            and math.isfinite(exc.retry_after)):
                        retry_delay = max(0.0, exc.retry_after)
                    # RestClient invalidated only the token used by this HTTP
                    # request. Do not invalidate a newer concurrent token or
                    # discard a valid gateway session because discovery failed.
                    policy = ClosePolicy(
                        "http_auth_retry" if exc.category == "authentication" else "resume",
                        f"rest_{exc.category}", minimum_delay=retry_delay)
                except GatewaySequenceGap:
                    # A missing local frame is not a server 4007 rejection.
                    # Preserve the durable point so Resume can replay the gap.
                    self.last_close_code = None
                    policy = ClosePolicy("resume", "noncontiguous_dispatch", True)
                except GatewayBackpressureError:
                    self.reason = "dispatch_queue_overflow"
                    raise
                except GatewayCallbackError:
                    self.reason = "durable_callback_failed"
                    raise
                except GatewayDisconnected as exc:
                    failure = exc
                except GatewayError:
                    self.reason = "gateway_protocol_error"
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Network/HTTP details may contain credential-bearing URLs.
                    failure = GatewayDisconnected(1006)
                if self._stop.is_set():
                    break
                if policy is None:
                    self.last_close_code = failure.code
                    policy = classify_close_code(failure.code)
                self.reason = policy.reason
                self.message_gap = self.message_gap or policy.gap
                if policy.action == "stop":
                    break
                if policy.action in {"refresh", "http_auth_retry"}:
                    auth_failures += 1
                    if auth_failures >= self.max_auth_failures:
                        self.reason = "authentication_retry_exhausted"
                        break
                    if policy.action == "refresh":
                        result = self.rest_client.token_manager.invalidate(self._authenticated_token)
                        if inspect.isawaitable(result):
                            await result
                if policy.action in ("identify", "refresh"):
                    await self._clear_checkpoint()
                # Brief READY-then-close loops must still exhaust the budget.
                if self._ready_at is not None and self.clock() - self._ready_at >= 60:
                    attempts = auth_failures = 0
                if attempts >= self.max_reconnects:
                    self.reason = "reconnect_budget_exhausted"
                    break
                base = min(60.0, 2.0 ** min(attempts, 6))
                delay = max(policy.minimum_delay, base * (0.5 + 0.5 * self.random()))
                attempts += 1
                await self._pause(delay)
        finally:
            self.state = "closed"
            if self._stop.is_set():
                self.reason = "stopped"
            self._running = False
