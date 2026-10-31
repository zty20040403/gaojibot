"""Host side of a background Codex run: the model relay and the app-server tunnel.

The VM reaches both on the host's loopback (10.0.2.2 under QEMU user networking).
Each job gets a token that only works while the job runs; the provider key, the
model and the reasoning effort are the host's, never the VM's.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import secrets

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
import uvicorn

MAX_BODY = 64 * 1024 * 1024
IDLE_SECONDS = 120


class Channel:
    """One job's app-server stdio, carried over a TCP connection from the VM."""

    def __init__(self):
        self.connected = asyncio.get_running_loop().create_future()
        self.reader = self.writer = None

    async def wait(self, timeout):
        self.reader, self.writer = await asyncio.wait_for(asyncio.shield(self.connected), timeout)

    async def send(self, message):
        self.writer.write(json.dumps(message, ensure_ascii=False).encode() + b'\n')
        await self.writer.drain()

    async def receive(self):
        line = await self.reader.readline()
        if not line:
            raise EOFError('Codex 进程已退出。')
        return json.loads(line)

    def close(self):
        if not self.connected.done():
            self.connected.cancel()
        if self.writer is not None:
            self.writer.close()


class _Server(uvicorn.Server):
    # The bot's own server owns the process signals.
    def capture_signals(self):
        return contextlib.nullcontext()


class CodexGateway:
    def __init__(self, *, port, upstream, model, logger, client=None):
        # upstream() -> (base_url, api_key); model() -> (model, effort)
        self.port, self.upstream, self.model, self.logger = port, upstream, model, logger
        self.channels: dict[str, Channel] = {}
        self.failures: dict[str, str] = {}
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(30, read=IDLE_SECONDS), trust_env=False)
        self.http = self.rpc = None
        self.started = asyncio.Lock()

    def open(self):
        token = secrets.token_urlsafe(32)
        self.channels[token] = Channel()
        return token, self.channels[token]

    def failure(self, token):
        return self.failures.get(token, '')

    def revoke(self, token):
        self.failures.pop(token, None)
        channel = self.channels.pop(token, None)
        if channel is not None:
            channel.close()

    async def start(self):
        async with self.started:
            if self.rpc is not None:
                return
            self.rpc = await asyncio.start_server(self._tunnel, '127.0.0.1', self.port + 1, limit=2**24)
            app = Starlette(routes=[Route('/v1/models', self._models),
                                    Route('/v1/responses', self._responses, methods=['POST']),
                                    Route('/v1/responses/compact', self._responses, methods=['POST'])])
            self.http = _Server(uvicorn.Config(app, host='127.0.0.1', port=self.port, log_level='warning',
                                               access_log=False, lifespan='off'))
            self.http_task = asyncio.create_task(self.http.serve())
            while not self.http.started:
                if self.http_task.done():
                    self.http_task.result()
                await asyncio.sleep(0.05)

    async def close(self):
        for token in tuple(self.channels):
            self.revoke(token)
        if self.rpc is not None:
            self.rpc.close()
            self.http.should_exit = True
            await asyncio.gather(self.http_task, return_exceptions=True)
        await self.client.aclose()

    async def _tunnel(self, reader, writer):
        try:
            token = (await asyncio.wait_for(reader.readline(), 10)).decode().strip()
        except (TimeoutError, UnicodeDecodeError):
            token = ''
        channel = self.channels.get(token)
        if channel is None or channel.connected.done():
            writer.close()
            return
        channel.connected.set_result((reader, writer))

    def _authorized(self, request: Request):
        header = request.headers.get('authorization', '')
        return header.startswith('Bearer ') and header[7:] in self.channels

    async def _models(self, request: Request):
        if not self._authorized(request):
            return JSONResponse({'error': 'invalid job token'}, 401)
        return JSONResponse({'object': 'list', 'data': [{'id': self.model()[0], 'object': 'model'}]})

    async def _responses(self, request: Request):
        if not self._authorized(request):
            return JSONResponse({'error': 'invalid job token'}, 401)
        body = await request.body()
        if len(body) > MAX_BODY:
            return JSONResponse({'error': 'request too large'}, 413)
        payload = json.loads(body)
        model, effort = self.model()
        payload['model'] = model
        if effort:
            payload['reasoning'] = {**(payload.get('reasoning') or {}), 'effort': effort}
        token = request.headers['authorization'][7:]
        try:
            base_url, key = self.upstream()
        except Exception as exc:
            self.logger.exception('No upstream for background model calls')
            self.failures[token] = f'宿主没有可用的模型端点：{exc}'
            return JSONResponse({'error': self.failures[token]}, 503)
        upstream = await self.client.send(self.client.build_request(
            'POST', base_url.rstrip('/') + request.url.path.removeprefix('/v1'),
            json=payload, headers={'Authorization': 'Bearer ' + key, 'Accept': request.headers.get('accept', '*/*')}),
            stream=True)
        if upstream.status_code >= 400:
            content = await upstream.aread()
            await upstream.aclose()
            self.logger.warning('Background model request failed: %s %s', upstream.status_code, content[:300])
            self.failures[token] = (f'模型端点返回 {upstream.status_code}：'
                                    + content[:200].decode(errors='replace'))
            return Response(content, upstream.status_code, media_type=upstream.headers.get('content-type'))

        async def chunks():
            # Decoded bytes: Content-Encoding is not passed on. A provider silent for
            # IDLE_SECONDS fails the call, and Codex retries it.
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(chunks(), upstream.status_code, media_type=upstream.headers.get('content-type'))
