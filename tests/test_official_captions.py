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
from src.plugins.ai_chat.model_billing import ModelBilling
from src.plugins.ai_chat.official_caption_worker import OfficialCaptionWorker
from src.plugins.ai_chat.quota import UsageStore
from src.plugins.ai_chat.vision_worker import VisionWorker
from tests import test_image_captions as admission
from tests import qq_official_inbox_fixture as ingress
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
                if getattr(fixture, 'provider_content', None) is not None:
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                        content=json.dumps(fixture.provider_content)))],
                        usage=SimpleNamespace(prompt_tokens=12,completion_tokens=8))
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
            db_call=self.calls,message_ledger=self.ledger,settings=SimpleNamespace(media_max_vision_bytes=1024*1024),
            model_billing=ModelBilling(self.database))
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

    def sticker_event(self):
        inbox = self.image()
        with self.tx() as cursor:
            raw = json.loads(cursor.execute('SELECT payload_json FROM qq_official_inbound_events WHERE inbox_id=?', (inbox,)).fetchone()['payload_json'])
            raw['d']['content'] = '<faceType=6,faceId="0",ext="">'
            cursor.execute('UPDATE qq_official_inbound_events SET payload_json=? WHERE inbox_id=?', (json.dumps(raw),inbox))
        return inbox

    async def test_a_qq_sticker_is_tagged_into_the_library_with_captions_off(self):
        CaptionSettings(self.database).set(self.endpoint.scope.key, enabled=False, daily_limit=200)
        self.provider_content = {'summary':'戴墨镜的猫面无表情装酷', 'description':'动漫风格的猫戴着墨镜。',
            'text':'', 'subjects':['猫','墨镜'], 'actions':['凝视'], 'emotion':['酷','拽','装'],
            'usage':['别人炫耀时回怼'], 'is_sticker':True, 'contains_person':False,
            'contains_private_info':False, 'safety':'safe'}
        self.process(self.sticker_event())
        links = self.query('SELECT * FROM message_media')
        self.assertEqual([(l['media_kind'], l['fetch_status']) for l in links], [('sticker','pending')])
        await self.run_job()
        job = self.query('SELECT status,last_error FROM media_jobs WHERE job_type=?',('official_caption',))[0]
        self.assertEqual(job['status'],'succeeded',job['last_error'])
        library = self.query('SELECT * FROM sticker_library')
        self.assertEqual([(r['enabled'], r['banned']) for r in library], [(1, 0)])
        analysis = self.query('SELECT * FROM media_analysis')[0]
        self.assertEqual(json.loads(analysis['emotions_json']), ['酷','拽','装'])
        self.assertIn('表情包标注', self.model_calls[0]['messages'][0]['content'])
        # The same sticker sent again is not tagged twice.
        self.process(self.sticker_event())
        await self.run_job()
        self.assertEqual(len(self.model_calls), 1)
        self.assertEqual(len(self.query('SELECT * FROM sticker_library')), 1)

    async def test_photos_and_unsafe_stickers_stay_out_of_the_library(self):
        CaptionSettings(self.database).set(self.endpoint.scope.key, enabled=False, daily_limit=200)
        self.process(self.image())
        self.assertEqual(self.query('SELECT * FROM media_jobs'), [])
        self.provider_content = {'summary':'一个人的自拍照片', 'description':'真人自拍。', 'text':'',
            'subjects':['人'], 'actions':[], 'emotion':[], 'usage':[], 'is_sticker':True,
            'contains_person':True, 'contains_private_info':False, 'safety':'safe'}
        self.process(self.sticker_event())
        await self.run_job()
        self.assertEqual(self.query('SELECT * FROM sticker_library'), [])

    async def test_only_screenshots_and_photos_stay_out(self):
        CaptionSettings(self.database).set(self.endpoint.scope.key, enabled=False, daily_limit=200)
        # A crude meme the model calls "review" and "not a sticker" is still the owner's sticker.
        self.provider_content = {'summary':'琪露诺玩偶顶着井盖骂人', 'description':'玩偶配粗话。', 'text':'',
            'subjects':['琪露诺'], 'actions':['骂人'], 'emotion':['暴躁'], 'usage':['斗图'], 'kind':'sticker',
            'is_sticker':False, 'contains_person':False, 'contains_private_info':False, 'safety':'review'}
        self.process(self.sticker_event())
        await self.run_job()
        self.assertEqual([r['enabled'] for r in self.query('SELECT * FROM sticker_library')], [1])
        found = await self.library.search_stickers('骂人')
        self.assertEqual([item.summary for item in found], ['琪露诺玩偶顶着井盖骂人'])
        raw = json.loads(self.query('SELECT raw_response_json FROM media_analysis')[0]['raw_response_json'])
        self.assertEqual((raw['model_is_sticker'], raw['is_sticker']), (False, True))

    def test_admission_rule(self):
        admits = MediaLibrary.admits_sticker
        self.assertTrue(admits({'kind': 'sticker', 'safety': 'blocked', 'contains_person': True}))
        self.assertFalse(admits({'kind': 'screenshot', 'is_sticker': True}))
        self.assertFalse(admits({'kind': 'photo', 'is_sticker': True}))
        # Replies from before the kind field keep the old verdict.
        self.assertTrue(admits({'is_sticker': True}))
        self.assertFalse(admits({'is_sticker': True, 'contains_private_info': True}))

    def test_sticker_collection_can_be_turned_off(self):
        CaptionSettings(self.database).set(self.endpoint.scope.key, enabled=False, daily_limit=200)
        with patch.dict('os.environ', {'AI_STICKER_COLLECTION': 'false'}):
            self.process(self.sticker_event())
        self.assertEqual(self.query('SELECT * FROM media_jobs'), [])

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
                         self.ledger.visible_messages_by_ids(self.endpoint.scope,[canonical])):
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
        bill = self.query('SELECT * FROM model_usage_calls')
        self.assertEqual(len(bill),1)
        self.assertEqual((bill[0]['scope_key'],bill[0]['purpose'],bill[0]['input_tokens']),
            (self.endpoint.scope.key,'image_caption',12))
        self.assertTrue(bill[0]['actor_key'])
        self.assertFalse(bill[0]['cost_complete'])
        cleanup = LegacyMediaCleanup(self.database,media_root=Path(self.temp.name))
        self.assertEqual(cleanup.preview()['candidate_count'],0)

    async def test_history_alias_caption_uses_physical_settings_not_history_permissions(self):
        from src.plugins.ai_chat.platform_types import EndpointKey
        other = EndpointKey(self.account,'group','TEST_OTHER_HISTORY')
        self.provision(other,binding='revoked')
        self.query('UPDATE qq_official_endpoints SET canonical_scope_key=?,route_epoch=2 WHERE endpoint_key=?',
            (other.scope.key,self.endpoint.key))
        canonical = self.process(self.image(route=2)).canonical_message_id
        await self.run_job()
        self.assertEqual(self.query('SELECT status FROM media_jobs')[0]['status'],'succeeded')
        self.assertEqual(len(self.model_calls),1)
        self.assertIn('猫坐在窗边',self.ledger.get_in_scope(other.scope,canonical).prompt_text)
        rows = {row['scope_key']:row for row in CaptionSettings(self.database).snapshot(ingress.NOW)}
        self.assertEqual(rows[self.endpoint.scope.key]['admitted'],1)
        self.assertEqual(rows[other.scope.key]['admitted'],0)
        self.assertFalse(rows[other.scope.key]['image_captions'])

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
