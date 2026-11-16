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

from .billing_context import drain
from .responses_usage import ResponsesUsage
from .tool_images import ToolImage

MAX_BODY = 64 * 1024 * 1024
MAX_JOB_TOOL_BODY = 1024 * 1024
TOOL_HEARTBEAT_SECONDS = 25.0
IDLE_SECONDS = 120
FALLBACK_STATUSES = frozenset({401, 403, 404, 408, 409, 429, 500, 502, 503, 504, 529})
MODEL_400_MARKERS = ('not found', 'does not exist', 'unsupported', 'not supported')


def _should_fallback_status(status: int, body: bytes) -> bool:
    if status in FALLBACK_STATUSES:
        return True
    if status == 400:
        text = body.decode(errors='replace').lower()
        return 'model' in text and any(marker in text for marker in MODEL_400_MARKERS)
    return False


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
        self.session = None
        self.model_index = 0
        self.last_model = None
        self.stream_failures = 0
        self.fallbacks: list[tuple[str, str, str]] = []

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
    def __init__(self, gateway, handler=None, *, vm_only=False, job_only=False, write=False):
        self.gateway = gateway
        self.vm_only, self.job_only, self.write = vm_only, job_only, write
        self.handler = handler or gateway._responses
        self.model_request = handler is None

    async def __call__(self, scope, receive, send):
        request = Request(scope,receive)
        header = request.headers.get('authorization','')
        channel = self.gateway.channels.get(header[7:]) if header.startswith('Bearer ') and not self.vm_only else None
        if (channel is None and not self.model_request and not self.job_only and header.startswith('Bearer gvm_')
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
            receipt = getattr(request.state,'model_receipt',None)
            async def cleanup():
                try:
                    if upstream is not None:await upstream.aclose()
                finally:
                    try:await request.state.resources.aclose()
                    finally:
                        if receipt is not None:
                            observer = getattr(request.state,'model_usage',None)
                            if observer is not None:receipt.observe(observer.finish())
                            await receipt.finish()
            if receipt is not None:await drain(asyncio.create_task(cleanup()))
            else:await cleanup()


class _Server(uvicorn.Server):
    # The bot's own server owns the process signals.
    def capture_signals(self):
        return contextlib.nullcontext()


class CodexGateway:
    def __init__(self, *, port, upstream, model, logger, client=None, vm_authority=None, vm_handler=None, billing=None, tool_heartbeat_seconds=TOOL_HEARTBEAT_SECONDS):
        # upstream() -> ModelProfile; model() -> (model, effort). One snapshot
        # provides both the actual route/key and its optional price.
        self.port, self.upstream, self.model, self.logger = port, upstream, model, logger
        self.billing = billing
        self.tool_heartbeat_seconds = tool_heartbeat_seconds
        self.vm_authority,self.vm_handler = vm_authority,vm_handler
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
                                    Route('/v1/responses/compact', _ModelRequest(self), methods=['POST']),
                                    Route('/job/tools', _ModelRequest(self, self._job_tools, job_only=True), methods=['GET']),
                                    Route('/job/tool', _ModelRequest(self, self._job_tool, job_only=True), methods=['POST'])])
            if self.vm_handler is not None:
                for path,method,write in (('/vm/say','POST',True),('/vm/wake','POST','wake'),('/vm/send-file','POST',True),('/vm/actions/{action_id}','GET',False),('/vm/actions','GET',False),
                        ('/vm/events','GET',False),('/vm/units','POST',False),('/vm/history','POST',False),('/vm/web-search','POST',False),('/vm/web-fetch','POST',False),
                        ('/vm/commands','POST','command'),('/vm/commands','GET',False),('/vm/commands/{command_id}','GET',False),
                        ('/vm/commands/{command_id}/stop','POST',False)):
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

    def _model_chain(self) -> tuple[list[str], str]:
        first, effort = self.model()
        if isinstance(first, str):
            return [first], effort
        chain = [str(item) for item in first if str(item)]
        if not chain:
            raise RuntimeError('后台模型链为空。')
        return chain, effort

    async def _models(self, request: Request):
        if not self._authorized(request):
            return JSONResponse({'error': 'invalid job token'}, 401)
        chain, _ = self._model_chain()
        return JSONResponse({'object': 'list', 'data': [{'id': item, 'object': 'model'} for item in chain]})

    async def _responses(self, request: Request):
        if not self._authorized(request):
            return JSONResponse({'error': 'invalid job token'}, 401)
        body = await request.body()
        if len(body) > MAX_BODY:
            return JSONResponse({'error': 'request too large'}, 413)
        payload = json.loads(body)
        chain, effort = self._model_chain()
        token = request.headers['authorization'][7:]
        try:
            profile = self.upstream()
            base_url, key = profile.base_url, profile.api_key
        except Exception as exc:
            self.logger.exception('No upstream for background model calls')
            self.failures[token] = f'宿主没有可用的模型端点：{exc}'
            return JSONResponse({'error': self.failures[token]}, 503)
        channel = request.state.channel
        context = self.billing(channel) if self.billing is not None else None
        start_idx = min(channel.model_index, len(chain) - 1)
        last_reason = ''
        for idx in range(start_idx, len(chain)):
            if channel.current is not None:
                await channel.current()
            candidate = chain[idx]
            attempt_payload = dict(payload, model=candidate)
            if effort:
                attempt_payload['reasoning'] = {**(attempt_payload.get('reasoning') or {}), 'effort': effort}
            receipt = context.request(profile.with_model(candidate)) if context is not None else None
            # Publish ownership before awaiting start: a cancelled DB insert can
            # still commit, so the ASGI finally must be able to finish it.
            request.state.model_receipt = receipt
            if receipt is not None:
                await receipt.start()
            upstream = None
            try:
                upstream = await self.client.send(
                    self.client.build_request(
                        'POST',
                        base_url.rstrip('/') + request.url.path.removeprefix('/v1'),
                        json=attempt_payload,
                        headers={'Authorization': 'Bearer ' + key, 'Accept': request.headers.get('accept', '*/*')},
                    ),
                    stream=True,
                )
                request.state.model_upstream = upstream
                if upstream.status_code >= 400:
                    content = await upstream.aread()
                    should_fallback = _should_fallback_status(upstream.status_code, content)
                    if key:
                        content = content.replace(key.encode(), b'[REDACTED]')
            except (httpx.HTTPError, TimeoutError, OSError) as exc:
                if upstream is not None:
                    await upstream.aclose()
                    request.state.model_upstream = None
                if receipt is not None:
                    await receipt.finish()
                    request.state.model_receipt = None
                last_reason = f'{type(exc).__name__}: {exc}'
                if key:
                    last_reason = last_reason.replace(key, '[REDACTED]')
                self.logger.warning('Background model %s transport error: %s', candidate, last_reason)
                continue

            if upstream.status_code >= 400:
                await upstream.aclose()
                request.state.model_upstream = None
                if should_fallback:
                    if receipt is not None:
                        await receipt.finish()
                        request.state.model_receipt = None
                    last_reason = f'HTTP {upstream.status_code}: {content[:120].decode(errors="replace")}'
                    self.logger.warning('Background model %s failed (%s), falling back', candidate, upstream.status_code)
                    continue
                self.logger.warning('Background model request failed: %s %s', upstream.status_code, content[:300])
                self.failures[token] = f'模型端点返回 {upstream.status_code}：' + content[:200].decode(errors='replace')
                return Response(content, upstream.status_code, media_type=upstream.headers.get('content-type'))

            if idx > start_idx:
                channel.fallbacks.append((chain[start_idx], candidate, last_reason))
                channel.model_index = idx
                channel.stream_failures = 0

            channel.last_model = candidate
            request.state.model_receipt = receipt
            request.state.model_upstream = upstream
            observer = request.state.model_usage = ResponsesUsage(upstream.headers.get('content-type', ''))

            async def chunks():
                # Decoded bytes: Content-Encoding is not passed on.
                stream_failed = False
                try:
                    async for chunk in upstream.aiter_bytes():
                        observer.feed(chunk)
                        yield chunk
                except (httpx.HTTPError, TimeoutError, OSError):
                    stream_failed = True
                    raise
                finally:
                    if stream_failed:
                        if not channel.revoked:
                            channel.stream_failures += 1
                            if channel.stream_failures >= 2:
                                if idx + 1 < len(chain):
                                    channel.fallbacks.append((candidate, chain[idx + 1], '连续流中断'))
                                    channel.model_index = idx + 1
                                channel.stream_failures = 0
                    else:
                        channel.stream_failures = 0
                    await upstream.aclose()

            return StreamingResponse(chunks(), upstream.status_code, media_type=upstream.headers.get('content-type'))

        self.failures[token] = f'后台模型链全部不可用（最后原因：{last_reason}）'
        return JSONResponse({'error': self.failures[token]}, 503)

    async def _job_current(self, channel):
        try:
            if channel.current is not None:
                await channel.current()
            return True
        except asyncio.CancelledError:
            # Background job current() uses CancelledError to deny a stale
            # lease. Do not swallow a real client/token cancellation instead.
            task = asyncio.current_task()
            if task is None or task.cancelling():
                raise
            self.logger.warning('Background tool authority check denied a stale job')
            return False
        except Exception as exc:
            self.logger.warning('Background tool authority check failed: %s', type(exc).__name__)
            return False

    async def _job_tools(self, request: Request):
        channel = request.state.channel
        session = getattr(channel, 'session', None)
        if session is None:
            return JSONResponse({'error': 'job session unavailable'}, 404)
        if not await self._job_current(channel):
            return JSONResponse({'error': '后台任务已失权或授权检查不可用'}, 403)
        tools = [
            {
                'name': func['name'],
                'description': func.get('description', ''),
                'parameters': func.get('parameters', {}),
            }
            for item in session.tools
            for func in [item['function']]
        ]
        return JSONResponse({'tools': tools})

    async def _job_tool(self, request: Request):
        channel = request.state.channel
        session = getattr(channel, 'session', None)
        if session is None:
            return JSONResponse({'error': 'job session unavailable'}, 404)
        if not await self._job_current(channel):
            return JSONResponse({'error': '后台任务已失权或授权检查不可用'}, 403)
        chunks = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_JOB_TOOL_BODY:
                return JSONResponse({'error': 'request too large'}, 413)
            chunks.append(chunk)
        try:
            payload = json.loads(b''.join(chunks))
        except ValueError:
            return JSONResponse({'error': 'invalid json body'}, 400)
        if not isinstance(payload, dict) or not isinstance(payload.get('name'), str):
            return JSONResponse({'error': 'tool name required'}, 400)
        arguments = payload.get('arguments')
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return JSONResponse({'error': 'arguments must be an object'}, 400)
        name = payload['name']

        async def events():
            task = asyncio.create_task(session.execute_tool(name, arguments))
            try:
                while not task.done():
                    await asyncio.wait({task}, timeout=self.tool_heartbeat_seconds)
                    if not task.done():
                        yield b'{"type":"heartbeat"}\n'
                try:
                    raw = task.result()
                    error = None
                except asyncio.CancelledError:
                    owner = asyncio.current_task()
                    if channel.revoked or owner is None or owner.cancelling():
                        raise
                    error = RuntimeError('后台工具已取消')
                except Exception as exc:
                    error = exc
                # The original job can lose authority while a slow read or
                # ask is in flight, before its token is explicitly revoked.
                if not await self._job_current(channel):
                    error = PermissionError('后台任务已失权或授权检查不可用')
                if error is not None:
                    line = json.dumps(
                        {'type': 'result', 'ok': False, 'error': f'{type(error).__name__}: {error}'},
                        ensure_ascii=False,
                    )
                else:
                    if isinstance(raw, str):
                        content = [{'type': 'text', 'text': raw}]
                    elif isinstance(raw, ToolImage):
                        header_part, _, b64_data = raw.data_url.partition(',')
                        mime = header_part[5:].split(';', 1)[0] if header_part.startswith('data:') else 'image/png'
                        content = [
                            {'type': 'text', 'text': raw.text},
                            {'type': 'image', 'data': b64_data, 'mimeType': mime},
                        ]
                    else:
                        content = [{'type': 'text', 'text': json.dumps(raw, ensure_ascii=False)}]
                    line = json.dumps({'type': 'result', 'ok': True, 'content': content}, ensure_ascii=False)
                yield (line + '\n').encode('utf-8')
            finally:
                if not task.done():
                    task.cancel()
                    # A disconnect and token revocation can overlap; shield the
                    # same cancelled task until its socket/usage cleanup ends.
                    await drain(asyncio.gather(task, return_exceptions=True))

        return StreamingResponse(events(), media_type='application/x-ndjson')
