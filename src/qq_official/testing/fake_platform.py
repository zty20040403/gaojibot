"""Deterministic local QQ fixture; never proxies requests to Tencent.

Synthetic validation codes are deliberately outside the documented QQ table;
tests must not turn their names into claims about the real platform.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import time
from typing import Any

from aiohttp import web, WSMsgType

FAKE_DUPLICATE_SEQUENCE = 90000001
FAKE_INVALID_PAYLOAD = 90000002
FAKE_UNKNOWN_WINDOW = 90000003


@dataclass
class FakeWindow:
    expires_at: float
    max_replies: int
    used: int = 0
    sequences: set[int] = field(default_factory=set)


class FakePlatform:
    app_id = "TEST_APP_ID"
    app_secret = "TEST_APP_SECRET"
    access_token = "TEST_ACCESS_TOKEN"

    def __init__(self, *, clock=time.time, heartbeat_interval_ms: int = 60_000,
                 nonmention_reply_allowed: bool = False, day_origin: float = 0):
        self.clock = clock
        self.heartbeat_interval_ms = heartbeat_interval_ms
        self.nonmention_reply_allowed = nonmention_reply_allowed
        self.day_origin = day_origin
        self.ack_heartbeats = True
        self.resume_invalid_count = 0
        self.identify_close_codes: deque[int] = deque()
        self.next_errors: deque[tuple[int, int]] = deque()
        self.drop_next_response = False
        self.echo_messages = False
        self.redirect_gateway_to: str | None = None
        self.token_calls = 0
        self.gateway_queries = 0
        self.identify_count = 0
        self.resume_count = 0
        self.heartbeats: list[int | None] = []
        self.received: list[dict] = []
        self.connections: set[web.WebSocketResponse] = set()
        self.ready_notifications: asyncio.Queue = asyncio.Queue()
        self.windows: dict[tuple[str, str, str, str], FakeWindow] = {}
        self.proactive_allowed: dict[tuple[str, str], bool] = {}
        self.session_start_limit = {"total": 100, "remaining": 100,
                                    "reset_after": 60_000, "max_concurrency": 1}
        self._budget_reset_at = self.clock() + 60
        self._proactive_times: list[tuple[float, str, str]] = []
        self._proactive_day_counts: dict[tuple[str, str, int], int] = {}
        self._seq = 0
        self._session = "TEST_SESSION_1"
        self._events: list[dict] = []
        self._runner: web.AppRunner | None = None
        self.base_url = ""
        self.gateway_url = ""

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def start(self) -> None:
        if self._runner is not None:
            raise RuntimeError("fake platform already started")
        app = web.Application(client_max_size=2 * 1024 * 1024)
        app.router.add_post("/app/getAppAccessToken", self._token)
        app.router.add_get("/gateway/bot", self._gateway)
        app.router.add_get("/gateway", self._gateway)
        app.router.add_get("/ws", self._websocket)
        app.router.add_post("/v2/{collection:groups|users}/{target}/messages", self._send)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        self.gateway_url = f"ws://127.0.0.1:{port}/ws"

    async def close(self) -> None:
        for ws in list(self.connections):
            await ws.close()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def stop(self) -> None:
        await self.close()

    async def wait_ready(self) -> int:
        """Wait for the next Identify/Resume, returning its opcode."""
        return await self.ready_notifications.get()

    def add_window(self, kind: str, target: str, message_id: str, *, issued_at=None,
                   ttl=None, max_replies=None, credential_kind="msg_id") -> FakeWindow:
        key = (kind, target, credential_kind, message_id)
        if key in self.windows:
            return self.windows[key]
        now = self.clock() if issued_at is None else issued_at
        window = FakeWindow(now + (ttl if ttl is not None else (300 if kind == "group" else 3600)),
                            max_replies if max_replies is not None else (5 if kind == "group" else 4))
        self.windows[key] = window
        return window

    async def emit(self, event_type: str, data: dict, event_id: str | None = None) -> dict:
        """Store every event for Resume, and dispatch to current connections."""
        self._seq += 1
        frame = {"op": 0, "t": event_type, "s": self._seq,
                 "id": event_id or f"TEST_EVENT_{self._seq}", "d": data}
        self._events.append(frame)
        if event_type in {"GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"}:
            kind = "user" if event_type == "C2C_MESSAGE_CREATE" else "group"
            target = (data.get("author", {}).get("user_openid") if kind == "user"
                      else data.get("group_openid"))
            at_bot = event_type == "GROUP_AT_MESSAGE_CREATE" or any(
                isinstance(item, dict) and item.get("is_you") is True for item in data.get("mentions", []))
            if target and data.get("id") and (kind == "user" or at_bot or self.nonmention_reply_allowed):
                issued = data.get("timestamp")
                try:
                    issued = datetime.fromisoformat(issued.replace("Z", "+00:00")).timestamp() if isinstance(issued, str) else self.clock()
                except ValueError:
                    issued = self.clock()
                self.add_window(kind, target, data["id"], issued_at=issued)
        if event_type.startswith("GROUP_"):
            kind, target = "group", data.get("group_openid")
        else:
            kind, target = "user", data.get("openid") or data.get("user_openid")
        if target and event_type.endswith("MSG_RECEIVE"):
            self.proactive_allowed[(kind, target)] = True
        elif target and event_type.endswith("MSG_REJECT"):
            self.proactive_allowed[(kind, target)] = False
        elif target and event_type == "GROUP_DEL_ROBOT":
            self.proactive_allowed[(kind, target)] = False
            for key in list(self.windows):
                if key[:2] == (kind, target):
                    del self.windows[key]
        for ws in list(self.connections):
            if not ws.closed:
                await ws.send_json(frame)
        return frame

    async def send_opcode(self, op: int, data: Any = None) -> None:
        for ws in list(self.connections):
            if not ws.closed:
                await ws.send_json({"op": op, "d": data})

    async def close_connections(self, code: int = 1000) -> None:
        for ws in list(self.connections):
            await ws.close(code=code)

    async def _token(self, request):
        self.token_calls += 1
        body = await request.json()
        if body != {"appId": self.app_id, "clientSecret": self.app_secret}:
            return web.json_response({"code": FAKE_INVALID_PAYLOAD}, status=401)
        return web.json_response({"access_token": self.access_token, "expires_in": 7200})

    def _authorized(self, request) -> bool:
        return request.headers.get("Authorization") == f"QQBot {self.access_token}"

    async def _gateway(self, request):
        self.gateway_queries += 1
        if not self._authorized(request):
            return web.json_response({"code": FAKE_INVALID_PAYLOAD}, status=401)
        if self.clock() >= self._budget_reset_at:
            self.session_start_limit["remaining"] = self.session_start_limit["total"]
            self._budget_reset_at = self.clock() + 60
        limit = dict(self.session_start_limit)
        limit["reset_after"] = max(0, int((self._budget_reset_at - self.clock()) * 1000))
        return web.json_response({"url": self.gateway_url, "shards": 1, "session_start_limit": limit})

    async def _websocket(self, request):
        if self.redirect_gateway_to is not None:
            raise web.HTTPFound(self.redirect_gateway_to)
        ws = web.WebSocketResponse(heartbeat=None, max_msg_size=2 * 1024 * 1024)
        await ws.prepare(request)
        await ws.send_json({"op": 10, "d": {"heartbeat_interval": self.heartbeat_interval_ms}})
        authenticated = False
        try:
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                try:
                    frame = message.json()
                    op, data = frame.get("op"), frame.get("d")
                except (ValueError, AttributeError):
                    await ws.close(code=4002)
                    break
                if op in (2, 6):
                    if not isinstance(data, dict) or data.get("token") != f"QQBot {self.access_token}":
                        await ws.close(code=4004)
                        break
                    if op == 2:
                        self.identify_count += 1
                        if self.identify_close_codes:
                            await ws.close(code=self.identify_close_codes.popleft())
                            break
                        if data.get("intents") != 1 << 25 or data.get("shard") != [0, 1]:
                            await ws.close(code=4014)
                            break
                        if self.session_start_limit["remaining"] <= 0:
                            await ws.close(code=4008)
                            break
                        self.session_start_limit["remaining"] -= 1
                        if self.identify_count > 1:
                            self._session = f"TEST_SESSION_{self.identify_count}"
                            self._seq += 1
                        ready_seq = 0 if self.identify_count == 1 else self._seq
                        await ws.send_json({"op": 0, "t": "READY", "s": ready_seq,
                                            "d": {"session_id": self._session, "user": {"id": "TEST_BOT"}}})
                        if self.identify_count == 1:
                            for event in self._events:
                                await ws.send_json(event)
                    else:
                        self.resume_count += 1
                        if self.resume_invalid_count or data.get("session_id") != self._session:
                            self.resume_invalid_count = max(0, self.resume_invalid_count - 1)
                            await ws.send_json({"op": 9, "d": False})
                            continue
                        seq = data.get("seq")
                        if type(seq) is not int or seq < 0 or seq > self._seq:
                            await ws.close(code=4007)
                            break
                        for event in self._events:
                            if event["s"] > seq:
                                await ws.send_json(event)
                        self._seq += 1
                        resumed = {"op": 0, "t": "RESUMED", "s": self._seq, "d": {}}
                        self._events.append(resumed)
                        await ws.send_json(resumed)
                    authenticated = True
                    self.connections.add(ws)
                    self.ready_notifications.put_nowait(op)
                elif op == 1 and authenticated:
                    self.heartbeats.append(data)
                    if self.ack_heartbeats:
                        await ws.send_json({"op": 11, "d": None})
                else:
                    await ws.close(code=4001)
                    break
        finally:
            self.connections.discard(ws)
        return ws

    def _day_key(self, kind, target):
        # Explicit fixture boundary, NOT evidence of Tencent's actual boundary.
        return (kind, target, int((self.clock() - self.day_origin) // 86_400))

    def _quota_rejected(self, kind, target) -> bool:
        now = self.clock()
        times = [(at, mode, endpoint) for at, mode, endpoint in self._proactive_times if now - at < 60]
        self._proactive_times = times
        same = [(at, mode, endpoint) for at, mode, endpoint in times if mode == kind]
        endpoint = [at for at, _, key in same if key == target]
        return (self._proactive_day_counts.get(self._day_key(kind, target), 0) >= 1000
                or sum(now - at < 60 for at in endpoint) >= 20
                or (kind == "group" and sum(now - at < 60 for at, _, _ in same) >= 60)
                or (kind == "user" and sum(now - at < 1 for at, _, _ in same) >= 10))

    async def _send(self, request):
        if not self._authorized(request):
            return web.json_response({"code": FAKE_INVALID_PAYLOAD}, status=401)
        if self.next_errors:
            status, code = self.next_errors.popleft()
            return web.json_response({"code": code, "message": "TEST_REMOTE_ERROR"}, status=status)
        kind = "group" if request.match_info["collection"] == "groups" else "user"
        target = request.match_info["target"]
        body = await request.json()
        if not isinstance(body, dict) or body.get("msg_type") not in (0, 2):
            return web.json_response({"code": FAKE_INVALID_PAYLOAD})
        credential_kind = "msg_id" if body.get("msg_id") else "event_id" if body.get("event_id") else None
        if credential_kind:
            window = self.windows.get((kind, target, credential_kind, body[credential_kind]))
            if window is None or self.clock() >= window.expires_at:
                return web.json_response({"code": FAKE_UNKNOWN_WINDOW})
            seq = body.get("msg_seq")
            if type(seq) is not int or not 0 <= seq <= 65535:
                return web.json_response({"code": FAKE_INVALID_PAYLOAD})
            if seq in window.sequences:
                return web.json_response({"code": FAKE_DUPLICATE_SEQUENCE})
            if window.used >= window.max_replies:
                return web.json_response({"code": 40034128})
            window.sequences.add(seq)
            window.used += 1
        elif not self.proactive_allowed.get((kind, target), False):
            return web.json_response({"code": 40034105})
        elif self._quota_rejected(kind, target):
            return web.json_response({"code": 50002}, status=429, headers={"Retry-After": "60"})
        else:
            self._proactive_times.append((self.clock(), kind, target))
            day_key = self._day_key(kind, target)
            self._proactive_day_counts[day_key] = self._proactive_day_counts.get(day_key, 0) + 1
        message_id = f"TEST_SENT_{len(self.received) + 1}"
        receipt = {"id": message_id, "ext_info": {"ref_idx": f"TEST_REF_{len(self.received) + 1}"}}
        self.received.append({"kind": kind, "target": target, "payload": body, "receipt": receipt})
        if self.echo_messages:
            await self.emit("GROUP_MESSAGE_CREATE" if kind == "group" else "C2C_MESSAGE_CREATE", {
                "id": message_id, "group_openid": target if kind == "group" else None,
                "author": {"member_openid": "TEST_BOT", "user_openid": "TEST_BOT"},
                "content": body.get("content", ""),
                "timestamp": datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(),
            })
        if self.drop_next_response:
            self.drop_next_response = False
            if request.transport:
                request.transport.close()
            return web.Response(status=200)
        return web.json_response(receipt)
