"""Official caption queue -> pinned bytes -> real model gateway -> scoped history."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch

import httpx2

from src.bot_storage import DatabaseCalls
from src.bot_storage.media_cleanup import LegacyMediaCleanup
from src.plugins.ai_chat.adapters.qq_official.content_reader import PinnedContentReader, PinnedMediaTransport
from src.plugins.ai_chat.image_captions import CaptionSettings
from src.plugins.ai_chat.llm_gateway import LLMGateway
from src.plugins.ai_chat.media_library import MediaLibrary
from src.plugins.ai_chat.model_catalog import ModelCatalog, ModelProfile, ModelCapabilities
from src.plugins.ai_chat.official_caption_worker import OfficialCaptionWorker
from src.plugins.ai_chat.quota import UsageStore
from src.plugins.ai_chat.vision_worker import VisionWorker
from tests import test_image_captions as admission
from tests import test_qq_official_ingest as ingress
from tests.test_qq_official_image_formats import png


class CaptionWorkerTests(unittest.IsolatedAsyncioTestCase):
    tx = ingress.QQOfficialIngestTests.tx
    query = ingress.QQOfficialIngestTests.query
    provision = ingress.QQOfficialIngestTests.provision
    seed = ingress.QQOfficialIngestTests.seed
    guard = ingress.QQOfficialIngestTests.guard
    process = ingress.QQOfficialIngestTests.process
    image = admission.CaptionAdmissionTests.image
    settings = admission.CaptionAdmissionTests.settings

    def setUp(self):
        ingress.QQOfficialIngestTests.setUp(self)
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.calls = DatabaseCalls()
        self.addAsyncCleanup(self.calls.close)
        self.model_calls, self.downloads, self.provider_action = [], [], None
        fixture = self
        class Provider:
            async def create_completion(self, profile, **kwargs):
                fixture.model_calls.append(kwargs)
                if fixture.provider_action:
                    fixture.provider_action()
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                    'summary':'TEST 猫坐在窗边', 'description':'TEST 猫坐在窗边', 'text':'TEST screenshot',
                    'observations':[], 'safety':'safe'})))], usage=SimpleNamespace(prompt_tokens=12,completion_tokens=8))
        profile = ModelProfile('testvision','openai','openai-chat','TEST_MODEL','https://example.invalid',
            api_key='TEST_KEY',max_input_tokens=4096,max_output_tokens=256,capabilities=ModelCapabilities(vision=True))
        catalog = ModelCatalog({'testvision':profile},default_profile='testvision')
        gateway = LLMGateway(providers={'openai-chat':Provider()},fallback_enabled=False)
        self.library = MediaLibrary(self.database,root=Path(self.temp.name),model_catalog=catalog,llm_gateway=gateway,vision_profile='testvision')
        self.addAsyncCleanup(self.library.close)
        self.vision = VisionWorker(self.database,model_catalog=catalog,llm_gateway=gateway,vision_profile='testvision')
        self.addAsyncCleanup(self.vision.close)
        self.usage = UsageStore(self.database)
        self.addCleanup(self.usage.close)
        self.app = SimpleNamespace(database=self.database,media_library=self.library,vision_worker=self.vision,usage_store=self.usage,
            db_call=self.calls,message_ledger=self.ledger,settings=SimpleNamespace(media_max_vision_bytes=1024*1024))
        self.worker = OfficialCaptionWorker(self.app)
        self.library.official_caption_worker = self.worker
        self.addresses = ('93.184.216.34',)
        async def dns(host, port):
            return self.addresses
        async def download(request):
            self.downloads.append(request)
            return httpx2.Response(200,content=png())
        self.worker.fetcher = PinnedContentReader(transport_factory=lambda url:
            PinnedMediaTransport(url,resolver=dns,transport=httpx2.MockTransport(download)))
        self.settings()
        self.clock = patch('time.time',return_value=ingress.NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def enqueue(self):
        result = self.process(self.image())
        return result.canonical_message_id

    async def run_job(self):
        jobs = await self.calls(self.library.claim_jobs)
        self.assertEqual(len(jobs),1)
        await self.library.process_job(jobs[0])
        return jobs[0]

    async def test_caption_reaches_history_without_changing_ir_or_creating_stickers(self):
        canonical = self.enqueue()
        original = self.query('SELECT body_json FROM messages WHERE canonical_message_id=?',(canonical,))[0]['body_json']
        await self.run_job()
        self.assertEqual(self.query('SELECT status,last_error FROM media_jobs')[0]['status'],'succeeded',self.query('SELECT last_error FROM media_jobs'))
        message = self.ledger.get_in_scope(self.endpoint.scope,canonical)
        self.assertIn('猫坐在窗边',message.prompt_text)
        self.assertIn('<untrusted',message.prompt_text)
        self.assertEqual(self.query('SELECT body_json FROM messages WHERE canonical_message_id=?',(canonical,))[0]['body_json'],original)
        self.assertEqual(len(self.ledger.history(self.endpoint.scope,terms=('猫',))),1)
        for messages in (self.ledger.history(self.endpoint.scope,terms=('猫',)),
                         self.ledger.recent_in_scope(self.endpoint.scope),
                         self.ledger.visible_messages_by_ids(self.endpoint.scope,[canonical]),
                         self.ledger.search_in_scope(self.endpoint.scope,'猫')):
            self.assertIn('猫坐在窗边',messages[0].prompt_text)
        self.assertEqual(self.query('SELECT * FROM sticker_library'),[])
        self.assertNotIn('TEST_SECRET',str(self.model_calls))
        usage = self.query("SELECT * FROM usage_events WHERE source='image_caption'")
        self.assertEqual([(r['input_tokens'],r['output_tokens']) for r in usage],[(12,8)])
        # An identical image is described only once, including across worker recreation.
        self.enqueue()
        new_worker = OfficialCaptionWorker(self.app)
        new_worker.fetcher = self.worker.fetcher
        self.library.official_caption_worker = new_worker
        await self.run_job()
        self.assertEqual(len(self.model_calls),1)
        self.assertEqual(len(self.query("SELECT * FROM media_jobs WHERE status='succeeded'")),2)
        cleanup = LegacyMediaCleanup(self.database,media_root=Path(self.temp.name))
        self.assertEqual(cleanup.preview()['candidate_count'],0)

    async def test_disable_during_model_call_preserves_cost_but_rejects_caption(self):
        self.enqueue()
        self.provider_action = lambda: CaptionSettings(self.database).set(self.endpoint.scope.key,enabled=False,daily_limit=200)
        await self.run_job()
        self.assertEqual(self.query('SELECT * FROM media_analysis'),[])
        self.assertEqual(len(self.query("SELECT * FROM usage_events WHERE source='image_caption'")),1)
        self.assertEqual(self.query('SELECT status FROM media_jobs')[0]['status'],'pending')

    async def test_unknown_provider_cost_is_not_refunded_and_quota_blocks_model(self):
        self.enqueue()
        def uncertain():
            raise RuntimeError('TEST uncertain provider TEST_SECRET')
        self.provider_action = uncertain
        await self.run_job()
        usage = self.query("SELECT * FROM usage_events WHERE source='image_caption_bound'")
        self.assertEqual(len(usage),1)
        self.assertGreater(usage[0]['input_tokens'],0)
        self.assertNotIn('TEST_SECRET',self.query('SELECT last_error FROM media_jobs')[0]['last_error'])
        self.usage.set_override(self.endpoint.scope.key,call_limit=1,input_limit=0,output_limit=0)
        self.query('UPDATE media_jobs SET next_attempt_at=0')
        await self.run_job()
        self.assertEqual(len(self.model_calls),1)
        self.assertEqual(self.query('SELECT status FROM media_jobs')[0]['status'],'failed')

    async def test_private_dns_never_reaches_http_or_model_and_retries_are_bounded(self):
        self.enqueue()
        self.addresses = ('127.0.0.1',)
        await self.run_job()
        self.query('UPDATE media_jobs SET next_attempt_at=0')
        await self.run_job()
        self.assertEqual(self.query('SELECT status FROM media_jobs')[0]['status'],'failed')
        self.assertEqual(self.query('SELECT fetch_status FROM message_media')[0]['fetch_status'],'failed')
        self.assertFalse(self.downloads)
        self.assertFalse(self.model_calls)

    async def test_stale_attempt_cannot_fail_new_owner_or_cross_scope(self):
        self.enqueue()
        old = (await self.calls(self.library.claim_jobs))[0]
        self.query('UPDATE media_jobs SET lease_until=0')
        new = (await self.calls(self.library.claim_jobs))[0]
        self.worker.fail(old,'TEST stale')
        self.assertEqual(self.query('SELECT status,attempts FROM media_jobs')[0]['status'],'running')
        self.assertGreater(new.attempts,old.attempts)
        # A forged association to another scope is rejected before any download.
        self.query("UPDATE message_media SET scope_key='TEST_OTHER_SCOPE'")
        await self.library.process_job(new)
        self.assertFalse(self.downloads)
        self.assertFalse(self.model_calls)
        self.assertEqual(self.query('SELECT status FROM media_jobs')[0]['status'],'failed')
