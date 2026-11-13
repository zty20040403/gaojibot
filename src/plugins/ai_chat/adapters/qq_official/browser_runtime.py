"""Physical-conversation browser access; guest values never enter host responses."""
import asyncio
import base64
import json
from pathlib import Path

from ...application.commands import CommandResult
from ...background_tools import workspace_owner
from ... import sandbox_browser_state as guest


class BrowserUnavailable(RuntimeError):
    pass


class BrowserRuntime:
    def __init__(self, lifecycle):
        self.sessions,self.manager,self.calls = lifecycle.store,lifecycle.manager,lifecycle.calls
        self.active = set()

    def check(self, endpoint, authorize, expected=None):
        with self.sessions.transaction() as cursor:
            route = self.sessions._route(cursor,endpoint)
            authorize(cursor)
            current = (route['generation'],route['route_epoch'])
            if expected is not None and current!=expected:
                raise PermissionError('browser_route_changed')
            return current

    async def snapshot(self, endpoint, *, authorize):
        if endpoint.key in self.active or len(self.active)>=4:
            raise BrowserUnavailable('browser_busy')
        self.active.add(endpoint.key)
        try:
            async with asyncio.timeout(20):
                route = await self.calls(self.check,endpoint,authorize)
                result = await self.inspect(endpoint,authorize,route)
                await self.calls(self.check,endpoint,authorize,route)
                return result
        except (PermissionError,BrowserUnavailable):
            raise
        except Exception:
            # Guest stderr and exception strings may contain browser secrets.
            raise BrowserUnavailable('browser_unavailable') from None
        finally:
            self.active.discard(endpoint.key)

    async def inspect(self, endpoint, authorize, route):
        owner = workspace_owner(endpoint)
        items = await self.manager.list(owner)
        if not items:
            return {'state':'missing','domains':[],'truncated':False}
        # Same first owned VM that ensure_default() chooses, without boot/create.
        sid = items[0]['sandbox_id']
        async with self.manager.io_lock(sid):
            await self.manager._metadata(owner,sid)
            await self.calls(self.check,endpoint,authorize,route)
            if not await self.manager._running(sid):
                return {'state':'offline','sandbox_id':sid,'domains':[],'truncated':False}
            # Send the fixed host payload, not a guest-modifiable script path.
            payload = base64.b64encode(Path(guest.__file__).read_bytes()).decode('ascii')
            script = "import base64;exec(compile(base64.b64decode("+repr(payload)+"),'<browser-state>','exec'))"
            pending = asyncio.create_task(self.manager._guest_exec(sid,'/usr/bin/python3',['-c',script,'domains'],timeout=10))
            try:
                while not pending.done():
                    await self.calls(self.check,endpoint,authorize,route)
                    await asyncio.wait((pending,),timeout=.25)
                code,content,_ = await pending
            finally:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending,return_exceptions=True)
            await self.manager._metadata(owner,sid)
        if code or type(content) is not bytes or len(content)>65536:
            raise BrowserUnavailable('browser_read_failed')
        result = json.loads(content)
        if (type(result) is not dict or set(result)!={'domains','truncated'} or type(result['truncated']) is not bool
                or type(result['domains']) is not list or len(result['domains'])>guest.DOMAIN_LIMIT
                or any(type(name) is not str or guest.domain_name(name)!=name for name in result['domains'])
                or result['domains']!=sorted(set(result['domains']))):
            raise BrowserUnavailable('browser_result_invalid')
        return dict(result,state='ready',sandbox_id=sid)

    async def command(self, inbound, request, *, authorize):
        if request.name!='logins' or request.argument:
            return CommandResult('syntax','用法：/logins')
        try:
            result = await self.snapshot(inbound.endpoint,authorize=authorize)
        except PermissionError:
            return CommandResult('denied','会话或权限已改变，未返回浏览器状态。')
        except BrowserUnavailable:
            return CommandResult('unavailable','浏览器状态暂时不可用；VM可能忙碌或客体代理不可达，不能确认登录状态。')
        if result['state']=='missing':
            return CommandResult('ok','本会话还没有 VM；没有可查询的浏览器资料。')
        if result['state']=='offline':
            return CommandResult('unavailable','本会话 VM 已离线；未擅自开机，请启动后再用 /logins 查询。')
        text = '本会话浏览器 Cookie 域名（不保证登录仍有效）：\n'
        shown = []
        for domain in result['domains']:
            if sum(len(name)+1 for name in shown)+len(domain)>5500:
                break
            shown.append(domain)
        text += '\n'.join(shown) if shown else '没有未过期 Cookie 记录；不代表所有网站均未登录。'
        if result['truncated'] or len(shown)<len(result['domains']):
            text += '\n结果已截断（最多200个域名及5500字）。'
        return CommandResult('ok',text)
