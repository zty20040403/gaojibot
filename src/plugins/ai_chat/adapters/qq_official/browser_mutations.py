"""Host driver for once-only browser actions; native commands already checkpoint execution."""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
from uuid import UUID

from ... import sandbox_browser,sandbox_browser_state,sandbox_browser_actions
from ...background_tools import workspace_owner
from .browser_runtime import BrowserUnavailable


class BrowserActionUnknown(BrowserUnavailable):
    pass


def script(request):
    lines = ['import asyncio,base64,json,sys,types',
        'def load(name,payload):',
        ' m=types.ModuleType(name);sys.modules[name]=m;exec(compile(base64.b64decode(payload),name,"exec"),m.__dict__);return m']
    for name,module in (('gaoji_browser',sandbox_browser),('state',sandbox_browser_state),('actions',sandbox_browser_actions)):
        payload = base64.b64encode(Path(module.__file__).read_bytes()).decode()
        lines.append(f'{name}=load({name!r},{payload!r})')
    encoded = base64.b64encode(json.dumps(request,separators=(',',':')).encode()).decode()
    lines += [f'request=json.loads(base64.b64decode({encoded!r}))',
        'async def perform(value):',
        ' if value["action"]=="clear": return await actions.clear_profile(value)',
        ' try: return await state.logout(value["domain"])',
        ' except state.ParentDomainRequired as error: return {"error":"browser_logout_parent_domain_required","domains":error.domains}',
        'try: result=asyncio.run(actions.execute(request,perform))',
        'except Exception: result={"error":"browser_action_unknown"}',
        'boot=actions.Path("/proc/sys/kernel/random/boot_id").read_text().strip()',
        'print(json.dumps({"id":request["id"],"boot":boot,"result":result},separators=(",",":")))']
    return '\n'.join(lines)


async def mutate(browser, endpoint, *, authorize, key, action, domain=''):
    if action not in {'logout','clear'} or not isinstance(key,str) or not 1<=len(key)<=256:
        raise ValueError('browser_request_invalid')
    domain = sandbox_browser_state.domain_name(domain) if action=='logout' else ''
    if endpoint.key in browser.active or len(browser.active)>=4:
        raise BrowserUnavailable('browser_busy')
    browser.active.add(endpoint.key)
    dispatched = False
    try:
        async with asyncio.timeout(45):
            route = await browser.calls(browser.check,endpoint,authorize)
            owner = workspace_owner(endpoint)
            items = await browser.manager.list(owner)
            if not items:
                return {'error':'browser_vm_missing'}
            sid = items[0]['sandbox_id']
            async with browser.manager.io_lock(sid):
                await browser.manager._metadata(owner,sid)
                await browser.calls(browser.check,endpoint,authorize,route)
                if not await browser.manager._running(sid):
                    return {'error':'browser_vm_offline'}
                code,boot,_ = await browser.manager._guest_exec(sid,'/usr/bin/python3',
                    ['-I','-S','-c','from pathlib import Path;print(Path("/proc/sys/kernel/random/boot_id").read_text().strip())'],timeout=5)
                if code or type(boot) is not bytes or len(boot)>64:
                    raise BrowserUnavailable('browser_boot_unavailable')
                boot = boot.decode().strip()
                if str(UUID(boot))!=boot:
                    raise BrowserUnavailable('browser_boot_invalid')
                request = dict(id=hashlib.sha256((endpoint.key+'\0'+key).encode()).hexdigest(),boot=boot,
                    expires=int(browser.sessions.clock())+60,action=action,domain=domain)
                await browser.calls(browser.check,endpoint,authorize,route)
                dispatched = True
                executable = '/opt/gaoji-tools/bin/python' if action=='logout' else '/usr/bin/python3'
                pending = asyncio.create_task(browser.manager._guest_exec(sid,executable,['-I','-c',script(request)],timeout=35))
                try:
                    while not pending.done():
                        await browser.calls(browser.check,endpoint,authorize,route)
                        await asyncio.wait((pending,),timeout=.25)
                    code,content,_ = await pending
                finally:
                    if not pending.done():
                        pending.cancel()
                    await asyncio.gather(pending,return_exceptions=True)
                await browser.manager._metadata(owner,sid)
                await browser.calls(browser.check,endpoint,authorize,route)
            if code or type(content) is not bytes or len(content)>65536:
                raise BrowserActionUnknown('browser_action_unknown')
            envelope = json.loads(content)
            if (type(envelope) is not dict or set(envelope)!={'id','boot','result'}
                    or envelope['id']!=request['id'] or envelope['boot']!=boot):
                raise BrowserActionUnknown('browser_action_unknown')
            result = envelope['result']
            expected = {'cleared':True,'domain':domain} if action=='logout' else {'cleared':True}
            if result==expected and type(result.get('cleared')) is bool:
                return result
            if action=='logout' and type(result) is dict and set(result)=={'error','domains'} and result['error']=='browser_logout_parent_domain_required':
                parents = result['domains']
                if (type(parents) is list and 0<len(parents)<=128 and all(type(name) is str and sandbox_browser_state.domain_name(name)==name
                        and name!=domain and sandbox_browser_state.within(domain,name) for name in parents)):
                    return result
            if (type(result) is dict and set(result)=={'error'} and result['error'] in {'browser_busy','browser_request_invalid',
                    'browser_request_expired','browser_request_conflict','browser_boot_changed','browser_action_unknown'}):
                return result
            raise BrowserActionUnknown('browser_action_unknown')
    except asyncio.CancelledError:
        raise # Native execution remains needs_attention; never claim it did not run.
    except Exception as error:
        if dispatched:
            raise BrowserActionUnknown('browser_action_unknown') from None
        if isinstance(error,(PermissionError,BrowserUnavailable)):
            raise
        raise BrowserUnavailable('browser_unavailable') from None
    finally:
        browser.active.discard(endpoint.key)
