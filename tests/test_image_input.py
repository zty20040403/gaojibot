"""Real ledger, pinned transport and ffmpeg, with synthetic image bytes only."""
import shutil
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx2

from src.plugins.ai_chat.adapters.qq_official.content_reader import PinnedContentReader, PinnedMediaTransport
from src.plugins.ai_chat.adapters.qq_official.image_input import current_images, read_image
from src.plugins.ai_chat.adapters.qq_official.media_refs import MediaDenied, MediaRef
from src.plugins.ai_chat.agent_loop import _build_messages, ask_deepseek_with_tools
from src.plugins.ai_chat.model_catalog import ModelCapabilities, ModelProfile
from src.plugins.ai_chat.tool_images import ToolImage
from src.plugins.ai_chat.vision_worker import VisionJobError, VisionWorker
from tests import test_image_selection as selection
from tests.test_qq_official_image_formats import png


class ImageInputTests(unittest.IsolatedAsyncioTestCase):
    setUp = selection.ImageSelectionTests.setUp
    say = selection.ImageSelectionTests.say

    def configure(self, data=None, addresses=('93.184.216.34',)):
        self.requests = []
        async def dns(host, port):
            return addresses
        async def fetch(request):
            self.requests.append(request)
            return httpx2.Response(200, content=data or png())
        self.session.service.image_fetcher = PinnedContentReader(max_bytes=1024*1024,
            transport_factory=lambda url: PinnedMediaTransport(url, resolver=dns, transport=httpx2.MockTransport(fetch)))
        self.session.service.config = SimpleNamespace(ocr=True, timeout_seconds=5)
        self.session.frontend_vision = True
        self.session.inbound.batch_message_ids = ()
        self.session.inbound.reply_to_native_message_id = None

    async def test_current_and_quoted_images_only_in_volatile_block_with_four_image_cap(self):
        self.configure()
        self.say(image=True)
        quoted_native = f'TEST_{self.counter}'
        batch = [self.say(image=True) for _ in range(5)]
        self.session.inbound.batch_message_ids = tuple(batch)
        self.session.inbound.reply_to_native_message_id = quoted_native
        images, warning = await current_images(self.session)
        self.assertEqual(len(images), 4)
        self.assertEqual(len(self.requests), 4)
        self.assertIn('view_image', warning)
        baseline = _build_messages('TEST', [], group_context='TEST stable tail', system_prompt='TEST stable system')
        actual = _build_messages('TEST', [], group_context='TEST stable tail', system_prompt='TEST stable system', input_images=images)
        self.assertEqual(actual[:-1], baseline[:-1])
        self.assertEqual(sum(p['type'] == 'image_url' for p in actual[-1]['content']), 4)
        self.assertIn('<untrusted', str(actual[-1]))
        # A referenced old image is included even though it already appears in the text tail.
        self.say()
        self.session.inbound.batch_message_ids = ()
        images, warning = await current_images(self.session)
        self.assertEqual(len(images), 1)
        self.assertIn('image#', images[0].text)
        self.assertEqual(warning, '')

    async def test_text_model_never_downloads_and_private_dns_fails_visibly(self):
        self.configure(addresses=('127.0.0.1',))
        self.say(image=True)
        self.session.frontend_vision = False
        self.assertEqual(await current_images(self.session), ((), ''))
        self.assertFalse(self.requests)
        self.session.frontend_vision = True
        images, warning = await current_images(self.session)
        self.assertFalse(images)
        self.assertIn('未加载', warning)
        self.assertFalse(self.requests)

    async def test_cache_still_revalidates_authority_and_foreign_handles_are_denied(self):
        self.configure()
        canonical = self.say(image=True)
        source = await self.session.resolver.resolve(MediaRef(canonical, 0))
        image = await read_image(self.session, source)
        self.assertEqual(await read_image(self.session, source), image)
        self.assertEqual(len(self.requests), 1)
        with self.ledger._transaction() as cursor:
            cursor.execute('UPDATE conversation_visibility SET min_canonical_message_id=?', (canonical + 1,))
        with self.assertRaises(MediaDenied):
            await read_image(self.session, source)

    async def test_no_tools_model_still_receives_images(self):
        from tests.test_qq_official_turn_endtoend import model_response
        calls = []
        async def complete(**kwargs):
            calls.append(kwargs)
            return model_response('TEST answer', [], kwargs)
        profile = ModelProfile('test', 'openai', 'openai-chat', 'TEST_MODEL', 'https://example.invalid',
                               api_key_required=False, capabilities=ModelCapabilities(vision=True, tools=False))
        image = ToolImage('TEST image', 'data:image/png;base64,TEST')
        with patch('src.plugins.ai_chat.agent_loop._create_completion', new=complete):
            await ask_deepseek_with_tools('TEST', [], [], None, profile=profile,
                                         system_prompt='TEST system', input_images=(image,))
        self.assertIn(image.data_url, str(calls[0]['messages'][-1]))

    async def test_download_limit_and_decoder_timeout_are_explicit(self):
        self.configure()
        self.say(image=True)
        self.session.service.image_fetcher.max_bytes = 1
        images, warning = await current_images(self.session)
        self.assertFalse(images)
        self.assertIn('download_size_limit', warning)
        self.session.service.image_fetcher.max_bytes = 1024*1024
        with patch.object(VisionWorker, 'prepare_image', side_effect=subprocess.TimeoutExpired('TEST', 1)):
            images, warning = await current_images(self.session)
        self.assertFalse(images)
        self.assertIn('image_preparation_unavailable', warning)

    async def test_corrupt_image_is_not_sent_to_the_model(self):
        self.configure(data=b'\x89PNG\r\n\x1a\nTEST invalid image')
        self.say(image=True)
        images, warning = await current_images(self.session)
        self.assertFalse(images)
        self.assertIn('image_preparation_unavailable', warning)

    def test_compressed_large_edge_is_scaled_and_missing_decoder_fails_closed(self):
        data = png(2000, 10, (b'\0' + b'\x20\x40\x80' * 2000) * 10)
        self.assertLess(len(data), 1024*1024)
        content, mime = VisionWorker.prepare_image(data, 'image/png', max_vision_bytes=1024*1024, strict=True)
        self.assertEqual(mime, 'image/jpeg')
        probe = subprocess.run([shutil.which('ffprobe'), '-v', 'error', '-show_entries', 'stream=width,height',
            '-of', 'csv=p=0', '-i', 'pipe:0'], input=content, capture_output=True, check=True)
        self.assertLessEqual(max(map(int, probe.stdout.decode().strip().split(','))), 1568)
        with self.assertRaises(VisionJobError), patch('shutil.which', return_value=None):
            VisionWorker.prepare_image(data, 'image/png', max_vision_bytes=1024*1024, strict=True)
