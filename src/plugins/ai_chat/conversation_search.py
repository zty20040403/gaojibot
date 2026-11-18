"""Read-only search in the physical conversation's already-running container."""
import json
import logging

from .sandbox import SandboxError
from .web_search import _ProviderError, _result

_log = logging.getLogger(__name__)


async def search_browser(manager, owner, query, limit):
    try:
        items = await manager.list(owner)
        if not items or not items[0]['status'].startswith('Up '):
            raise _ProviderError('offline')
        sid = items[0]['sandbox_id']
        async with manager.io_lock(sid):
            await manager._metadata(owner, sid)
            for engine in ('bing', 'baidu'):
                code, stdout, _ = await manager._guest_exec(sid, '/run/current-system/sw/bin/gaoji-python',
                    ['-m', 'sandbox_search', json.dumps([engine, query])], timeout=9)
                if code or len(stdout) > 100_000:
                    raise _ProviderError('guest_error')
                reply = json.loads(stdout)
                status = reply['status']
                if status not in ('ok', 'empty', 'captcha', 'error'):
                    raise ValueError
                _log.info('browser_search engine=%s outcome=%s', engine, status)
                if status == 'ok':
                    rows = reply['results']
                    if not isinstance(rows, list) or not 1 <= len(rows) <= 10:
                        raise ValueError
                    return [_result(row['title'], row['url'], row['snippet'], '', 'browser') for row in rows[:limit]]
            if status != 'empty':
                raise _ProviderError(status)
            return []
    except SandboxError:
        raise _ProviderError('unavailable') from None
    except (KeyError, TypeError, ValueError):
        raise _ProviderError('schema_error') from None
