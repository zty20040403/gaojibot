"""Deterministic guest search fixtures; no browser or network."""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.plugins.ai_chat import sandbox_search as search

FIXTURES = {
    'bing': ('h2 a', 'h2', '/ck/a?u=TEST_PUBLIC'),
    'baidu': ('h3 a', 'h3', '/link?url=TEST_PUBLIC'),
    'bilibili': ('a:has(h3), h3 a', 'h3', '//www.bilibili.com/video/BVtest?x=1'),
    'xiaohongshu': ('a.cover, a.title', '.title', '/explore/abc?xsec_token=TEST_SECRET&x=1'),
    'zhihu': ('.ContentItem-title a', '.ContentItem-title', '/question/123?sort=created'),
}


class SearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_offline_search(self):
        page, context = MagicMock(), MagicMock()
        page.goto = AsyncMock(return_value=MagicMock(status=200))
        page.evaluate = AsyncMock()
        page.locator.return_value.first.wait_for = AsyncMock()
        context.route, context.route_web_socket = AsyncMock(), AsyncMock()
        context.new_page, context.close = AsyncMock(return_value=page), AsyncMock()
        browser = MagicMock(new_context=AsyncMock(return_value=context))
        connect = AsyncMock(return_value=browser)
        with patch.object(search.syslog, 'syslog') as journal:
            for engine, (link, title, href) in FIXTURES.items():
                page.url = 'https://%s%s?q=TEST' % search.ENGINES[engine][:2]
                page.evaluate.return_value = [{'url': href, 'title': 'Title', 'snippet': 'S' * 900}]
                result = await search.search(engine, 'x&y', connect)
                self.assertEqual((result['status'], len(result['results'][0]['snippet'])), ('ok', 800))
                self.assertNotIn('TEST_SECRET', str(result))
                self.assertEqual(page.evaluate.call_args.args[1][1:3], [link, title])
                self.assertIn('x%26y', page.goto.call_args.args[0])
                journal.assert_called_with(search.syslog.LOG_INFO, f'gaoji_search engine={engine} status=ok')
            self.assertEqual((journal.call_count, context.close.await_count), (5, 5))
            browser.new_context.assert_called_with(service_workers='block', java_script_enabled=True, accept_downloads=False)
            page.url = 'https://www.bing.com/search?q=TEST'
            for data, status in (([], 'empty'), (None, 'captcha')):
                page.evaluate.return_value = data
                self.assertEqual((await search.search('bing', 'x', connect))['status'], status)
                journal.assert_called_with(search.syslog.LOG_INFO, f'gaoji_search engine=bing status={status}')
            page.url = 'https://www.bing.com/account'
            self.assertEqual((await search.search('bing', 'x', connect))['error'], 'unexpected_url')
            page.goto.side_effect = RuntimeError('TEST_SECRET')
            self.assertEqual((await search.search('bing', 'x', connect))['error'], 'navigation_error')
            self.assertEqual((await search.search('bing', 'x' * 513, connect))['error'], 'invalid_arguments')
            page.goto.side_effect, page.url = None, 'https://www.bing.com/search?q=TEST'
            page.evaluate.return_value = [{'url': f'https://example.org/{i}', 'title': 'T', 'snippet': ''} for i in range(20)]
            journal.side_effect = OSError('TEST_SECRET')
            with self.assertLogs(level='WARNING') as logs:
                result = await search.search('bing', 'TEST_SECRET', connect)
            self.assertEqual((result['status'], len(result['results'])), ('ok', 10))
            self.assertNotIn('TEST_SECRET', str(logs.output))
            for public in (False, True):
                await search.search('bing', 'TEST', connect, documents_only=public)
                browser.new_context.assert_called_with(service_workers='block', java_script_enabled=not public, accept_downloads=False)
                handler = context.route.await_args.args[1]
                for url, nav, method, allowed in [('https://cdn.example.org/a', False, 'GET', not public),
                                                  (page.url, True, 'GET', True), ('https://example.org/a', True, 'GET', False),
                                                  (page.url, False, 'POST', False)]:
                    route = MagicMock(abort=AsyncMock(), continue_=AsyncMock())
                    route.request = MagicMock(url=url, method=method, is_navigation_request=lambda: nav,
                                              resource_type='document' if nav else 'image')
                    await handler(route)
                    (route.continue_ if allowed else route.abort).assert_awaited_once()

    def test_result_urls(self):
        base = 'https://www.bing.com/search?q=TEST'
        for href in ('', ' ', '#section', 'javascript:TEST', 'https://user:pass@example.org/'):
            self.assertIsNone(search.result_url(href, base))
        self.assertEqual(search.result_url('https://example.org/?token=TEST_SECRET&x=1', base),
                         'https://example.org/?x=1')
