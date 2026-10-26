from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Optional

from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse

from src.bot_security.store import SecurityError

from ..admin_security import SESSION_COOKIE
from .context import AdminRouteContext, authorize


def create_router(context: AdminRouteContext) -> APIRouter:
    router = context.new_router()
    mobile = context.services.mobile_authorization
    control_store = context.control_store
    event_broker = context.event_broker
    realtime_monitor = context.realtime_monitor

    @router.get("/api/events")
    async def events(
        request: Request,
        authorization: Optional[str] = Header(default=None),
    ) -> StreamingResponse:
        authorize(authorization)

        async def stream() -> AsyncIterator[str]:
            queue = event_broker.subscribe()
            realtime_monitor.start()
            ready = {
                "sequence": 0,
                "type": "ready",
                "resources": [],
                "timestamp": int(time.time()),
                "versions": control_store.versions(),
            }
            try:
                yield "retry: 2000\n"
                yield f"data: {json.dumps(ready, separators=(',', ':'))}\n\n"
                while not await request.is_disconnected():
                    try:
                        if mobile is None:
                            break
                        active = await asyncio.to_thread(mobile.store.session, request.cookies.get(SESSION_COOKIE, ""))
                        if active["role"] != "admin":
                            break
                    except SecurityError:
                        break
                    try:
                        payload = await asyncio.wait_for(
                            queue.get(),
                            timeout=15.0,
                        )
                    except TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    encoded = json.dumps(payload, separators=(",", ":"))
                    yield f"data: {encoded}\n\n"
            finally:
                event_broker.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "close",
                "X-Accel-Buffering": "no",
            },
        )

    return router
