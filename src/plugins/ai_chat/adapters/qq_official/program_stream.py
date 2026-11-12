"""Bounded NDJSON subscriptions backed by committed ledger cursors, not RAM."""
import asyncio
import json
import re
import time

from starlette.responses import JSONResponse, StreamingResponse

from .program_events import ProgramEvents

HEARTBEAT_SECONDS, POLL_SECONDS = 30, 0.5


class ProgramStream:
    def __init__(self, sessions, calls, logger):
        self.events,self.calls,self.logger = ProgramEvents(sessions),calls,logger
        self.active = {}

    def release(self, endpoint):
        remaining = self.active[endpoint]-1
        if remaining:
            self.active[endpoint] = remaining
        else:
            self.active.pop(endpoint)

    async def handle(self, request):
        scope = request.state.channel
        params = request.query_params
        after = params.get('after')
        if (set(params)-{'after'} or len(params.getlist('after'))>1 or after is not None
                and (re.fullmatch(r'[0-9]{1,19}',after) is None or int(after)>=2**63)):
            return JSONResponse({'error':'invalid_program_event_cursor'},400)
        endpoint = scope.identity.endpoint.key
        count = self.active.get(endpoint,0)
        if count>=2 or sum(self.active.values())>=16:
            return JSONResponse({'error':'program_event_streams_busy'},429)
        self.active[endpoint] = count+1
        # ASGI owns this permit even if disconnect happens before the generator
        # starts; a generator-only finally would leak that admission slot.
        request.state.resources.callback(self.release,endpoint)
        try:
            head = await self.calls(self.events.head,scope.identity)
            cursor = head if after is None else int(after)
            if cursor>head:
                return JSONResponse({'error':'program_event_cursor_ahead'},409)
            await scope.current()
            return StreamingResponse(self.stream(scope,cursor),media_type='application/x-ndjson',
                headers={'X-Gaoji-Head':str(head),'Cache-Control':'no-store','X-Accel-Buffering':'no'})
        except PermissionError:
            return JSONResponse({'error':'vm_source_revoked'},403)
        except Exception as error:
            self.logger.warning('Program event subscription failed error=%s',type(error).__name__)
            return JSONResponse({'error':'program_events_unavailable'},503)

    async def stream(self, scope, cursor):
        last = time.monotonic()
        while True:
            rows = await self.calls(self.events.page,scope.identity,cursor)
            await scope.current()
            if rows:
                yield ''.join(json.dumps(row,ensure_ascii=False,separators=(',',':'))+'\n' for row in rows).encode()
                cursor = rows[-1]['id']
                last = time.monotonic()
                continue
            if time.monotonic()-last>=HEARTBEAT_SECONDS:
                yield b'\n'
                last = time.monotonic()
            await asyncio.sleep(POLL_SECONDS)
