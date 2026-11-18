import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.plugins.ai_chat.tools import background, sources
from src.plugins.ai_chat.tools.state import ToolContext, TurnState
from src.plugins.ai_chat.web_fetch import FetchedPage
from src.plugins.ai_chat.web_search import SearchError, SearchResult


def turn(bg=False):
    settings = SimpleNamespace(max_input_chars=1500, search_max_results=10, search_timeout_seconds=10, search_api_key='TEST_KEY')
    return TurnState(ToolContext(SimpleNamespace(context=SimpleNamespace(settings=settings, logger=Mock()))), search_background=bg)


async def call(state, *queries):
    return json.loads(await sources.web_search(state, {'queries': list(queries)}))


class SearchProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_progress_and_hard_cap(self):
        for bg, cap in ((False, 15), (True, 40)):
            state = turn(bg)
            async def unique(queries, *, all_results, **kwargs):
                hit = SearchResult('TEST', f'https://example.test/{queries[0]}', 'TEST', '2026-01-01', 'exa')
                all_results.append(hit)
                return [hit]
            with patch.object(sources, 'search_web', side_effect=unique) as provider:
                replies = await asyncio.gather(*(call(state, str(i)) for i in range(cap+2)))
            self.assertEqual(provider.await_count, cap)
            self.assertEqual(sum(r['ok'] for r in replies), cap)
            self.assertEqual(replies[-1]['note'], sources.STOP_SEARCH)
            self.assertIn('新链接 1 条', replies[cap-1]['progress'])

    async def test_no_new_counts_host_path_not_query_or_fragment(self):
        for bg, permitted in ((False, 3), (True, 4)):
            state = turn(bg)
            async def repeat(queries, *, all_results, **kwargs):
                hit = SearchResult('TEST', f'https://example.test/read?v={queries[0]}#x', '')
                all_results.append(hit)
                return [hit]
            with patch.object(sources, 'search_web', side_effect=repeat) as provider:
                for i in range(permitted):
                    reply = await call(state, str(i))
                    self.assertIn(f'新链接 {int(i == 0)} 条', reply['progress'])
                self.assertEqual((await call(state, 'stop'))['note'], sources.STOP_SEARCH)
                self.assertEqual(provider.await_count, permitted)

    async def test_failing_providers_do_not_reset_the_streak(self):
        state = turn()
        with patch.object(sources, 'search_web', side_effect=SearchError('unavailable')) as provider:
            for _ in range(2):
                self.assertIn('error', await call(state, 'TEST'))
            self.assertEqual((await call(state, 'TEST'))['note'], sources.STOP_SEARCH)
            self.assertEqual(provider.await_count, 2)

    async def test_slow_foreground_passes_evidence_not_credentials(self):
        state = turn()
        clock = SimpleNamespace(monotonic=iter((0, 31)).__next__)
        async def searched(*args, all_results, **kwargs):
            all_results.append(SearchResult('TEST', 'https://example.test/found?secret=TEST_SECRET', ''))
            return all_results
        with patch.object(sources, 'time', clock), patch.object(sources, 'search_web', side_effect=searched):
            result = await call(state, 'TEST question', 'api_key=TEST_SECRET')
        self.assertIn('已用 31 秒', result['progress'])
        self.assertIn('say', result['handoff'])
        with patch.object(sources, 'fetch_page', return_value=FetchedPage('https://example.test/read?x=TEST_SECRET', 'T', 'body')):
            await sources.web_fetch(state, {'url': 'https://example.test/read'})
        control = AsyncMock(return_value={'job': 'job#1'})
        with patch.object(background, 'service', return_value=SimpleNamespace(control=control)):
            await background.execute(state, {'task': 'research'}, name='agent')
        task = control.await_args.args[2]['task']
        for text in ('TEST question', 'https://example.test/found', 'https://example.test/read', 'read_urls'):
            self.assertIn(text, task)
        self.assertNotIn('TEST_SECRET', task)
