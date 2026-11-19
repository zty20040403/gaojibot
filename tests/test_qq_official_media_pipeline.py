"""M7 candidate actual host factory -> M4 -> tools -> M3, isolated PG only."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aiohttp import web
import httpx2
from src.plugins.ai_chat.adapters.qq_official.host_config import HostConfiguration
from src.plugins.ai_chat.adapters.qq_official.content_reader import PinnedContentReader, PinnedMediaTransport
from src.plugins.ai_chat.adapters.qq_official.media_runtime import MediaSession
from src.plugins.ai_chat.adapters.qq_official.native_background_factory import ProactiveConfiguration, background_factory
from src.plugins.ai_chat.conversation_config import change
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.llm_gateway import LLMGateway
from src.plugins.ai_chat.media_library import MediaLibrary
from src.plugins.ai_chat.model_catalog import ModelCapabilities, ModelProfile, ModelCatalog
from src.plugins.ai_chat.model_billing import ModelBilling
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey
from src.plugins.ai_chat.vision_worker import VisionWorker
from tests import test_qq_official_turn_endtoend as turn_fixture
from tests import test_qq_official_runtime as runtime_fixture
from tests.qq_official_file_http_fixture import FileMediaFake
from tests.test_qq_official_image_formats import png


class PipelineFake(FileMediaFake):
    async def start(self):
        # Compose the original M0 WebSocket behavior with the existing M6 fake
        # upload/message routes. No arbitrary target or URL is proxied.
        original=web.AppRunner
        def runner(app,*args,**kwargs):
            app.router.add_get('/ws',self._websocket)
            return original(app,*args,**kwargs)
        with patch('aiohttp.web.AppRunner',new=runner):
            await super().start()
        self.gateway_url=self.base_url.replace('http://','ws://')+'/ws'


@asynccontextmanager
async def platform():
    original=web.WebSocketResponse.send_json
    async def write(socket,data,*args,**kwargs):
        if isinstance(data,dict) and data.get('t')=='READY':
            data={**data,'d':{**data['d'],'user':{**data['d']['user'],'bot':True}}}
        return await original(socket,data,*args,**kwargs)
    with patch.object(web.WebSocketResponse,'send_json',new=write):
        async with PipelineFake() as fake:yield fake


class MediaPipelineTests(turn_fixture.OfficialTurnFixture, unittest.IsolatedAsyncioTestCase):
    async def start(self,fake,*,flags,images=False,unknown_vision=False,frontend_vision=False,background=None):
        self.app=self.application()
        profile = self.app.model_catalog.resolve('test')
        self.app.model_catalog = ModelCatalog({'test': replace(profile,
            capabilities=replace(profile.capabilities, vision=frontend_vision))}, default_profile='test')
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.app.database=self.database
        self.app.model_billing=ModelBilling(self.database)
        self.app.state_dir=Path(self.temp.name)
        self.app.project_runtime=None;self.app.sandbox_manager=None
        self.app.delivery_store=DeliveryStore(self.database,recover_interrupted=False)
        self.addCleanup(self.app.delivery_store.close)
        self.app.media_library=None
        self.vision_calls=[]
        fixture=self
        class Provider:
            async def create_completion(self,profile,**kwargs):
                fixture.vision_calls.append(kwargs)
                await asyncio.sleep(0.02)
                if unknown_vision:raise RuntimeError('TEST uncertain provider response')
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                    'summary':'TEST image','description':'TEST image','text':'TEST OCR content',
                    'observations':[],'safety':'safe'})))],
                    usage=SimpleNamespace(prompt_tokens=12,completion_tokens=8))
        self.vision_profile=ModelProfile('testvision','openai','openai-chat','TEST_VISION',
            'https://model.example.invalid',api_key='TEST_ONLY',max_input_tokens=4096,max_output_tokens=256,
            capabilities=ModelCapabilities(vision=True))
        gateway=LLMGateway(providers={'openai-chat':Provider()},fallback_enabled=False)
        worker=object.__new__(VisionWorker)
        worker.model_catalog=ModelCatalog({'testvision':self.vision_profile},default_profile='testvision')
        worker.vision_profile_name='testvision';worker.llm_gateway=gateway
        worker.max_vision_bytes=worker.prepare_threshold_bytes=1048576
        worker.max_edge_pixels, worker.timeout_seconds, worker.ffmpeg_path = 1568, 5, 'ffmpeg'
        self.app.vision_worker=worker
        account=AccountKey('qq-official',fake.app_id,'test')
        endpoint=EndpointKey(account,'private','TEST_RUNTIME_OWNER')
        raw={'version':1,'account':account.key,'onebot_accounts':[{
            'account':AccountKey('onebot-v11','713005','production').key,'deployment_digest':'f'*64}],
            'media':flags}
        if images:
            raw['files']={'private_directory':str(Path(self.temp.name)/'files'),'min_free_bytes':0,
                'native_routes':[dict(endpoint=endpoint.key,endpoint_generation=1,route_epoch=1,
                    capability_revision=1,max_bytes=3500000,verified_at=int(time.time())-1,
                    expires_at=int(time.time())+120,contract='unused-legacy-contract',file_type=1)]}
        path=Path(self.temp.name)/'host.json';path.touch(mode=0o600);path.write_text(json.dumps(raw))
        config=HostConfiguration.from_env({'AI_QQ_MIGRATION_ENABLED':'true','AI_QQ_MIGRATION_CONFIG_FILE':str(path)})
        self.migration=config.build(self.app,SimpleNamespace(handlers=SimpleNamespace(
            chat=SimpleNamespace(_conversation_scope=lambda event:None))))
        self.addAsyncCleanup(self.migration.close)
        self.host=self.app.official_host
        self.runtime_instance=self.runtime(loaded=runtime_fixture.loaded_config(fake.app_id,base=fake.base_url),
            application=self.app,ledger=self.app.message_ledger,journal=self.app.turn_journal,
            migration_factory=self.host.migration_factory,media_factory=self.host.media_factory,
            file_services_factory=self.host.files_factory if images else None, background_factory=background)
        self.assertTrue(await self.runtime_instance.start())
        await asyncio.wait_for(fake.wait_ready(),5)
        await self.wait_db("SELECT bot_native_id FROM qq_official_accounts WHERE bot_identity_source='ready'")
        self.assertIsNotNone(self.runtime_instance.media_service)
        self.downloads=[]
        async def dns(host,port):return ('93.184.216.34',)
        async def fetch(request):
            self.downloads.append(str(request.url))
            if request.url.path.endswith('/doc'):
                return httpx2.Response(200,content=b'TEST external document',headers={'content-type':'text/plain'})
            return httpx2.Response(200,content=png(),headers={'content-type':'image/png'})
        self.runtime_instance.media_service.fetcher=PinnedContentReader(transport_factory=lambda url:
            PinnedMediaTransport(url,resolver=dns,transport=httpx2.MockTransport(fetch)))
        self.runtime_instance.media_service.image_fetcher = self.runtime_instance.media_service.fetcher
        return self.runtime_instance

    async def emit(self,fake,native,**extra):
        await fake.emit('C2C_MESSAGE_CREATE',{'id':native,'content':'TEST media request',
            'timestamp':datetime.now(timezone.utc).isoformat(),'author':{'user_openid':'TEST_RUNTIME_OWNER'},
            'message_scene':{'ext':{'msg_idx':'TEST_INDEX_'+native}},**extra})

    def completion(self,steps):
        index=0
        self.model_calls=[]
        async def complete(**kwargs):
            nonlocal index
            self.model_calls.append(deepcopy(kwargs))
            if index<len(steps):
                name,args=steps[index];index+=1
                self.assertIn(name,{t['function']['name'] for t in kwargs['tools']})
                calls=[SimpleNamespace(id='TEST_CALL_'+str(index),type='function',
                    function=SimpleNamespace(name=name,arguments=json.dumps(args)))]
                content=None
            else: calls=[];content='TEST final'
            return turn_fixture.model_response(content,calls,kwargs)
        return complete


    async def test_real_ocr_document_tools_charge_usage_and_original_model_gateway(self):
        complete=self.completion([('view_image',{}),('read_attachment',{})])
        with patch('src.plugins.ai_chat.agent_loop._create_completion',new=complete):
            async with platform() as fake:
                runtime=await self.start(fake,flags=dict(ocr=True,documents=True))
                await self.emit(fake,'TEST_MEDIA_OCR',content='TEST read https://example.invalid/doc',attachments=[
                    {'url':'https://example.invalid/image?signature=TEST_SECRET','content_type':'image/png'}])
                await self.complete('TEST_MEDIA_OCR')
                self.assertEqual(len(self.vision_calls),1)
                self.assertIn('TEST OCR content',str(self.model_calls[-1]['messages']))
                self.assertIn('TEST image',str(self.model_calls[-1]['messages']))
                self.assertIn('[image#', str(self.model_calls[0]['messages']))
                self.assertNotIn('read_image_text', str(self.model_calls[0]['tools']))
                self.assertIn('TEST external document',str(self.model_calls[-1]['messages']))
                self.assertNotIn('signature=TEST_SECRET',str(self.vision_calls))
                usage=self.query("SELECT * FROM usage_events WHERE source='official_ocr'")
                bill=self.query('SELECT * FROM model_usage_calls')
                self.assertEqual(len(bill),1)
                self.assertEqual((bill[0]['purpose'],bill[0]['input_tokens']),('ocr',12))
                self.assertTrue(bill[0]['turn_id'])
                self.assertTrue(bill[0]['actor_key'])
                self.assertEqual([(v['input_tokens'],v['output_tokens']) for v in usage],[(12,8)])
                self.assertEqual(len(fake.received),1)
                await runtime.stop()

    async def test_concurrent_ocr_reservations_preserve_unknown_charge_and_original_deadline(self):
        original=MediaSession.ocr
        results=[]
        async def concurrent(session,arguments):
            # Both calls are real source-bound OCR requests. Only this test
            # schedules them concurrently at the existing handler boundary.
            if results:return await original(session,arguments)
            values=await asyncio.gather(original(session,arguments),original(session,arguments),return_exceptions=True)
            results.extend(values)
            raise values[0] if isinstance(values[0],BaseException) else values[1]
        complete=self.completion([('view_image',{})])
        with patch('src.plugins.ai_chat.agent_loop._create_completion',new=complete),patch.object(MediaSession,'ocr',new=concurrent):
            async with platform() as fake:
                runtime=await self.start(fake,flags=dict(ocr=True),unknown_vision=True)
                endpoint=EndpointKey(runtime.loaded.account,'private','TEST_RUNTIME_OWNER')
                self.app.usage_store.set_override(endpoint.scope.key,call_limit=1,input_limit=0,output_limit=0)
                await self.emit(fake,'TEST_MEDIA_QUOTA',attachments=[{'url':'https://example.invalid/image','content_type':'image/png'}])
                await self.complete('TEST_MEDIA_QUOTA')
                self.assertEqual(len(self.vision_calls),1)
                usage=self.query("SELECT * FROM usage_events WHERE source='official_ocr_bound'")
                self.assertEqual(len(usage),1);self.assertGreater(usage[0]['input_tokens'],0)
                self.assertGreater(usage[0]['output_tokens'],0)
                self.assertTrue(any('ocr_quota_exhausted' in str(value) for value in results))
                self.assertEqual(len(self.query('SELECT * FROM qq_official_turn_executions')),1)
                await runtime.stop()

    async def test_media_and_mid_turn_followup_render_canonical_handles_and_read_previous_image(self):
        base_complete = self.completion([('view_image', {})])
        first_entered = asyncio.Event()
        second_emitted = asyncio.Event()

        async def complete(**kwargs):
            if not self.model_calls:
                first_entered.set()
                await second_emitted.wait()
            return await base_complete(**kwargs)

        with patch('src.plugins.ai_chat.agent_loop._create_completion', new=complete):
            async with platform() as fake:
                runtime = await self.start(fake, flags=dict(ocr=True))
                await self.emit(fake, 'TEST_MEDIA_BATCH', content='', attachments=[
                    {'url': 'https://example.invalid/image?signature=TEST_SECRET', 'content_type': 'image/png'},
                    {'url': 'https://example.invalid/video', 'content_type': 'video/mp4'},
                    {'url': 'https://example.invalid/audio', 'content_type': 'audio/wav'},
                    {'url': 'https://example.invalid/file', 'content_type': 'application/pdf'}])
                await asyncio.wait_for(first_entered.wait(), 5)
                await self.emit(fake, 'TEST_MEDIA_QUESTION', content='TEST 这是啥')
                for _ in range(30):
                    st = next(iter(runtime.active_conversation_turns.values()), None)
                    if st is not None and st.pending:
                        break
                    await asyncio.sleep(0.02)
                second_emitted.set()
                await self.complete('TEST_MEDIA_BATCH')
                first_msg = json.dumps(self.model_calls[0]['messages'], ensure_ascii=False)
                second_msg = json.dumps(self.model_calls[1]['messages'], ensure_ascii=False)
                for kind in ('image', 'video', 'voice', 'file'):
                    self.assertIn(f'[{kind}#', first_msg)
                self.assertNotIn('TEST_SECRET', first_msg)
                self.assertIn('TEST 这是啥', second_msg)
                self.assertEqual(len(self.vision_calls), 1)
                self.assertEqual(len(fake.received), 1)
                await runtime.stop()

    async def test_vision_frontend_gets_image_and_tool_image_without_caption_call(self):
        complete = self.completion([('view_image', {})])
        with patch('src.plugins.ai_chat.agent_loop._create_completion', new=complete):
            async with platform() as fake:
                runtime = await self.start(fake, flags=dict(ocr=True), frontend_vision=True)
                await self.emit(fake, 'TEST_DIRECT_IMAGE', attachments=[
                    {'url': 'https://example.invalid/image?signature=TEST_SECRET', 'content_type': 'image/png'}])
                await self.complete('TEST_DIRECT_IMAGE')
                first = self.model_calls[0]['messages'][-1]['content']
                self.assertIsInstance(first, list)
                self.assertEqual(sum(p['type'] == 'image_url' for p in first), 1)
                last = self.model_calls[-1]['messages'][-1]['content']
                self.assertEqual(sum(p['type'] == 'image_url' for p in last), 1)
                self.assertIn('<untrusted', str(first))
                self.assertNotIn('TEST_SECRET', str(self.model_calls))
                self.assertEqual(len(self.vision_calls), 0)
                self.assertEqual(len(self.downloads), 1)
                await runtime.stop()

    async def seed_sticker(self):
        root=Path(self.temp.name)/'library'
        library=MediaLibrary(self.database,root=root,model_catalog=self.app.vision_worker.model_catalog,
            llm_gateway=self.app.vision_worker.llm_gateway,vision_profile='testvision')
        self.app.media_library=library
        data=png();digest=hashlib.sha256(data).hexdigest();(root/'blobs'/'test.png').write_bytes(data)
        with self.app.delivery_store.transaction() as cursor:
            media=cursor.execute('''INSERT INTO media_blobs(sha256,mime_type,byte_size,storage_path,first_seen_at,last_seen_at)
                VALUES (?,'image/png',?,'blobs/test.png',?,?) RETURNING media_id''',(digest,len(data),int(time.time()),int(time.time()))).fetchone()['media_id']
            cursor.execute('''INSERT INTO media_analysis(media_id,vision_profile,vision_model,summary,description,
                is_sticker,raw_response_json,content_hash,analyzed_at,subjects_json)
                VALUES (?,'testvision','TEST_VISION','TEST sticker','TEST sticker',1,'{}',?,?,'["TEST"]')''',
                (media,digest,int(time.time())))
            cursor.execute('INSERT INTO sticker_library(media_id,created_at,updated_at) VALUES (?,?,?)',
                           (media,int(time.time()),int(time.time())))
        return media

    async def test_group_attention_sends_one_curated_image_without_a_chat_turn(self):
        decision = {'action': 'sticker', 'reason': 'TEST', 'classification_reason': '表情足以回应喜讯'}
        with patch('src.plugins.ai_chat.application.shared_proactive.ask_deepseek_json', return_value=decision) as scorer, \
                patch('src.plugins.ai_chat.agent_loop._create_completion') as chat:
            async with platform() as fake:
                runtime = await self.start(fake, flags={'stickers': True}, images=True,
                    background=background_factory(proactive_configuration=ProactiveConfiguration(enabled=True),
                                                  proactive_profile='test'))
                self.addAsyncCleanup(runtime.stop)
                async def emit(native, text):
                    await fake.emit('GROUP_MESSAGE_CREATE', {'id': native, 'content': text,
                        'timestamp': datetime.now(timezone.utc).isoformat(), 'group_openid': 'TEST_GROUP',
                        'author': {'member_openid': 'TEST_MEMBER'}})
                await emit('TEST_INIT', 'hi')
                endpoint = EndpointKey(runtime.loaded.account, 'group', 'TEST_GROUP')
                await self.wait_db('SELECT inbox_id FROM qq_official_inbound_events WHERE native_message_id=?', ('TEST_INIT',))
                with runtime.store.transaction() as cursor:
                    cursor.execute("UPDATE qq_official_endpoints SET binding_state='active' WHERE endpoint_key=?", (endpoint.key,))
                fake.proactive_allowed[('group', 'TEST_GROUP')] = True
                await runtime.sender.refresh_bound_groups()
                await emit('TEST_MISSING', 'TEST 有个好消息！')
                await self.wait_db("SELECT inbox_id FROM qq_official_proactive_observations WHERE state='discarded' AND decision_reason='表情足以回应喜讯'")
                self.assertFalse(fake.received)
                await self.seed_sticker()
                fake.files_release = asyncio.Event()
                await emit('TEST_CANCEL', 'TEST 又过了一次验收！')
                await asyncio.wait_for(fake.files_seen.wait(), 12)
                with runtime.store.transaction() as cursor:
                    change(cursor, endpoint, 'proactive_enabled', False, actor='TEST_ADMIN', now=int(time.time()))
                fake.files_release.set()
                await self.wait_db("""SELECT inbox_id FROM qq_official_proactive_observations
                    WHERE source_native_message_id='TEST_CANCEL' AND state='discarded' AND plan_id IS NULL""")
                self.assertFalse(fake.received)
                with runtime.store.transaction() as cursor:
                    change(cursor, endpoint, 'proactive_enabled', True, actor='TEST_ADMIN', now=int(time.time()))
                await emit('TEST_CELEBRATE', 'TEST 项目验收通过啦！')
                await self.wait_db("""SELECT d.delivery_id FROM deliveries d JOIN qq_official_proactive_observations o USING(plan_id)
                    WHERE d.purpose='unsolicited' AND d.status='committed' AND o.state='queued' AND o.decision_reason='表情足以回应喜讯'""")
                self.assertEqual(fake.uploaded['groups', 'TEST_GROUP'], png())
                self.assertTrue(all(x[3]['file_type'] == 1 for x in fake.media_calls))
                self.assertEqual([x['payload']['msg_type'] for x in fake.received], [7])
                entered, release = asyncio.Event(), asyncio.Event()
                async def hold_send(endpoint):
                    entered.set()
                    await release.wait()
                with patch.object(runtime.sender.rate_gate, 'acquire', new=hold_send):
                    await emit('TEST_QUEUED', 'TEST 我终于毕业啦！')
                    await asyncio.wait_for(entered.wait(), 12)
                    with runtime.store.transaction() as cursor:
                        change(cursor, endpoint, 'proactive_enabled', False, actor='TEST_ADMIN', now=int(time.time()))
                        change(cursor, endpoint, 'proactive_enabled', True, actor='TEST_ADMIN', now=int(time.time()))
                    release.set()
                    await self.wait_db("""SELECT delivery_id FROM deliveries JOIN qq_official_proactive_observations USING(plan_id)
                        WHERE source_native_message_id='TEST_QUEUED' AND deliveries.status='cancelled'""")
                self.assertEqual(len(fake.received), 1)
                self.assertEqual(scorer.call_count, 4)
                chat.assert_not_called()
                self.assertFalse(self.query('SELECT execution_id FROM qq_official_turn_executions'))
                await runtime.stop()

    async def test_voice_uses_native_audio_and_sends_once_per_turn(self):
        complete=self.completion([('reply_with_voice',{'text':'TEST voice'}),
                                  ('reply_with_voice',{'text':'TEST duplicate'})])
        with patch('src.plugins.ai_chat.agent_loop._create_completion',new=complete), \
                patch('src.plugins.ai_chat.voice_reply.synthesize',return_value=b'\x02#!SILK_V3TEST') as synthesize:
            async with platform() as fake:
                runtime=await self.start(fake,flags=dict(asr=True),images=True)
                await self.emit(fake,'TEST_MEDIA_VOICE')
                await self.complete('TEST_MEDIA_VOICE')
                await self.wait_db("SELECT delivery_id FROM deliveries WHERE purpose='requested_artifact' AND status='committed'")
                self.assertEqual(synthesize.call_count,1)
                self.assertEqual([v[3]['file_type'] for v in fake.media_calls],[3])
                self.assertEqual(sum(v['payload']['msg_type']==7 for v in fake.received),1)
                self.assertIn('audio',self.query("SELECT body_json FROM deliveries WHERE body_json LIKE ?",('%audio/silk%',))[0]['body_json'])
                await runtime.stop()

    async def test_real_curated_sticker_shared_tool_to_m6_type_one_type_seven_receipt(self):
        steps=[('find_stickers',{'query':'TEST'}),('send_sticker',{'media_handle':'media#1'})]
        complete=self.completion(steps)
        with patch('src.plugins.ai_chat.agent_loop._create_completion',new=complete):
            async with platform() as fake:
                runtime=await self.start(fake,flags=dict(stickers=True),images=True)
                media=await self.seed_sticker();steps[1]=('send_sticker',{'media_handle':f'media#{media}'})
                await self.emit(fake,'TEST_MEDIA_STICKER')
                await self.complete('TEST_MEDIA_STICKER')
                await self.wait_db("SELECT delivery_id FROM deliveries WHERE purpose='requested_artifact' AND status='committed'")
                self.assertEqual(len(fake.media_calls),1)
                self.assertEqual(fake.media_calls[0][3]['file_type'],1)
                self.assertEqual(fake.uploaded[('users','TEST_RUNTIME_OWNER')],png())
                self.assertEqual(sum(v['payload']['msg_type']==7 for v in fake.received),1)
                self.assertEqual(len(self.query('SELECT * FROM delivery_receipts WHERE applied_at IS NOT NULL')),2)
                # The sticker is queued after the reply text, so QQ receives the words first.
                order=[row['purpose'] for row in self.query('SELECT purpose FROM deliveries ORDER BY delivery_id')]
                self.assertLess(order.index('final'),order.index('requested_artifact'))
                self.assertEqual([v['payload']['msg_type'] for v in fake.received][-1],7)
                # The conversation remembers it, so the same sticker goes last next time.
                scope=self.query("SELECT source_scope_key FROM deliveries WHERE purpose='requested_artifact'")[0]['source_scope_key']
                self.assertIn(media,self.app.media_library.recent_sticker_sends(scope,0))
                # Counted so the least-sent stickers come first next time.
                self.assertEqual(self.query('SELECT times_sent FROM sticker_library')[0]['times_sent'],1)
                await runtime.stop()
