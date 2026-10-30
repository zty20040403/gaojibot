import asyncio
from datetime import datetime,timezone
import json
from types import SimpleNamespace
from unittest.mock import patch

from tests.qq_official_native_schedule_fixture import NativeFixture
from src.plugins.ai_chat.adapters.qq_official.native_background_factory import background_factory
from src.plugins.ai_chat.tools.native_reminders import execute as reminder_tool
from src.plugins.ai_chat.adapters.qq_official.migration_types import MigrationDenied
from src.plugins.ai_chat.model_budget import active_model_budget
from src.plugins.ai_chat.model_catalog import ModelProfile


class FactoryPGTests(NativeFixture):
    def make_runtime(self):
        profile=ModelProfile(name='TEST',provider='test',protocol='openai',model='TEST',base_url='http://127.0.0.1',api_key_required=False,max_output_tokens=1024)
        self.profile=profile
        return SimpleNamespace(application=SimpleNamespace(usage_store=self.usage,model_catalog=SimpleNamespace(resolve=lambda name:profile)),
            database=self.database,jobs=self.jobs,outbox=self.outbox,store=SimpleNamespace(admitted=self.admission,clock=lambda:self.now,transaction=self.tx),
            _calls=asyncio.to_thread,migration_service=None)

    def create_service(self,**kwargs):
        runtime=self.make_runtime()
        return asyncio.run(background_factory(schedule_configuration=self.policy.configuration,schedule_profile='TEST',**kwargs)(runtime,self.token))

    def platform(self):
        return SimpleNamespace(execution=SimpleNamespace(handle=self.handle,job=self.job,token=self.token))

    def test_actual_factory_command_creates_then_shared_model_settles_budget(self):
        service=self.create_service()
        life=SimpleNamespace(handle=self.handle,job=self.job,token=self.token)
        command=service.bind(life=life,inbound=self.source.inbound,platform=self.platform())['routine']
        result=asyncio.run(command(self.source.inbound,SimpleNamespace(argument='添加 测试 | * * * * * | summarize TEST')))
        self.assertIn('已保存',result.text)
        self.assertEqual(len(self.query('SELECT * FROM agent_routines')),1)
        self.finish_foreground();self.now+=60
        asyncio.run(service.maintain());job=self.claim_native()
        async def model(*args,**kwargs):
            self.assertEqual(kwargs['max_tool_rounds'],0)
            _,reservation=active_model_budget().prepare(kwargs['profile'],{'messages':[{'role':'user','content':'TEST'}],'max_tokens':1024})
            reservation.finish({'prompt_tokens':12,'completion_tokens':7})
            return 'real shared-loop boundary TEST'
        with patch('src.plugins.ai_chat.agent_loop.ask_deepseek_with_tools',new=model):
            outcome=asyncio.run(service.execute(job))
        self.assertEqual(outcome['status'],'queued')
        self.assertEqual(self.query('SELECT status,charged_tokens FROM routine_budget_reservations')[0]['status'],'settled')
        self.assertEqual(self.query('SELECT charged_tokens FROM routine_budget_reservations')[0][0],19)
        self.assertEqual(self.query('SELECT input_tokens,output_tokens FROM usage_events')[0]['input_tokens'],12)
        asyncio.run(service.close())
        self.assertIsNone(getattr(service.runtime,'native_commands',None))

    def test_actual_reminder_tool_source_list_cancel_and_identity_rejection(self):
        service=self.create_service();platform=self.platform();platform.native_background=service
        context=SimpleNamespace(platform=platform,inbound=self.source.inbound,capabilities=frozenset({'reminders'}))
        turn=SimpleNamespace(context=context)
        due=datetime.fromtimestamp(self.now+100,timezone.utc).isoformat()
        args={'due_at':due,'message':'TEST reminder'}
        result=json.loads(asyncio.run(reminder_tool(turn,args,name='reminder_set')))
        self.assertTrue(result['ok']);handle=result['reminder']['handle']
        again=json.loads(asyncio.run(reminder_tool(turn,args,name='reminder_set')))
        self.assertEqual(again,result)
        listed=json.loads(asyncio.run(reminder_tool(turn,{},name='reminder_list')))
        self.assertEqual(listed['reminders'][0]['handle'],handle)
        wrong=json.loads(asyncio.run(reminder_tool(turn,{'reminder_handle':'reminder#999999'},name='reminder_cancel')))
        self.assertFalse(wrong['ok'])
        cancelled=json.loads(asyncio.run(reminder_tool(turn,{'reminder_handle':handle},name='reminder_cancel')))
        self.assertTrue(cancelled['cancelled'])
        platform.execution.token=object()
        self.assertFalse(json.loads(asyncio.run(reminder_tool(turn,args,name='reminder_set')))['ok'])
        self.assertEqual(len(self.query('SELECT * FROM reminders')),1)
        asyncio.run(service.close())

    def test_missing_configuration_still_installs_persisted_plan_deny_guard(self):
        runtime=self.make_runtime()
        service=asyncio.run(background_factory()(runtime,self.token))
        self.assertIsNone(service.domain)
        self.assertEqual(service.bind(life=SimpleNamespace(handle=self.handle,job=self.job,token=self.token),
            inbound=self.source.inbound,platform=self.platform()),{})
        # Existing native source cannot be authorized by disabling its service.
        with self.assertRaisesRegex(MigrationDenied,'native_background_disabled'):
            with self.tx() as c:
                from src.plugins.ai_chat.adapters.qq_official.native_schedules import OccurrenceProof
                service.plan_guard(c,plan={'plan_id':'TEST'},operation='enqueue',source_proof=OccurrenceProof('TEST',self.job))
        asyncio.run(service.close())

    def test_optional_wiki_close_failure_cannot_leak_epoch_guard(self):
        events=[]
        class Wiki:
            def register(self,worker):events.append(('register',worker))
            def commands_for(self,**kw):return 'TEST_CALLBACK'
            async def maintain(self):events.append(('maintain',))
            def close(self):raise RuntimeError('TEST_CLOSE_FAILURE')
        runtime=self.make_runtime();old=runtime.outbox.source_guard
        service=asyncio.run(background_factory(wiki_factory=lambda r,t:Wiki())(runtime,self.token))
        service.register('REAL_EXISTING_WORKER')
        self.assertEqual(service.bind(life=SimpleNamespace(handle=self.handle,job=self.job,token=self.token),
            inbound=self.source.inbound,platform=self.platform()),{'wiki':'TEST_CALLBACK'})
        asyncio.run(service.maintain())
        with self.assertRaises(RuntimeError):asyncio.run(service.close())
        self.assertIs(runtime.outbox.source_guard,old);self.assertIsNone(runtime.native_commands)
        self.assertEqual(events,[('register','REAL_EXISTING_WORKER'),('maintain',)])

    def test_shared_model_unknown_usage_never_refunds_or_claims_success(self):
        service=self.create_service()
        service.domain.set_routine(self.source,request_key='a'*64,name='TEST unknown',prompt='TEST',cron='* * * * *',timezone='UTC')
        self.finish_foreground();self.now+=60;asyncio.run(service.maintain());job=self.claim_native()
        async def model(*args,**kwargs):
            active_model_budget().prepare(kwargs['profile'],{'messages':[{'role':'user','content':'TEST'}],'max_tokens':1024})
            raise OSError('TEST_PRIVATE_PROVIDER_DETAIL')
        with patch('src.plugins.ai_chat.agent_loop.ask_deepseek_with_tools',new=model):result=asyncio.run(service.execute(job))
        self.assertEqual(result,{'status':'needs_attention','reason':'execution_outcome_unknown'})
        row=self.query('SELECT status,charged_tokens,reserved_tokens FROM routine_budget_reservations')[0]
        self.assertEqual(row['status'],'unknown');self.assertEqual(row['charged_tokens'],row['reserved_tokens'])
        self.assertFalse(self.query('SELECT * FROM delivery_plans'))
        asyncio.run(service.close())

    def test_actual_recent_speakers_tool_is_bounded_readonly_and_source_fenced(self):
        from src.plugins.ai_chat.tools.recent_speakers import query
        platform=self.platform();platform.outbox=self.outbox;platform._clock=lambda:self.now
        platform.execution.admission=self.admission
        context=SimpleNamespace(platform=platform,inbound=self.source.inbound,capabilities=frozenset({'context'}))
        result=query(context,{})
        self.assertTrue(result['ok']);self.assertFalse(result['is_membership_list'])
        self.assertEqual(result['scanned_message_limit'],500)
        self.assertEqual(len(result['speakers']),1)
        self.assertEqual(result['speakers'][0]['sample_message_count'],1)
        self.query("UPDATE qq_official_endpoints SET binding_state='revoked'")
        from src.plugins.ai_chat.adapters.qq_official.turn_source import ExecutionFenceLost
        with self.assertRaises(ExecutionFenceLost):query(context,{})
