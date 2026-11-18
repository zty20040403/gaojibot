import asyncio
from functools import partial
import json
import unittest
from unittest.mock import AsyncMock, Mock

import httpx

from src.plugins.ai_chat.conversation_search import search_browser
from src.plugins.ai_chat.web_search import search_web


class ConversationSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_chain_uses_only_owned_running_container_after_exa(self):
        manager = Mock()
        manager.list = AsyncMock(return_value=[{'sandbox_id': 'TEST_SID', 'status': 'Up (container)'}])
        manager.io_lock.return_value = asyncio.Lock()
        manager._metadata = AsyncMock()
        manager._guest_exec = AsyncMock(side_effect=[(0, b'{"status":"captcha"}', b''), (0, json.dumps({
            'status': 'ok', 'results': [{'title': 'TEST', 'url': 'https://example.test/read', 'snippet': 'body'}],
        }).encode(), b'')])
        calls = []
        def post(request):
            calls.append(request.url.host)
            if request.url.host == 'mcp.exa.ai':
                return httpx.Response(429)
            return httpx.Response(200, json={'data': {'webPages': {'value': []}}})
        options = dict(browser=partial(search_browser, manager, 'TEST_OWNER'), api_key='TEST_KEY',
                       transport=httpx.MockTransport(post))
        hits = await search_web(['TEST query'], **options)
        self.assertEqual([(r.title, r.provider) for r in hits], [('TEST', 'browser')])
        self.assertEqual(calls, ['mcp.exa.ai'])
        manager._metadata.assert_awaited_once_with('TEST_OWNER', 'TEST_SID')
        self.assertEqual([json.loads(call.args[2][-1])[0] for call in manager._guest_exec.await_args_list], ['bing', 'baidu'])
        manager.list.return_value[0]['status'] = 'Exited'
        self.assertEqual(await search_web(['TEST query'], **options), [])
        self.assertEqual(calls[-2:], ['mcp.exa.ai', 'api.bochaai.com'])
        self.assertEqual(manager._guest_exec.await_count, 2)
        manager.start_owned.assert_not_called()
        manager.ensure_default.assert_not_called()
