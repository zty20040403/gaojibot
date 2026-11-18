"""Small adapter checks; delivery/observation transactions are exercised by integration tests."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official.media_refs import MediaDenied
from src.plugins.ai_chat.adapters.qq_official.media_runtime import MediaConfig, OfficialMediaService
from src.plugins.ai_chat.adapters.qq_official.proactive_ingress import ProactiveIngress


class StickerAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_sticker_does_not_wake_turn(self):
        async def calls(fn, *args):
            return fn(*args)
        ingress = ProactiveIngress(token=object(), transaction=None, admission=None,
            outbox=None, scorer=None, clock=lambda: 1, database_calls=calls,
            wake_turn=lambda **kwargs: self.fail('must not start turn'))
        self.assertFalse(await ingress.wake(SimpleNamespace(inbox_id='source'),
            SimpleNamespace(action='sticker', reason='happy')))

    async def test_curated_search_skips_unsendable_and_does_not_fallback(self):
        token = object()
        async def calls(fn, *args):
            return fn(*args)
        library = SimpleNamespace(find_stickers=lambda query, limit: [
            SimpleNamespace(handle='media#1'), SimpleNamespace(handle='media#2')])
        runtime = SimpleNamespace(_token=token, _stopping=False, file_service=object(), _calls=calls)
        service = OfficialMediaService(runtime, token,
            SimpleNamespace(media_library=library, settings=SimpleNamespace(media_max_vision_bytes=100)),
            MediaConfig(stickers=True))
        def verified(service, handle, endpoint, actor):
            if handle == 'media#1':
                raise MediaDenied('sticker_content_unavailable')
            return b'image', 'sticker.png', SimpleNamespace(handle=handle)
        with patch('src.plugins.ai_chat.adapters.qq_official.media_runtime._verified_sticker', verified):
            self.assertEqual((await service.unsolicited_sticker('happy', 'ep', 'actor'))[2].handle, 'media#2')
        service.config = MediaConfig(stickers=False)
        with self.assertRaises(MediaDenied):
            await service.unsolicited_sticker('happy', 'ep', 'actor')
