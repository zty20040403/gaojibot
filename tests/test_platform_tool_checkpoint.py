"""Host adjunct recovery for foreground search and deferred QQ media (no database)."""
import json
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from src.plugins.ai_chat.adapters.qq_official.media_runtime import MediaSession
from src.plugins.ai_chat.agent_loop import AgentTrace
from src.plugins.ai_chat.application.platform_chat import _TurnCheckpoint
from src.plugins.ai_chat.tools.background import search_handoff
from src.plugins.ai_chat.tools.sources import web_search
from src.plugins.ai_chat.tools.state import ToolContext, TurnState
from src.plugins.ai_chat.web_search import SearchResult, render_search_sources


class DeferredMedia:
    def __init__(self):
        self.pending_text = []
        self.voice_sent = False
        self.selected_sticker = None


def turn():
    settings = SimpleNamespace(max_input_chars=200, search_max_results=10,
                               search_timeout_seconds=10, search_api_key='TEST_SEARCH_KEY')
    context = ToolContext(SimpleNamespace(context=SimpleNamespace(settings=settings)),
                          capabilities=frozenset({'web'}))
    return TurnState(context=context)


class PlatformToolCheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def snapshot(self, state=None, media=None):
        saved = []
        async def sink(value):
            saved.append(value)
        checkpoint = _TurnCheckpoint(state or turn(), media, None, sink, None)
        await checkpoint.save({'phase': 'batch', 'resumable': True})
        self.assertEqual(len(saved), 1)
        return saved[0]

    async def test_search_budget_citation_and_handoff_continue_after_restore(self):
        original = turn()
        original.search_results = [SearchResult('First', 'https://example.org/one', 'private snippet'),
                                   SearchResult('Second', 'https://example.org/two', '')]
        original.search_count = 7
        original.search_seconds = 31.4
        original.search_no_new = 1
        original.search_seen = {('example.org', '/one'), ('example.org', '/two')}
        original.search_terms = ['first query']
        original.search_found = ['https://example.org/one']
        original.search_read = ['https://example.org/one']
        saved = await self.snapshot(original)
        encoded = json.dumps(saved, ensure_ascii=False)
        self.assertNotIn('private snippet', encoded)
        self.assertNotIn('TEST_SEARCH_KEY', encoded)
        self.assertEqual(saved['adjunct']['search_seen'], [['example.org', '/one'], ['example.org', '/two']])
        restored = turn()
        _TurnCheckpoint(restored, None, None, AsyncMock(), json.loads(encoded))
        self.assertEqual((restored.search_count, restored.search_seconds, restored.search_no_new), (7, 31.4, 1))
        self.assertEqual(restored.search_seen, original.search_seen)
        self.assertEqual((restored.search_terms, restored.search_found, restored.search_read),
                         (original.search_terms, original.search_found, original.search_read))
        self.assertEqual(render_search_sources(restored.search_results, 'As [2] says')[1],
                         render_search_sources(original.search_results, 'As [2] says')[1])
        self.assertIn('first query', search_handoff(restored))
        self.assertIn('https://example.org/one', search_handoff(restored))

        async def same_results(_queries, *, all_results, **_kwargs):
            results = [SearchResult('Again', 'https://example.org/two', 'new snippet')]
            all_results.extend(results)
            return results

        with patch('src.plugins.ai_chat.tools.sources.search_web', new=same_results):
            result = json.loads(await web_search(restored, {'queries': ['second query']}))
        self.assertTrue(result['ok'])
        self.assertEqual(result['results'][0]['id'], 2)
        self.assertEqual((restored.search_count, restored.search_no_new), (8, 2))
        self.assertEqual(len(restored.search_results), 2)
        self.assertIn('已用', result['progress'])
        self.assertIn('handoff', result)
        with patch('src.plugins.ai_chat.tools.sources.search_web', new=AsyncMock()) as search:
            stopped = json.loads(await web_search(restored, {'queries': ['more']}))
            search.assert_not_awaited()
        self.assertFalse(stopped['ok'])
        self.assertEqual(restored.search_count, 8)

    async def test_restored_selected_sticker_is_requeued_with_real_media_session(self):
        async def calls(function, *args):
            return function(*args)

        row = {'mime_type': 'image/png', 'byte_size': 4, 'sha256': 'digest'}
        connection = SimpleNamespace(execute=lambda *_: SimpleNamespace(fetchone=lambda: row), close=lambda: None)
        library = SimpleNamespace(database=SimpleNamespace(store_connection=lambda: connection),
            root='/unused', get_sticker=lambda *_: SimpleNamespace(handle='media#42',
                mime_type='image/png', storage_path='unused', summary='hello', emotions=()))
        files = SimpleNamespace(transaction=lambda: nullcontext(object()), _account=lambda *_: None,
                                _route=lambda *_: True)
        service = SimpleNamespace(runtime=SimpleNamespace(_calls=calls, file_service=files),
                                  app=SimpleNamespace(media_library=library))

        def session():
            media = MediaSession.__new__(MediaSession)
            media.service = service
            media.inbound = SimpleNamespace(endpoint='endpoint', actor='actor')
            media.platform = SimpleNamespace(pending_media=[])
            media.pending_text = []
            media.voice_sent = False
            media.selected_sticker = None
            media._turn_file_meta = lambda: ('execution', 100, 1, 0)
            return media

        state = turn()
        state.sticker_handles_seen = {'media#42'}
        media = session()
        deferred = lambda cursor: None
        with patch.object(MediaSession, 'current', new_callable=AsyncMock), \
                patch.object(MediaSession, '_send_bytes', new_callable=AsyncMock, return_value=deferred) as send, \
                patch('src.plugins.ai_chat.adapters.qq_official.media_runtime._read_library_bytes', return_value=b'data'), \
                patch('src.plugins.ai_chat.adapters.qq_official.media_runtime.detect_image', return_value='image/png'):
            result = await media.sticker({'media_handle': 'media#42'}, state.sticker_handles_seen)
            self.assertEqual(result['state'], 'queued')
            self.assertIs(media.platform.pending_media[0], deferred)
            self.assertTrue(send.await_args.kwargs['defer'])
            state.sticker_handles_this_turn.add('media#42')
            state.sticker_sent = state.delivered = True
            media.pending_text.append('remaining reply')
            media.voice_sent = True
            saved = await self.snapshot(state, media)
            restored_media = session()
            restored = turn()
            checkpoint = _TurnCheckpoint(restored, restored_media, None, AsyncMock(), saved)
            self.assertEqual(restored_media.platform.pending_media, [])  # closure is not serialized
            self.assertEqual(restored_media.pending_text, ['remaining reply'])
            self.assertTrue(restored_media.voice_sent)
            self.assertTrue(restored.delivered)
            self.assertTrue(restored.sticker_sent)
            await checkpoint.requeue()
            self.assertEqual(restored_media.platform.pending_media, [deferred])
            self.assertEqual(restored_media.selected_sticker, 'media#42')
            self.assertEqual(send.await_count, 2)

    async def test_usage_and_sticker_authority_survive_without_serializing_media_closure(self):
        state, media = turn(), DeferredMedia()
        state.sticker_handles_seen.add('media#42')
        state.sticker_sent = True
        media.selected_sticker = 'media#42'
        trace = AgentTrace(input_tokens=12, output_tokens=3, cached_tokens=2, total_tokens=15)
        saved = []
        async def sink(value):
            saved.append(value)
        await _TurnCheckpoint(state, media, trace, sink, None).save({'phase': 'batch', 'resumable': True})
        resumed_trace = AgentTrace()
        _TurnCheckpoint(turn(), DeferredMedia(), resumed_trace, AsyncMock(), saved[0])
        self.assertEqual((resumed_trace.input_tokens, resumed_trace.output_tokens,
                          resumed_trace.cached_tokens, resumed_trace.total_tokens), (12, 3, 2, 15))
        invalid = deepcopy(saved[0])
        invalid['adjunct']['seen'] = []
        with self.assertRaisesRegex(RuntimeError, 'sticker is unavailable'):
            _TurnCheckpoint(turn(), DeferredMedia(), AgentTrace(), AsyncMock(), invalid)

    async def test_sensitive_and_oversized_evidence_never_enters_adjunct(self):
        for field, value in (
            ('search_terms', ['password=private']),
            ('search_terms', ['https://example.org/path?token=private']),
            ('search_found', ['https://example.org/page?token=private']),
            ('search_read', ['https://user:private@example.org/page']),
            ('search_seen', {('example.org', '/access_token/private')}),
            ('search_found', ['https://example.org/' + 'p' * 510]),
            ('search_terms', ['x' * 300]),
        ):
            with self.subTest(field=field, value=value):
                state = turn()
                setattr(state, field, value)
                saved = await self.snapshot(state)
                self.assertFalse(saved['resumable'])
                self.assertIsNone(saved['adjunct'])
                with self.assertRaisesRegex(RuntimeError, 'cannot be resumed'):
                    _TurnCheckpoint(turn(), None, None, AsyncMock(), saved)
        state = turn()
        state.search_results = [SearchResult('name', 'https://example.org/?api_key=private', '')]
        saved = await self.snapshot(state)
        self.assertIsNone(saved['adjunct'])
        self.assertNotIn('private', json.dumps(saved))

    async def test_marker_pending_does_not_allow_unsafe_selection_replay(self):
        saved = []
        async def sink(value):
            saved.append(value)
        checkpoint = _TurnCheckpoint(turn(), DeferredMedia(), None, sink, None)
        await checkpoint.save({'phase': 'final', 'resumable': True, 'answer': 'hello [表情：happy]'})
        self.assertFalse(saved[-1]['resumable'])
        self.assertIsNotNone(saved[-1]['adjunct'])
        await checkpoint.finish('hello')
        self.assertTrue(saved[-1]['resumable'])  # selection has finished; no marker remains


if __name__ == '__main__':
    unittest.main()
