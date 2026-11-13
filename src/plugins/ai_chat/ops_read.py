"""Bounded host-only GET calls for a verified owner's live private task."""
import asyncio
from contextlib import suppress
import json
import re
from urllib.parse import quote, urlsplit

import httpx

from src.qq_official.credentials import _read_secret
from .tools.background import definition


MAX_RESPONSE = 1024 * 1024
DEADLINE = 20
ACTIONS = ('overview', 'services', 'service', 'logs', 'disk', 'resources', 'version')
TOOL = definition('ops_read', '只读查看概况、服务列表(services)/状态(service)、日志、磁盘/内存和当前部署依据(version)。version返回实测系统closure/配置revision，缺失字段未知，不能当作应用Git版本；旧缓存不能说是实时状态。只供不共享历史的owner私聊任务，结果不转发到群或其他会话，不执行写操作。',
    {'action': {'type': 'string', 'enum': list(ACTIONS)},
     'host': {'type': 'string'}, 'unit': {'type': 'string'},
     'lines': {'type': 'integer', 'minimum': 1, 'maximum': 200},
     'since_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 86400}}, ('action',))


class OpsReadError(RuntimeError):
    """Only fixed diagnostics; no configured addresses, credentials or raw errors."""


def owner_allowed(app, inbound):
    if inbound.endpoint.kind != 'private' or not app.settings.ops_read_base_url or not app.settings.ops_read_token_file:
        return False
    # Job reports are persisted in their canonical history. Never admit sensitive
    # observations to a private task whose history is shared with another scope.
    if getattr(inbound,'canonical_scope',None) not in (None,inbound.endpoint.scope):
        return False
    runtime = app.official_runtime
    if runtime is None or runtime.store.authority is None:
        return False
    with runtime.store.transaction() as cursor:
        return runtime.store.authority.owner_for_actor(cursor, inbound.endpoint, inbound.actor) is not None


def request_path(args):
    if type(args) is not dict or args.keys()-{'action','host','unit','lines','since_seconds'}:
        raise ValueError('不支持此运维读取参数。')
    action, host, unit = args.get('action'), args.get('host',''), args.get('unit','')
    if type(action) is not str or action not in ACTIONS:
        raise ValueError('不支持此运维读取操作。')
    if type(host) is not str or (host and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',host) is None):
        raise ValueError('host 必须是配置中的主机标识，不是 URL 或路径。')
    if action!='overview' and not host:
        raise ValueError('请指定 host。')
    if type(unit) is not str or (unit and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@:-]{0,119}\.service',unit) is None):
        raise ValueError('unit 必须是有效的 .service 名称。')
    if action in {'service','logs'} and not unit:
        raise ValueError('请指定 unit。')
    if (unit and action not in {'service','logs'}) or (action!='logs' and {'lines','since_seconds'} & args.keys()):
        raise ValueError('此操作不接受 unit 或日志范围参数。')
    params = {}
    if action=='logs':
        for name,default,maximum in (('lines',50,200),('since_seconds',3600,86400)):
            value = args.get(name,default)
            if type(value) is not int or not 1<=value<=maximum:
                raise ValueError('日志行数或时间范围超出限制。')
            params[name] = value
    host_path = '/v1/hosts/'+quote(host,safe='')
    return {'overview':host_path if host else '/v1/fleet', 'service':host_path+'/units/'+quote(unit,safe=''),
        'logs':host_path+'/units/'+quote(unit,safe='')+'/logs',
        'services':host_path+'/units','version':host_path,
        'disk':host_path+'/metrics','resources':host_path+'/metrics'}[action],params


def connection(settings):
    try:
        base = settings.ops_read_base_url.rstrip('/')
        parsed = urlsplit(base)
        if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or re.search(r'[\s\\]',base) or parsed.port==0
                or (parsed.scheme!='https' and not (parsed.scheme=='http' and parsed.hostname in {'127.0.0.1','::1'}))):
            raise ValueError
        token = _read_secret(settings.ops_read_token_file)
        if not 1<=len(token)<=16384 or re.fullmatch(r'[A-Za-z0-9._~+/-]+=*',token) is None:
            raise ValueError
        return base,token,parsed.scheme+'://'+parsed.netloc
    except (ValueError,TypeError,AttributeError,OSError):
        raise OpsReadError('运维连接配置或私有凭据不可用。') from None


async def read(app, inbound, args, *, current):
    runtime = app.official_runtime
    async def authorized():
        # current binds the original durable job, message, lease, route and stop
        # generation. A VM/program identity cannot provide this host callback.
        await current()
        if app.official_runtime is not runtime:
            raise PermissionError('运维读取的原任务运行时已改变。')
        allowed = await app.db_call(owner_allowed,app,inbound)
        if not allowed or app.official_runtime is not runtime:
            raise PermissionError('运维读取只供仍获授权的 owner 私聊任务使用。')
    await authorized()
    path,params = request_path(args)
    base,token,origin = await asyncio.to_thread(connection,app.settings)

    async def request():
        await authorized()
        async with httpx.AsyncClient(timeout=DEADLINE,trust_env=False,follow_redirects=False) as client:
            async with client.stream('GET',base+path,headers={'Authorization':'Bearer '+token,
                    'Accept':'application/json','Accept-Encoding':'identity'},params=params) as response:
                if not 200<=response.status_code<300:
                    raise OpsReadError(f'运维读取未成功（HTTP {response.status_code}），未跟随跳转。')
                if response.headers.get('content-encoding','identity').lower()!='identity':
                    raise OpsReadError('运维响应使用了不支持的压缩格式。')
                raw = bytearray()
                async for chunk in response.aiter_raw():
                    if len(chunk)>MAX_RESPONSE-len(raw):
                        raise OpsReadError('运维响应超过 1 MiB；请缩小读取范围。')
                    raw.extend(chunk)
                try:
                    value = json.loads(raw)
                    if type(value) is not dict:
                        raise ValueError
                    result = json.dumps(value,ensure_ascii=False,allow_nan=False)
                except (ValueError,RecursionError):
                    raise OpsReadError('运维服务未返回有效的 JSON 对象。') from None
                for secret in (token,base,origin):
                    result = result.replace(secret,'[redacted]')
                return result
    pending = asyncio.create_task(request())
    try:
        async with asyncio.timeout(DEADLINE):
            while not pending.done():
                await asyncio.wait({pending},timeout=0.25)
                await authorized()
            result = await pending
            await authorized()
            return result
    except (TimeoutError,httpx.HTTPError):
        raise OpsReadError('运维读取超时或连接失败；未自动重试。') from None
    finally:
        pending.cancel()
        with suppress(BaseException):
            await pending
