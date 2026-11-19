import asyncio
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
import httpx

from src.plugins.ai_chat import sandbox_search, web_search


class PublicSearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        path = self.enterContext(tempfile.TemporaryDirectory(dir='/tmp')) + '/search.sock'
        self.enterContext(patch.object(web_search, '_BROWSER_SOCKET', path))
        with patch.dict(sys.modules, sandbox_search=sandbox_search):
            app = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'nix/search-browser.py'))['application']
        self.search = AsyncMock()
        self.enterContext(patch.dict(app.__globals__, search=self.search))
        runner = web.AppRunner(app(AsyncMock()))
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        self.site = web.UnixSite(runner, path)
        await self.site.start()

    async def test_exa_timeout_uses_query_only_socket_then_last_provider_if_unavailable(self):
        self.search.side_effect = [{'status': 'captcha'}, {'status': 'ok', 'results': [
            {'title': 'TEST', 'url': 'https://example.test/read', 'snippet': 'body'}]}]
        providers = []
        def post(request):
            providers.append(request.url.host)
            if request.url.host == 'mcp.exa.ai':
                raise httpx.ReadTimeout('TEST timeout')
            return httpx.Response(200, json={'data': {'webPages': {'value': []}}})
        async def run():
            return await web_search.search_web(['“TEST query” OR site:example.test'], api_key='TEST_KEY', transport=httpx.MockTransport(post))
        self.assertEqual([(hit.title, hit.provider) for hit in await run()], [('TEST', 'browser')])
        self.assertEqual(providers, ['mcp.exa.ai'])
        self.assertEqual([c.args[:2] for c in self.search.await_args_list], [(engine, 'TEST query') for engine in ('bing', 'baidu')])
        self.assertTrue(all(c.kwargs == {'documents_only': True} for c in self.search.await_args_list))
        await self.site.stop()
        self.assertEqual(await run(), [])
        self.assertEqual(providers[-2:], ['mcp.exa.ai', 'api.bochaai.com'])

    async def test_full_service_refuses_without_starting_a_fifth_search(self):
        barrier, release = asyncio.Barrier(5), asyncio.Event()
        async def hold(*args, **kwargs):
            await barrier.wait()
            await release.wait()
            return {'status': 'ok', 'results': []}
        self.search.side_effect = hold
        async with asyncio.timeout(3), asyncio.TaskGroup() as group:
            for _ in range(4):
                group.create_task(web_search._search_browser('TEST', 1))
            await barrier.wait()
            with self.assertRaisesRegex(web_search._ProviderError, 'busy'):
                await web_search._search_browser('TEST fifth', 1)
            release.set()
        self.assertEqual(self.search.await_count, 4)
