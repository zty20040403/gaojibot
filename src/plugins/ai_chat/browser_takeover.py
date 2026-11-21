"""One-use conversation browser viewer. The browser client never gets a CDP connection."""
import asyncio
from contextlib import asynccontextmanager
from itertools import count
from pathlib import Path
import secrets
from urllib.parse import quote

import aiohttp
from fastapi import WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from .sandbox import SandboxError


class TakeoverLinks:
    def __init__(self):
        self.pending = {}

    def issue(self, owner, sid):
        loop = asyncio.get_running_loop()
        token = secrets.token_urlsafe(32)
        deadline = loop.time() + 600
        self.pending[token] = (owner, sid, deadline)
        loop.call_at(deadline, self.pending.pop, token, None)
        return token

    def claim(self, token):
        entry = self.pending.pop(token, None)
        if entry and asyncio.get_running_loop().time() < entry[2]:
            return entry


@asynccontextmanager
async def current_tab(manager, owner, sid):
    socket = Path(manager.broker).with_name(f'cdp-{sid}.sock')
    async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(socket)),
                                     timeout=aiohttp.ClientTimeout(total=10)) as client:
        async with manager.io_lock(sid):
            await manager._owned_running(owner, sid)
            async with client.get('http://localhost/json/list', allow_redirects=False) as response:
                response.raise_for_status()
                targets = await response.json()
            tab = next((t['id'] for t in targets if t['type'] == 'page'), None)
            if not tab:
                raise ValueError('no_current_tab')
            cdp = await client.ws_connect('ws://localhost/devtools/page/' + quote(tab, safe=''))
        async with cdp:
            yield cdp


def input_command(event):
    kind = event['type']
    if kind in ('mousePressed', 'mouseMoved', 'mouseReleased'):
        return 'Input.dispatchMouseEvent', dict(type=kind, x=event['x'], y=event['y'],
            button='left', buttons=int(kind != 'mouseReleased'), clickCount=int(kind != 'mouseMoved'))
    if kind == 'wheel':
        return 'Input.dispatchMouseEvent', dict(type='mouseWheel', x=event['x'], y=event['y'],
                                                deltaX=event['dx'], deltaY=event['dy'])
    if kind == 'text':
        return 'Input.insertText', {'text': event['text']}
    if kind == 'ack':
        return 'Page.screencastFrameAck', {'sessionId': event['id']}
    if kind in ('keyDown', 'keyUp'):
        key = event['key']
        return 'Input.dispatchKeyEvent', dict(type=kind, key=key, windowsVirtualKeyCode=event['keyCode'],
                                               text='\r' if kind == 'keyDown' and key == 'Enter' else '')
    raise ValueError('unknown_input')


async def mirror(viewer, cdp, width, height):
    ids = count(1)

    async def send(method, params):
        await cdp.send_json({'id': next(ids), 'method': method, 'params': params})

    async def frames():
        async for msg in cdp:
            data = msg.json()
            if 'error' in data:
                raise ValueError('cdp_command_failed')
            if data.get('method') == 'Page.screencastFrame':
                frame = data['params']
                # The viewer acks on arrival, so a slow phone link holds one frame, not a backlog.
                await viewer.send_json({'image': frame['data'], 'viewport': frame['metadata'], 'id': frame['sessionId']})
        raise ValueError('tab_disconnected')

    async def input_events():
        while True:
            event = await viewer.receive_json()
            if event == {'type': 'done'}:
                return
            await send(*input_command(event))

    # Lay the page out at the viewer's size so a phone shows it 1:1; Chrome drops the
    # override when this CDP connection closes.
    await send('Emulation.setDeviceMetricsOverride', {'width': width, 'height': height,
                                                      'deviceScaleFactor': 0, 'mobile': False})
    await send('Page.startScreencast', {'format': 'jpeg', 'quality': 75})
    tasks = [asyncio.create_task(pump()) for pump in (frames, input_events)]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def register_takeover(app, context):
    @app.get('/browser-takeover', include_in_schema=False)
    async def viewer():
        return FileResponse(Path(__file__).with_suffix('.html'), headers={
            'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': "default-src 'none'; img-src data:; script-src 'unsafe-inline'; "
                                       f"style-src 'unsafe-inline'; connect-src {context.settings.admin_origin.replace('https:', 'wss:', 1)}; "
                                       "frame-ancestors 'none'",
        })

    @app.websocket('/browser-takeover/live')
    async def live(ws: WebSocket):
        if ws.headers.get('origin') != context.settings.admin_origin:
            await ws.close(code=1008)
            return
        await ws.accept()
        try:
            async with asyncio.timeout(5):
                hello = await ws.receive_json()
                entry = context.browser_takeovers.claim(hello['token'])
            if not entry:
                await ws.close(code=4403, reason='链接已使用或失效')
                return
            owner, sid, deadline = entry
            async with asyncio.timeout_at(deadline):
                async with current_tab(context.sandbox_manager, owner, sid) as cdp:
                    await mirror(ws, cdp, int(hello['width']), int(hello['height']))
            await ws.close()
        except (WebSocketDisconnect, RuntimeError):  # RuntimeError: sending after the viewer left.
            return
        except TimeoutError:
            await ws.close(code=4408, reason='接管连接超时')
        except (aiohttp.ClientError, OSError, SandboxError, ValueError, TypeError, KeyError):
            await ws.close(code=1011, reason='浏览器连接或操作失败，请回原会话核对页面')
