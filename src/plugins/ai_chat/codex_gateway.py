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
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
import uvicorn

MAX_BODY = 64 * 1024 * 1024
IDLE_SECONDS = 120


class RequestScope:
    """Host-authenticated identity and revocation for complete ASGI lifetimes."""

    def __init__(self, *, source=None, current=None, caller_ref=None, identity=None, generation=None):
        self.source = dict(source) if source is not None else None
        self.current = current
        self.identity,self.generation = identity,generation
        self.caller_ref = caller_ref or (f"job#{source['job_id']}" if source and 'job_id' in source else None)
        self.requests = set()
        self.revoked = False

    def close(self, *, status=401, reason='caller token revoked'):
        self.revoked = True
        self.denial = status,reason
        for task in tuple(self.requests):
            if not task.cancelling():
                task.cancel()


class Channel(RequestScope):
    """Only job scopes have an app-server TCP tunnel; VM scopes never do."""

    def __init__(self, *, source=None, current=None):
        super().__init__(source=source,current=current)
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
        super().close()
        if not self.connected.done():
            self.connected.cancel()
        elif not self.connected.cancelled() and self.writer is None:
            self.reader,self.writer = self.connected.result()
        if self.writer is not None:
            self.writer.close()


class _ModelRequest:
    """Own the whole ASGI response, including upstream headers and streaming.

    Revoking a job must cancel an already admitted model request, not merely
    reject the next request. The response's upstream socket closes even if
    cancellation occurs before the streaming generator starts.
    """
    def __init__(self, gateway, handler=None, *, vm_only=False, write=False):
        self.gateway = gateway
        self.vm_only,self.write = vm_only,write
        self.handler = handler or gateway._responses
        self.model_request = handler is None

    async def __call__(self, scope, receive, send):
        request = Request(scope,receive)
        header = request.headers.get('authorization','')
        channel = self.gateway.channels.get(header[7:]) if header.startswith('Bearer ') and not self.vm_only else None
        if (channel is None and not self.model_request and header.startswith('Bearer gvm_')
                and self.gateway.vm_authority is not None):
            try:
                channel = await self.gateway.vm_authority.authorize(header[7:],write=self.write)
            except asyncio.CancelledError:
                if not self.gateway.vm_authority.closed:
                    raise
                return await JSONResponse({'error':'vm_service_closed'},503)(scope,receive,send)
            except HTTPException as error:
                return await JSONResponse({'error':error.detail},error.status_code)(scope,receive,send)
            except PermissionError:
                return await JSONResponse({'error':'invalid vm token'},401)(scope,receive,send)
            except Exception:
                return await JSONResponse({'error':'vm_authorization_unavailable'},503)(scope,receive,send)
        if channel is None or channel.revoked:
            return await JSONResponse({'error':'invalid caller token'},401)(scope,receive,send)
        task = asyncio.current_task()
        channel.requests.add(task)
        request.state.channel = channel
        request.state.resources = contextlib.AsyncExitStack()
        started,finished = False,False
        async def tracked_send(message):
            nonlocal started,finished
            started |= message['type']=='http.response.start'
            finished |= message['type']=='http.response.body' and not message.get('more_body',False)
            await send(message)
        try:
            response = await self.handler(request)
            await response(scope,receive,tracked_send)
        except asyncio.CancelledError:
            if not channel.revoked:
                raise
            if not started:
                await JSONResponse({'error':channel.denial[1]},channel.denial[0])(scope,receive,send)
            elif not finished:
                if not self.model_request:
                    # A binary/JSON API has no SSE completion marker. Abort the
                    # connection: a clean chunked EOF would falsely mean success.
                    raise
                # No fabricated completion event; the interrupted stream ends.
                await send({'type':'http.response.body','body':b'','more_body':False})
        finally:
            channel.requests.discard(task)
            upstream = getattr(request.state,'model_upstream',None)
            try:
                if upstream is not None:
                    await upstream.aclose()
            finally:
                await request.state.resources.aclose()


class _Server(uvicorn.Server):
    # The bot's own server owns the process signals.
    def capture_signals(self):
        return contextlib.nullcontext()


class CodexGateway:
    def __init__(self, *, port, upstream, model, logger, client=None, api_handler=None, vm_authority=None, vm_handler=None):
        # upstream() -> (base_url, api_key); model() -> (model, effort)
        self.port, self.upstream, self.model, self.logger = port, upstream, model, logger
        self.api_handler,self.vm_authority,self.vm_handler = api_handler,vm_authority,vm_handler
        self.channels: dict[str, Channel] = {}
        self.failures: dict[str, str] = {}
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(30, read=IDLE_SECONDS), trust_env=False)
        self.http = self.rpc = None
        self.started = asyncio.Lock()

    def open(self, *, source=None, current=None):
        token = secrets.token_urlsafe(32)
        self.channels[token] = Channel(source=source,current=current)
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
                                    Route('/v1/responses', _ModelRequest(self), methods=['POST']),
                                    Route('/v1/responses/compact', _ModelRequest(self), methods=['POST'])])
            if self.api_handler is not None:
                from .host_apis import METHODS
                for path in ('/api/{name}','/api/{name}/{path:path}'):
                    app.router.routes.append(Route(path,_ModelRequest(self,self.api_handler),methods=sorted(METHODS)))
            if self.vm_handler is not None:
                for path,method,write in (('/vm/say','POST',True),('/vm/actions/{action_id}','GET',False),('/vm/actions','GET',False)):
                    app.router.routes.append(Route(path,_ModelRequest(self,self.vm_handler,vm_only=True,write=write),methods=[method]))
            self.http = _Server(uvicorn.Config(app, host='127.0.0.1', port=self.port, log_level='warning',
                                               access_log=False, lifespan='off'))
            self.http_task = asyncio.create_task(self.http.serve())
            while not self.http.started:
                if self.http_task.done():
                    self.http_task.result()
                await asyncio.sleep(0.05)

    async def close(self):
        if self.vm_authority is not None:
            await self.vm_authority.close()
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
        request.state.model_upstream = upstream
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
