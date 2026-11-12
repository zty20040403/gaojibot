"""Real job/permission/usage/history transactions; only VM and model are doubles."""
import asyncio
from dataclasses import replace
import json
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official.background_agents import OfficialBackgroundAgents
from src.plugins.ai_chat.adapters.qq_official.media_refs import MediaDenied
from src.plugins.ai_chat.config import settings
from src.plugins.ai_chat.message_ir import MessageBody, MediaNode
from src.plugins.ai_chat.model_catalog import ModelCatalog, ModelCapabilities, ModelProfile
from src.plugins.ai_chat.llm_gateway import LLMGateway
from src.plugins.ai_chat.quota import UsageStore
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from tests.qq_official_turn_fixture import ExecutionFixture
from tests.test_video_format import metadata_clip


class BackgroundVideoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        f = self.f = ExecutionFixture('runTest'); f.setUp(); self.addCleanup(f.doCleanups)
        self.calls = DatabaseCalls(); self.addAsyncCleanup(self.calls.close)
        self.usage = UsageStore(f.database); self.addCleanup(self.usage.close)
        self.model_calls, self.provider_action = [], None
        test = self
        class Provider:
            async def create_completion(self,profile,**kwargs):
                test.model_calls.append(kwargs)
                if test.provider_action:
                    test.provider_action()
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='TEST 猫跳上窗台，听到音乐'))],
                    usage=SimpleNamespace(prompt_tokens=12,completion_tokens=8))
        profile = ModelProfile('testvideo','openai','openai-chat','TEST_MODEL','https://example.invalid',
            api_key='TEST_KEY',max_input_tokens=4096,max_output_tokens=256,capabilities=ModelCapabilities(video=True))
        runtime = SimpleNamespace(store=SimpleNamespace(transaction=f.tx,admitted=f.admission,clock=lambda:f.now),
            jobs=f.jobs,_calls=self.calls)
        self.manager = SimpleNamespace(ensure_default=AsyncMock(return_value={'sandbox_id':'TEST_VM'}),
            read_file=AsyncMock(return_value=metadata_clip()))
        self.app = SimpleNamespace(settings=replace(settings,video_profile='testvideo',search_enabled=False),
            logger=logging.getLogger(__name__),db_call=self.calls,message_ledger=f.ledger,usage_store=self.usage,
            model_preferences=SimpleNamespace(get_explicit=lambda _:None),sandbox_manager=self.manager,
            model_catalog=ModelCatalog({'testvideo':profile},default_profile='testvideo'),
            llm_gateway=LLMGateway(providers={'openai-chat':Provider()},fallback_enabled=False))
        self.service = OfficialBackgroundAgents(runtime,f.token,self.app)
        self.addAsyncCleanup(self.service.close)
        source = dict(account_key=f.account.key,endpoint_key=f.endpoint.key,actor_key=f.actor.key,scope_key=f.endpoint.scope.key,
            source_execution_id=None,canonical_message_id=f.message.canonical_message_id,native_message_id='TEST_INPUT',
            endpoint_generation=1,route_epoch=1)
        with f.tx() as cursor:
            row = self.service.store.submit(cursor,kind=self.service.kind,key='TEST_VIDEO_JOB',source=source,
                task='TEST summarize video',title='TEST video',profile='testvideo')
        self.job = f.jobs.claim_due('TEST_VIDEO_WORKER',kinds=(self.service.kind,),now=f.now)[0]
        self.service.store.update(self.job.job_id,state='running')
        self.session = await self.service.session(row,self.job,AsyncMock())
        self.message = f.ledger.record_message(f.endpoint.scope,native_message_id='TEST_VIDEO',sender_native_user_id=f.actor.native_user_id,
            sender_display='TEST member',body=MessageBody((MediaNode(0,'video',name='TEST.mp4'),)),
            occurred_at=f.now,actor=f.actor,source_endpoint=f.endpoint)
        self.arguments = {'message_handle':f'video#{self.message.canonical_message_id}.0','path':'/work/TEST-prepared.mp4'}

    async def test_video_and_frames_reach_search_without_changing_original_body(self):
        original = self.f.query('SELECT body_json FROM messages WHERE canonical_message_id=?',(self.message.canonical_message_id,))[0]
        result = json.loads(await self.session.execute_tool('analyze_video',self.arguments))
        self.assertTrue(result['stored'])
        self.assertEqual(result['seconds'],3)
        self.assertIn('猫跳上窗台',self.f.ledger.history(self.f.endpoint.scope,terms=('猫',))[0].prompt_text)
        self.assertIn('<untrusted',self.f.ledger.get_in_scope(self.f.endpoint.scope,self.message.canonical_message_id).prompt_text)
        self.assertEqual(original,self.f.query('SELECT body_json FROM messages WHERE canonical_message_id=?',(self.message.canonical_message_id,))[0])
        usage = self.f.query("SELECT * FROM usage_events WHERE source='video_analysis'")[0]
        self.assertEqual((usage['input_tokens'],usage['output_tokens'],usage['background_job_id']),(12,8,self.job.job_id))
        self.assertNotIn('TEST_KEY',str(self.manager.read_file.await_args))
        self.assertEqual(self.model_calls[0]['messages'][-1]['content'][-1]['type'],'video_url')
        await self.session.execute_tool('analyze_video',self.arguments)
        self.assertEqual(len(self.model_calls),1)
        await self.session.execute_tool('record_video_summary',{'message_handle':self.arguments['message_handle'],'summary':'TEST 抽帧见猫，音轨无法转写'})
        self.assertEqual(self.f.query('SELECT method FROM message_media_notes')[0]['method'],'frames')
        self.assertEqual(len(self.f.ledger.history(self.f.endpoint.scope,terms=('无法转写',))),1)

    async def test_cancel_during_model_call_saves_cost_not_conclusion(self):
        self.provider_action = lambda: self.service.store.cancel(self.job.job_id,self.f.endpoint.key,self.f.actor.key)
        with self.assertRaises(asyncio.CancelledError):
            await self.session.execute_tool('analyze_video',self.arguments)
        self.assertFalse(self.f.query('SELECT * FROM message_media_notes'))
        self.assertEqual(self.f.query("SELECT input_tokens FROM usage_events WHERE source='video_analysis'")[0]['input_tokens'],12)

    async def test_unknown_cost_is_not_refunded_and_quota_prevents_retry(self):
        def uncertain():
            raise RuntimeError('TEST uncertain TEST_SECRET')
        self.provider_action = uncertain
        with self.assertRaises(MediaDenied) as failed:
            await self.session.execute_tool('analyze_video',self.arguments)
        self.assertNotIn('TEST_SECRET',str(failed.exception))
        self.assertGreater(self.f.query("SELECT input_tokens FROM usage_events WHERE source='video_analysis_bound'")[0]['input_tokens'],0)
        self.usage.set_override(self.f.endpoint.scope.key,call_limit=1,input_limit=0,output_limit=0)
        with self.assertRaisesRegex(MediaDenied,'quota'):
            await self.session.execute_tool('analyze_video',self.arguments)
        self.assertEqual(len(self.model_calls),1)

    async def test_hidden_source_and_expired_job_cannot_write_or_call_model(self):
        self.f.ledger.hide_history(self.f.endpoint.scope)
        with self.assertRaises(MediaDenied):
            await self.session.execute_tool('analyze_video',self.arguments)
        self.f.query('UPDATE durable_jobs SET lease_until=0 WHERE job_id=?',(self.job.job_id,))
        with self.assertRaises(asyncio.CancelledError):
            await self.session.execute_tool('record_video_summary',{'message_handle':self.arguments['message_handle'],'summary':'TEST no'})
        self.assertFalse(self.model_calls)
        self.assertFalse(self.f.query('SELECT * FROM message_media_notes'))

    async def test_foreign_endpoint_in_same_canonical_scope_is_rejected(self):
        endpoint = EndpointKey(self.f.account,'group','TEST_FOREIGN')
        actor = ActorKey(self.f.account,'group_member','TEST_FOREIGN','TEST_MEMBER')
        self.f.provision(endpoint)
        message = self.f.ledger.record_message(self.f.endpoint.scope,native_message_id='TEST_FOREIGN_VIDEO',
            sender_native_user_id=actor.native_user_id,sender_display='TEST foreign',
            body=MessageBody((MediaNode(0,'video',name='TEST.mp4'),)),actor=actor,source_endpoint=endpoint)
        with self.assertRaises(MediaDenied):
            await self.session.execute_tool('record_video_summary',{'message_handle':f'video#{message.canonical_message_id}.0','summary':'TEST forbidden'})
        self.assertFalse(self.f.query('SELECT * FROM message_media_notes'))

    async def test_optional_video_model_and_host_metadata_limits(self):
        self.app.settings = replace(self.app.settings,video_profile='')
        with self.assertRaisesRegex(ValueError,'not_configured'):
            await self.session.execute_tool('analyze_video',self.arguments)
        await self.session.execute_tool('record_video_summary',{'message_handle':self.arguments['message_handle'],'summary':'TEST frames only'})
        self.app.settings = replace(self.app.settings,video_profile='testvideo')
        self.manager.read_file.return_value = metadata_clip(601)
        with self.assertRaisesRegex(ValueError,'duration_limit'):
            await self.session.execute_tool('analyze_video',self.arguments)
        self.assertFalse(self.model_calls)
