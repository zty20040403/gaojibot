"""Native M4 task -> actual external journal -> shared quorum -> real Fleet PG/ASGI.

The logical migration route is an explicit named test port; no host is contacted.
"""
from tests import qq_official_host_fixture as host_fixture
import unittest
import json
from types import SimpleNamespace
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import Mock

from src.bot_security.store import SecurityError
from src.plugins.ai_chat.agent.contracts import ContextPacket
from src.plugins.ai_chat.agent.external import ExternalCalls,active_external,poll_external
from src.plugins.ai_chat.platform_types import BackgroundToolSource
from src.plugins.ai_chat.subagents import SubAgentStore,SubAgentCoordinator,TaskStep
from src.plugins.ai_chat.model_catalog import ModelCatalog,ModelProfile
from src.plugins.ai_chat.adapters.qq_official import turn_source as execution
from src.plugins.ai_chat.adapters.qq_official.subagent_runtime import submit,job_kind
from src.plugins.ai_chat.adapters.qq_official.background_host import NativeTaskSourcePort
from src.plugins.ai_chat.adapters.qq_official.host_external import OfficialExternalOperations
from src.plugins.ai_chat.fleet_client import FleetControlError

class LogicalRouteTestPort:
 def __init__(self,c):self.c=c
 def require_current(self,cursor,*,source,operation,now):
  if not self.c.route_live or source.route_epoch!=0 or operation!='execute':
   raise SecurityError('route_unavailable')

