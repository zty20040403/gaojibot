"""Host-authenticated API forwarding on the existing relay (not MCP)."""
import asyncio
import json

from starlette.responses import JSONResponse, StreamingResponse

from .host_apis import ApiDenied,canonical_path
from .host_api_transport import MAX_BODY,forward
from .platform_types import EndpointKey


def api_instructions(apis, port):
    if not apis:
        return ''
    entries = [dict(name=api.name,url=f'http://10.0.2.2:{port}/api/{api.name}',
        allow=api.allow,per_hour=api.per_hour,about=api.about) for api in apis.values()]
    github = ('\nGitHub：只在用户明确要求时开 PR；描述末尾单独写一行 '
        'This pull request was opened by an AI at a group member\'s request. '
        '不要批量骚扰或发垃圾内容。用 git config --system 读取公开提交身份；'
        '禁止 gh auth login 或在 VM 保存 GitHub token。Git 推送到 github-git 中转，'
        '用 git -c credential.helper= -c http.followRedirects=false '
        '-c "http.extraHeader=Authorization: Bearer $GAOJI_JOB_TOKEN" push <中转仓库URL> <分支>。'
        '不要在 URL 或永久 git 配置中保存任务令牌。') if {'github','github-git'}<=apis.keys() else ''
    return ('\n宿主 API（只在任务存活时可用）：用 curl/fetch 请求下面的地址，认证头为 '
        '`Authorization: Bearer $GAOJI_JOB_TOKEN`（展开环境变量，不是上游密钥）。'
        '只按用户要求调用；HTTP 失败要说明，超时或中断可能已在外部生效，不自动重试写操作。'
        'allow 是方法和路径段前缀；per_hour 按本会话整点小时计算，0 不设本地上限，失败也占次数。'
        '不得尝试获取宿主密钥。\n'+json.dumps(entries,ensure_ascii=False)+github)


class HostAPIProxy:
    def __init__(self, apis, store, *, calls, transport=forward):
        self.apis,self.store,self.calls,self.transport = apis,store,calls,transport
        self.capacity = asyncio.Semaphore(4)

    async def handle(self, request):
        channel = request.state.channel
        source = channel.source
        if source is None or channel.current is None or not channel.caller_ref:
            return JSONResponse({'error':'api_caller_unavailable'},403)
        endpoint = EndpointKey.parse(source['endpoint_key'])
        name = request.path_params['name']
        api = self.apis.get(name)
        path,raw_path,rejection = '<invalid>',b'/',None

        async def require():
            if channel.revoked:
                raise PermissionError
            try:
                await channel.current()
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                raise PermissionError from None

        try:
            await require()
            raw = request.scope['raw_path']
            prefix = ('/api/'+name).encode('ascii')
            if not raw.startswith(prefix) or (raw[len(prefix):] and not raw[len(prefix):].startswith(b'/')):
                raise ApiDenied('invalid_api_path')
            raw_path = raw[len(prefix):] or b'/'
            path = canonical_path(raw_path)
            if api is None:
                rejection = (404,'api_unavailable')
            elif not api.permits(request.method,path):
                rejection = (403,'api_operation_denied')
        except (ApiDenied,UnicodeError):
            rejection = (400,'invalid_api_path')
        except PermissionError:
            rejection = (403,'api_caller_revoked')
        except Exception:
            return JSONResponse({'error':'api_authorization_unavailable'},503)
        try:
            admitted = await self.calls(self.store.begin,scope_key=endpoint.scope.key,api_name=name,
                caller_ref=channel.caller_ref,method=request.method,path=path,
                limit=api.per_hour if api else 0,rejection=rejection)
        except Exception:
            return JSONResponse({'error':'api_audit_unavailable'},503)
        if admitted['status_code']:
            return JSONResponse({'error':admitted['error_code']},admitted['status_code'])
        result = dict(status_code=0,outcome='interrupted',error_code='api_request_interrupted')
        async def finish():
            await self.calls(self.store.finish,admitted['call_id'],**result)
        request.state.resources.push_async_callback(finish)
        try:
            async with asyncio.timeout(10):
                await self.capacity.acquire()
            request.state.resources.callback(self.capacity.release)
            await require()
            body = bytearray()
            async with asyncio.timeout(60):
                async for chunk in request.stream():
                    if len(body)+len(chunk)>MAX_BODY:
                        raise ApiDenied('api_request_too_large')
                    body.extend(chunk)
            await require()
            upstream = await request.state.resources.enter_async_context(self.transport(api,method=request.method,
                raw_path=raw_path,query=request.scope['query_string'],headers=request.headers.raw,body=bytes(body),
                current=require))
            result['status_code'] = upstream.status_code
            await require()
            async def chunks():
                try:
                    async for chunk in upstream.chunks():
                        await require()
                        yield chunk
                    result.update(outcome='completed',error_code='')
                except asyncio.CancelledError:
                    raise
                except Exception:
                    result['error_code'] = 'api_stream_interrupted'
                    raise ApiDenied('api_stream_interrupted') from None
            response = StreamingResponse(chunks(),upstream.status_code)
            response.raw_headers = [(key.encode('ascii'),value.encode('latin-1')) for key,value in upstream.headers]
            return response
        except PermissionError:
            status,error = 403,'api_caller_revoked'
        except ApiDenied as exc:
            error = str(exc)
            status = (413 if error=='api_request_too_large' else 403 if error=='api_caller_revoked'
                else 400 if error=='invalid_api_request' else 503)
        except Exception:
            status,error = 503,'api_unavailable'
        result.update(status_code=status,outcome='failed',error_code=error)
        return JSONResponse({'error':error},status)