class BackgroundTests(host_fixture.approval_fixtures.ApprovalFixture,unittest.IsolatedAsyncioTestCase):
 asyncSetUpBase=host_fixture.HostFixture.asyncSetUpBase
 asyncTearDownBase=host_fixture.HostFixture.asyncTearDownBase
 delivered=host_fixture.HostFixture.delivered
 quorum=host_fixture.HostFixture.quorum
 asyncTearDown=host_fixture.HostFixture.asyncTearDown
 async def asyncSetUp(self):
  await host_fixture.HostFixture.asyncSetUp(self)
  self.task_store=SubAgentStore(self.database);self.addCleanup(self.task_store.close)
  self.coordinator=SubAgentCoordinator(self.task_store,ModelCatalog({'test':ModelProfile(name='test',provider='test',protocol='openai-chat',model='test',base_url='http://127.0.0.1',api_key_required=False)},default_profile='test'),logger=Mock())
  packet=ContextPacket(self.endpoint.scope.key,'TEST_HOST_TASK',None,self.message.canonical_message_id,
    'TEST objective cannot grant host access',endpoint=self.endpoint,requester_actor=self.actor)
  self.args=dict(token=self.token,source_api=execution,source_handle=self.handle,source_job=self.job,
   tool_call_id='TEST_NATIVE_HOST',tool_name='run_subagents',packet=packet,decision=None,profile='test',now=self.now,
   execution_deadline=self.now+600,result_wait_seconds=600,admission=self.admission,
   coordinator=self.coordinator,jobs=self.jobs,tool_profile='official-host-v1',host_admission=lambda c,h:True)
  with self.tx() as c:self.task=submit(c,**self.args)
  self.jobs.lease_seconds=3600
  self.task_job=self.jobs.claim_due('TEST_HOST_BACKGROUND',kinds=(job_kind(self.account),),now=self.now)[0]
  self.port=NativeTaskSourcePort(token=self.token,admission=self.admission,route=LogicalRouteTestPort(self))
  self.transport.source_port=self.port
  self.bridge=OfficialExternalOperations(store=self.task_store,authority=self.host,source_port=self.port,transaction_factory=self.tx)
  self.task_store.official_external=self.bridge
  with self.tx() as c:self.source=self.port.source_for(c,background=self.metadata(),route_epoch=0,now=self.now)
  self.run=self.task_store.create_run(self.task.task_id,TaskStep('TEST_SERVICE','analyst','TEST','TEST',()),
    allowed_tools=('operation_prepare','operation_status'),model_profile='test',now=self.now)
  self.tracker=ExternalCalls(self.task_store,self.task.task_id,self.run.run_id)
  self.tracker.call_id='TEST_PREPARE';self.tracker.tool_name='operation_prepare'
 def metadata(self):return BackgroundToolSource(self.endpoint,self.actor,'subagent','task:'+str(self.task.task_id),1)
 @contextmanager
 def bound(self):
  with self.bridge.bind(self.task,self.task_job,route_epoch=0):
   value=active_external.set(self.tracker)
   try:yield
   finally:active_external.reset(value)
 async def prepare(self):
  with self.bound():return await self.client.prepare_operation(self.body,actor='official',origin=self.endpoint.scope.key)
 async def approve(self,record):
  self.tracker.call_id='TEST_APPROVE';self.tracker.tool_name='operation_approve'
  with self.bound():return await self.client.approve_operation(record['operation_id'],record['contract_hash'],record['resource_version'],
    actor='official',origin=self.endpoint.scope.key)
 async def test_pending_quorum_resume_is_fact_not_execution_and_approve_is_once(self):
  first=await self.prepare();record=first['operation']
  self.assertEqual(self.task_store.external_calls(self.task.task_id)[0]['status'],'pending')
  with self.bound():self.assertFalse(await poll_external(self.task_store,self.task,self.client))
  self.assertEqual(len([c for c in self.http_calls if c[0]=='POST']),1)
  await self.quorum()
  before=len(self.http_calls)
  with self.bound():self.assertTrue(await poll_external(self.task_store,self.task,self.client))
  self.assertTrue(all(c[0]=='GET' for c in self.http_calls[before:]))
  item=self.task_store.external_calls(self.task.task_id)[0]
  self.assertEqual(item['status'],'resolved');self.assertFalse(item['response']['executed'])
  self.assertEqual(item['response']['operation']['status'],'awaiting_approval')
  self.assertEqual(self.remote_store.get_operation(record['operation_id'])['status'],'awaiting_approval')
  # Existing actual session checkpoint consumes the stored prepare receipt.
  with self.bound():
   self.task_store.save_agent_session(self.task.task_id,self.run.run_id,
    [{'role':'assistant','tool_calls':[{'id':'TEST_PREPARE','type':'function','function':{'name':'operation_prepare','arguments':'{}'}}]}],
    scope_key=self.task.scope_key,requester_user_id=None,endpoint=self.endpoint,requester_actor=self.actor,
    model_profile='test',expected_version=0)
   hydrated=self.task_store.hydrate_external_session(self.task.task_id,self.run.run_id)
  self.assertIn('"executed":false',hydrated['messages'][-1]['content'])
  result=await self.approve(record);self.assertTrue(result['submitted'])
  await self.approve(record)
  self.assertEqual(len([c for c in self.http_calls if c[0]=='POST' and c[1].endswith('/approve')]),1)
  self.assertEqual(len(self.query("SELECT * FROM admin_approvals WHERE kind='server_task'")),1)
  self.assertEqual(len(self.fake.received),2) # original + independent reviewer only
 async def test_profile_must_be_explicit_and_cannot_upgrade_existing_submission(self):
  with self.tx() as c:
   with self.assertRaises(ValueError):submit(c,**(self.args|{'host_admission':None,'tool_call_id':'TEST_DENY'}))
  with self.tx() as c:
   with self.assertRaises(ValueError):submit(c,**(self.args|{'tool_profile':'official-text-v1'}))
  self.query("UPDATE qq_official_subagent_dispatches SET tool_profile='official-text-v1' WHERE task_id=?",(self.task.task_id,))
  with self.assertRaises(SecurityError):
   with self.bound():pass
  self.assertFalse(self.http_calls)
 async def test_metadata_and_stale_job_are_not_authority(self):
  with self.tx() as c:
   with self.assertRaises(SecurityError):self.port.source_for(c,background=replace(self.metadata(),source_revision=2),route_epoch=0,now=self.now)
  self.task_job=replace(self.task_job,lease_generation=self.task_job.lease_generation+1)
  with self.assertRaises(SecurityError):await self.prepare()
  self.assertFalse(self.http_calls)
 async def test_owner_cancel_and_route_change_stop_resume(self):
  await self.prepare();await self.quorum()
  self.route_live=False
  before=len(self.http_calls)
  with self.assertRaises(SecurityError):
   with self.bound():await poll_external(self.task_store,self.task,self.client)
  self.assertEqual(before,len(self.http_calls))
  self.route_live=True;self.query('UPDATE subagent_tasks SET cancel_requested=TRUE WHERE task_id=?',(self.task.task_id,))
  with self.assertRaises(SecurityError):
   with self.bound():pass
 async def test_changed_remote_contract_never_promotes_receipt(self):
  record=(await self.prepare())['operation'];await self.quorum()
  self.query('UPDATE fleet_operations SET resource_version=resource_version+1 WHERE operation_id=?',(record['operation_id'],))
  with self.assertRaises((SecurityError,FleetControlError)):
   with self.bound():await poll_external(self.task_store,self.task,self.client)
  self.assertEqual(self.task_store.external_calls(self.task.task_id)[0]['status'],'pending')
 async def test_missing_first_receipt_is_parked_without_repost(self):
  from unittest.mock import AsyncMock,patch
  with patch.object(self.client,'_authorized_request',new=AsyncMock(side_effect=TimeoutError('TEST unsafe detail'))):
   result=await self.prepare()
  self.assertEqual(result['status'],'needs_attention');self.assertIsNone(result['executed'])
  await self.prepare()
  self.assertFalse(self.http_calls)
  row=self.task_store.external_calls(self.task.task_id)[0]
  self.assertNotIn('unsafe',str(row['response']))
 async def test_confirmation_can_wait_pending_but_pending_job_cannot_effect(self):
  await self.prepare()
  self.query("UPDATE durable_jobs SET status='pending',lease_owner='',lease_until=0 WHERE job_id=?",(self.task_job.job_id,))
  await self.quorum()
  with self.assertRaises(SecurityError):await self.prepare()
  self.assertFalse([c for c in self.http_calls if c[1].endswith('/approve')])

 async def test_crash_after_intent_without_receipt_never_reposts(self):
  with self.bound():
   _,request=self.bridge._request(self.tracker,'POST','/v1/operations/prepare',self.body,'official',self.endpoint.scope.key)
   self.task_store.begin_external(self.task.task_id,1,self.run.run_id,self.tracker.call_id,request)
  result=await self.prepare()
  self.assertEqual(result['status'],'needs_attention')
  self.assertFalse(self.http_calls)
 async def test_independent_reviewer_revocation_denies_hydrate_and_replay(self):
  record=(await self.prepare())['operation'];await self.quorum()
  with self.bound():await poll_external(self.task_store,self.task,self.client)
  with self.tx() as c:self.owners.apply_trusted_config(c,self.account,('TEST_ADMIN_A',),'TEST_REVOKE_REVIEWER')
  before=len(self.http_calls)
  with self.assertRaises(SecurityError):
   with self.bound():self.task_store.hydrate_external_session(self.task.task_id,self.run.run_id)
  result=await self.approve(record)
  self.assertFalse(result.get('submitted',False))
  self.assertFalse([c for c in self.http_calls[before:] if c[0]=='POST' and c[1].endswith('/approve')])


 def task_app(self):
  from src.plugins.ai_chat.config import settings
  return SimpleNamespace(settings=replace(settings,search_enabled=False),db_call=self.calls,
    long_term_memory=None,message_ledger=self.ledger,context_store=None,pin_store=None,semantic_recall=None,
    skill_registry=SimpleNamespace(get=lambda name:None,list=lambda:[]),self_source=Mock())
 async def test_real_registry_only_exact_source_can_prepare_and_approve(self):
  from src.plugins.ai_chat.adapters.qq_official.host_session import HostTaskSessionFactory
  from src.plugins.ai_chat.tool_invocation import active_tool_call_id
  factory=HostTaskSessionFactory(bridge=self.bridge,client=self.client,app=self.task_app(),route_epoch=lambda c,t:0)
  session=await factory(self.task_job,self.task,False)
  mark=active_external.set(self.tracker);call=active_tool_call_id.set('TEST_PREPARE')
  try:
   self.assertTrue({'operation_prepare','operation_status','operation_approve'} <= {d['function']['name'] for d in session.tools})
   denied=json.loads(await session.execute_tool('operation_approve',{'operation_id':'op_'+'0'*32,'contract_hash':'0'*64,'resource_version':True}))
   self.assertFalse(denied['ok']);self.assertFalse(self.http_calls)
   result=json.loads(await session.execute_tool('operation_prepare',{'host_id':'testhost','resource_ref':'test.service','operation':'service.restart','ttl_seconds':120}))
   record=result['operation']
  finally:
   active_tool_call_id.reset(call);active_external.reset(mark);await session.close()
  await self.quorum()
  session=await factory(self.task_job,self.task,False) # polls only original ID
  self.tracker.call_id='TEST_APPROVE';self.tracker.tool_name='operation_approve'
  mark=active_external.set(self.tracker)
  try:
   from src.plugins.ai_chat.tool_policy import policy_for_tool
   approval_args={'operation_id':record['operation_id'],'contract_hash':record['contract_hash'],'resource_version':record['resource_version']}
   decision=await session.hooks.approval_checker(policy_for_tool('operation_approve'),'operation_approve',approval_args)
   self.assertTrue(decision.allowed)
   wrong=await session.hooks.approval_checker(policy_for_tool('operation_approve'),'operation_approve',approval_args|{'contract_hash':'0'*64})
   self.assertFalse(wrong.allowed)
   result=json.loads(await session.execute_tool('operation_approve',{'operation_id':record['operation_id'],
        'contract_hash':record['contract_hash'],'resource_version':record['resource_version']}))
   self.assertTrue(result['submitted'])
  finally:active_external.reset(mark);await session.close()
  self.assertEqual(len([c for c in self.http_calls if c[0]=='POST' and c[1].endswith('/approve')]),1)
 async def test_host_factory_requires_configuration_and_defers_pending_without_new_model(self):
  from src.plugins.ai_chat.adapters.qq_official.host_session import HostTaskSessionFactory
  from src.plugins.ai_chat.workers.durable_jobs import JobDeferred
  with self.assertRaises(SecurityError):HostTaskSessionFactory(bridge=self.bridge,client=self.client,app=self.task_app(),route_epoch=None)
  await self.prepare()
  factory=HostTaskSessionFactory(bridge=self.bridge,client=self.client,app=self.task_app(),route_epoch=lambda c,t:0)
  with self.assertRaises(JobDeferred):await factory(self.task_job,self.task,False)
  with self.assertRaises(SecurityError):self.bridge.guard(self.task.task_id)
  self.assertEqual(len([c for c in self.http_calls if c[0]=='POST']),1)
 async def test_actual_dispatcher_coordinator_resumes_same_operation_through_three_rounds(self):
  from unittest.mock import AsyncMock,patch
  from src.plugins.ai_chat.adapters.qq_official.host_session import HostTaskSessionFactory
  from src.plugins.ai_chat.adapters.qq_official.subagent_composition import OfficialSubAgentService
  from src.plugins.ai_chat.adapters.qq_official.approval_runtime import OfficialApprovalService
  from src.plugins.ai_chat.agent_loop import AgentLoopEvent
  from src.plugins.ai_chat.tool_invocation import active_tool_call_id
  from src.plugins.ai_chat.workers.durable_jobs import DurableJobWorker
  app=self.task_app();app.subagent_store=self.task_store;app.subagent_coordinator=self.coordinator
  app.model_catalog=self.coordinator.model_catalog;app.logger=Mock()
  factory=HostTaskSessionFactory(bridge=self.bridge,client=self.client,app=app,route_epoch=lambda c,t:0)
  approvals=OfficialApprovalService(token=self.token,mobile=self.mobile,
    host_admission=lambda c,h:h==self.handle,host_session_factory=factory)
  runtime=SimpleNamespace(store=SimpleNamespace(clock=lambda:self.now,admitted=self.admission),
    deliveries=self.deliveries,_calls=self.calls,jobs=self.jobs,outbox=self.outbox,sender=self.sender,
    approval_service=approvals)
  service=OfficialSubAgentService(runtime,self.token,app)
  worker=DurableJobWorker(self.jobs,logger=Mock(),database_calls=self.calls,worker_id=self.task_job.lease_owner)
  worker.register(job_kind(self.account),service.execute)
  self.task_store.set_task_state(self.task.task_id,'queued',plan={'mode':'workflow','steps':[{'id':'TEST_SERVICE','agent':'analyst','objective':'TEST','depends_on':[]}]})
  round_number=0;stored=None
  async def complete(text,messages,tools,execute,**kw):
   nonlocal round_number,stored
   round_number+=1
   self.assertIn('operation_approve',{d['function']['name'] for d in tools})
   if round_number<=2:
    name='operation_prepare' if round_number==1 else 'operation_approve'
    args=({'host_id':'testhost','resource_ref':'test.service','operation':'service.restart','ttl_seconds':120}
      if round_number==1 else {'operation_id':stored['operation_id'],'contract_hash':stored['contract_hash'],'resource_version':stored['resource_version']})
    call_id='TEST_DRIVER_'+str(round_number)
    await kw['event_sink'](AgentLoopEvent('tool_started',1,tool_name=name,arguments=args,call_id=call_id))
    if name=='operation_approve':
     from src.plugins.ai_chat.agent_loop import _check_tool_approval
     from src.plugins.ai_chat.tool_policy import policy_for_tool
     decision=await _check_tool_approval(kw['approval_checker'],policy_for_tool(name),name,args)
     self.assertTrue(decision.allowed)
    mark=active_tool_call_id.set(call_id)
    try:result=json.loads(await execute(name,args))
    finally:active_tool_call_id.reset(mark)
    if round_number==1:stored=result['operation']
    transcript=[*messages,{'role':'assistant','tool_calls':[{'id':call_id,'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]},
      {'role':'tool','tool_call_id':call_id,'content':json.dumps(result)}]
    kw['transcript_sink'](transcript)
    kw['after_tool_round']() # genuine ExternalPending, existing Coordinator path
   return '{"status":"success","summary":"TEST verified final","artifacts":[]}'
  with patch('src.plugins.ai_chat.subagents.ask_deepseek_with_tools',new=complete), \
       patch('src.plugins.ai_chat.subagents.ask_deepseek',new=AsyncMock(return_value='TEST final')):
   await worker._execute(self.task_job)
   self.assertEqual(self.task_store.get(self.task.task_id).status,'waiting_external')
   self.assertEqual(self.jobs.get(self.task_job.job_id).status,'pending')
   await self.quorum()
   self.now+=4;self.task_job=self.jobs.claim_due(worker.worker_id,kinds=(job_kind(self.account),),now=self.now)[0]
   await worker._execute(self.task_job)
   self.assertEqual(self.task_store.get(self.task.task_id).status,'waiting_external')
   self.assertEqual(self.remote_store.get_operation(stored['operation_id'])['status'],'queued')
   # Only the test provider asserts its terminal fact; no actual host execution.
   self.query("UPDATE fleet_operations SET status='succeeded',result_json=? WHERE operation_id=?",('{"ok":true,"source":"TEST_FAKE_PROVIDER"}',stored['operation_id']))
   self.now+=4;self.task_job=self.jobs.claim_due(worker.worker_id,kinds=(job_kind(self.account),),now=self.now)[0]
   await worker._execute(self.task_job)
  self.assertEqual(round_number,3)
  self.assertEqual(self.task_store.get(self.task.task_id).status,'completed')
  self.assertEqual(self.jobs.get(self.task_job.job_id).status,'succeeded')
  self.assertEqual(len([c for c in self.http_calls if c[0]=='POST' and c[1].endswith('/approve')]),1)
  self.assertEqual(len([c for c in self.http_calls if c[0]=='POST' and c[1].endswith('/prepare')]),1)
  self.assertIsNotNone(self.query('SELECT plan_id FROM qq_official_subagent_dispatches WHERE task_id=?',(self.task.task_id,))[0]['plan_id'])

 async def test_new_model_call_id_cannot_replay_unknown_approval(self):
  from unittest.mock import patch
  record=(await self.prepare())['operation'];await self.quorum()
  with self.bound():await poll_external(self.task_store,self.task,self.client)
  original=self.client._raw_request
  attempts=0
  async def lost(method,path,*args,**kwargs):
   nonlocal attempts
   if method=='POST' and path.endswith('/approve'):
    attempts+=1
    raise TimeoutError('TEST response absent')
   return await original(method,path,*args,**kwargs)
  with patch.object(self.client,'_raw_request',new=lost):
   first=await self.approve(record)
  self.assertIsNone(first['executed']);self.assertEqual(attempts,1)
  self.tracker.call_id='TEST_NEW_MODEL_CALL';self.tracker.tool_name='operation_approve'
  with self.bound():
   second=await self.client.approve_operation(record['operation_id'],record['contract_hash'],record['resource_version'],
      actor='official',origin=self.endpoint.scope.key)
  self.assertEqual(second['status'],'needs_attention');self.assertIsNone(second['executed'])
  self.assertFalse([c for c in self.http_calls if c[0]=='POST' and c[1].endswith('/approve')])
  self.assertEqual(len([c for c in self.task_store.external_calls(self.task.task_id)
                        if c['request']['path'].endswith('/approve')]),1)

if __name__=='__main__':unittest.main()
