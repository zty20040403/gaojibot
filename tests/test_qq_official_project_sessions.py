"""Actual project/task/source leases with fake owned guests and model calls."""
from dataclasses import replace
import hashlib
import asyncio
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import zipfile

from src.bot_storage import DatabaseCalls
from src.project_update import ARTIFACT_NAME, DESCRIPTOR
from src.plugins.ai_chat.agent.control import LeaseLost
from src.plugins.ai_chat.projects import ProjectConflict
from src.plugins.ai_chat.agent.execution import active_agent_step, EntryDecision, TaskContract
from src.plugins.ai_chat.agent_loop import AgentLoopEvent
from src.plugins.ai_chat.config import settings
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.model_catalog import ModelCatalog, ModelProfile
from src.plugins.ai_chat.project_lifecycle import ProjectLifecycle
from src.plugins.ai_chat.subagents import SubAgentCoordinator, TaskStep
from src.plugins.ai_chat.workers.durable_jobs import JobDeferred
from src.plugins.ai_chat.tool_invocation import active_tool_call_id
from src.plugins.ai_chat.adapters.qq_official.file_services import FileServiceConfig, FileServices
from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
from src.plugins.ai_chat.adapters.qq_official.send_store import OfficialSendStore
from src.plugins.ai_chat.adapters.qq_official.project_artifacts import ProjectArtifactAuthority
from src.plugins.ai_chat.adapters.qq_official.project_sessions import ProjectTaskSessions
from src.plugins.ai_chat.adapters.qq_official.subagent_runtime import OfficialSubAgentDispatcher
from tests import test_qq_official_project_background as task_fixture
from tests.qq_official_project_fixture import ProjectFixture
from tests.qq_official_message_fixture import record_evidence
from tests.test_owned_sandbox import FakeManager


class ProjectGuest(FakeManager):
    async def list(self, owner):
        return [{**item,'purpose':'task'} for item in await super().list(owner)]

    async def install_readonly_file(self, owner, sid, path, content):
        self.own(owner,sid)['files'][path]=bytes(content)
        self.event('install_readonly',owner,sid)
        return len(content)

    async def exec(self, owner, sid, command, timeout_seconds=None, packages=None):
        files=self.own(owner,sid)['files']
        if command=='make-project-update':
            manifests=[json.loads(value) for path,value in files.items() if path.endswith('/manifest.json')]
            assert len(manifests)==1
            descriptor=dict(format=1,project_id=self.pid,base_version=1,base_manifest_sha256=self.baseline,
                files=[dict(path='note.txt',sha256=hashlib.sha256(b'TEST edited').hexdigest(),size=11,mode=420)],delete=[])
            output=io.BytesIO()
            with zipfile.ZipFile(output,'w') as archive:
                archive.writestr(DESCRIPTOR,json.dumps(descriptor))
                archive.writestr('files/note.txt',b'TEST edited')
            files[ARTIFACT_NAME]=output.getvalue()
        elif command.startswith('inspect-update '):
            artifact=files[command.removeprefix('inspect-update ')]
            with zipfile.ZipFile(io.BytesIO(artifact)) as archive:
                assert archive.read('files/note.txt')==b'TEST edited'
            assert any(path.endswith('/manifest.json') for path in files)
        else:
            return await super().exec(owner,sid,command,timeout_seconds,packages)
        self.event('exec',owner,sid)
        return SimpleNamespace(returncode=0,stdout='TEST actual pinned update checked',stderr='',manifest=None)


class ProjectSessionTests(ProjectFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        from src.plugins.ai_chat.subagents import SubAgentStore
        from src.plugins.ai_chat.project_blobs import ProjectFile
        from src.plugins.ai_chat.agent.contracts import ContextPacket
        from src.plugins.ai_chat.project_tasks import ProjectTaskStore
        from src.plugins.ai_chat.project_background import ProjectTaskDispatcher
        self.tasks=SubAgentStore(self.database);self.addCleanup(self.tasks.close)
        self.project=self.create()
        self.store.snapshot(self.access,self.project['project_id'],[ProjectFile('note.txt',b'TEST content')],
            expected_version=self.project['version'],now=self.now)
        self.store.open(self.access,self.project['project_id'],now=self.now)
        self.packet=ContextPacket(self.endpoint.scope.key,'TEST_PROJECT_TASK_CONVERSATION',None,
            self.message.canonical_message_id,'TEST Update the project',endpoint=self.endpoint,requester_actor=self.actor)
        self.dispatch={'platform':'qq-official','profile':'test','tool_profile':'official-projects-v1','deadline':self.now+600}
        self.task_store=ProjectTaskStore(self.store,self.tasks,official_dispatch=ProjectTaskDispatcher(self.jobs))
        record_evidence(self)
        self.query('''UPDATE qq_official_inbound_events i SET endpoint_generation=e.generation,route_epoch=e.route_epoch
            FROM qq_official_endpoints e WHERE e.endpoint_key=i.endpoint_key''')
        self.profile=ModelProfile(name='test',provider='test',protocol='openai-chat',model='test',
            base_url='http://127.0.0.1',api_key_required=False)
        self.catalog=ModelCatalog({'test':self.profile},default_profile='test')
        self.coordinator=SubAgentCoordinator(self.tasks,self.catalog,logger=Mock(),max_parallelism=1)
        self.manager=ProjectGuest();self.manager.pid=self.project['project_id']
        self.manager.baseline=self.query('SELECT manifest_sha256 FROM project_versions WHERE project_id=?',(self.project['project_id'],))[0]['manifest_sha256']
        self.deliveries=DeliveryStore(self.database,recover_interrupted=False)
        self.addCleanup(self.deliveries.close)
        self.outbox=OfficialSendStore(self.deliveries,admission=self.admission,
            pepper=b'TEST_PROJECT_SESSION_PEPPER'*2,clock=lambda:self.now)
        self.temp=tempfile.TemporaryDirectory(prefix='TEST_project_sessions_')
        self.addCleanup(self.temp.cleanup)
        self.jobs.lease_seconds=3600
        self.model_calls=0
        self.sequence=0

    prepare_task=task_fixture.TaskTests.prepare_task

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration=2
        self.calls=DatabaseCalls()
        self.runtime=SimpleNamespace(_calls=self.calls,deliveries=self.deliveries,jobs=self.jobs,outbox=self.outbox,
            _stopping=False,_token=self.token,store=SimpleNamespace(admitted=self.admission,clock=lambda:self.now))
        self.authority=ProjectArtifactAuthority(token=self.token,clock=lambda:self.now,route_guard=self.route_guard)
        self.config=FileServiceConfig(Path(self.temp.name),self.manager,generation_enabled=True,
            source_authority=self.authority,min_free_bytes=0)
        self.files=FileServices(self.runtime,self.token,None,self.config)
        self.runtime.file_service=self.files
        lifecycle=ProjectLifecycle(self.store,self.jobs)
        self.projects=SimpleNamespace(store=self.store,lifecycle=lifecycle,ready=True,closed=False,
            backends={'oci':SimpleNamespace(preflight=lambda _:None)})
        self.app=SimpleNamespace(project_runtime=self.projects,subagent_store=self.tasks,subagent_coordinator=self.coordinator,
            model_catalog=self.catalog,logger=Mock(),db_call=self.calls,
            settings=replace(settings,projects_enabled=True,search_enabled=False),
            message_ledger=self.ledger,context_store=None,pin_store=None,semantic_recall=None,long_term_memory=None,
            skill_registry=SimpleNamespace(get=lambda _:None,list=lambda:[]),self_source=Mock())
        self.sessions=ProjectTaskSessions(self.runtime,self.token,self.app,authority=self.authority)
        self.open_sessions=[]

    async def asyncTearDown(self):
        for session in self.open_sessions:await session.close()
        await self.files.close()
        await self.calls.close()

    def admit(self, *, decision=None):
        self.task=self.prepare_task(decision=decision)
        self.task_job=self.jobs.claim_due('TEST_PROJECT_SESSION_WORKER',
            kinds=(account_job_kind('subagent',self.account.key),),now=self.now)[0]
        return self.task

    async def session(self):
        value=await self.sessions.prepare(self.task_job,self.tasks.get(self.task.task_id),True)
        self.open_sessions.append(value)
        return value

    def step(self, key):
        self.tasks.set_task_state(self.task.task_id,'running')
        step=TaskStep(key,'coder','TEST edit','TEST artifact')
        run=self.tasks.create_run(self.task.task_id,step,allowed_tools=[],model_profile=self.profile.name)
        self.tasks.start_run(run.run_id)
        return active_agent_step.set(f'task#{self.task.task_id}/{key}')

    async def test_real_per_step_owners_baseline_and_cross_step_denial(self):
        self.admit();session=await self.session()
        ids=[];owners=[]
        for step in ('produce','review'):
            mark=self.step(step)
            try:
                value=json.loads(await session.execute_tool('sandbox_create',{'runtime':'python'}))
                self.assertTrue(value.get('ok',True),value)
                sid=value['sandbox_id'];ids.append(sid)
                owner=self.manager.items[sid]['owner'];owners.append(owner)
                self.assertRegex(owner,r'^qqo:v1:[a-f0-9]{64}:x:[a-f0-9]{32}$')
                imported=json.loads(await session.hooks.project_inputs.import_into(self.task.task_id,
                    session.hooks.workspaces,{'sandbox_id':sid}))
                self.assertEqual(imported['manifest_sha256'],self.manager.baseline)
                self.assertIn(b'TEST content',self.manager.items[sid]['files'].values())
                if len(ids)>1:
                    bad=json.loads(await session.execute_tool('sandbox_read_file',{'sandbox_id':ids[0],'path':'note.txt'}))
                    self.assertFalse(bad['ok'])
            finally:active_agent_step.reset(mark)
        self.assertNotEqual(*owners)
        with self.assertRaises((PermissionError, LeaseLost, ProjectConflict)):
            await session.execute_tool('sandbox_create',{'runtime':'python'})
        self.assertEqual(len(self.manager.items),2)

    async def test_revoked_step_stops_next_physical_rpc_and_keeps_original_owner(self):
        self.admit();session=await self.session();mark=self.step('produce')
        try:
            sid=json.loads(await session.execute_tool('sandbox_create',{'runtime':'python'}))['sandbox_id']
            before=len(self.manager.calls)
            self.query("UPDATE subagent_runs SET status='succeeded' WHERE task_id=?",(self.task.task_id,))
            denied=json.loads(await session.execute_tool('sandbox_write_file',{'sandbox_id':sid,'path':'x','content':'secret'}))
            self.assertFalse(denied['ok'])
            self.assertEqual(len(self.manager.calls),before)
        finally:active_agent_step.reset(mark)

    async def test_import_post_rpc_revoke_does_not_claim_completed_baseline(self):
        self.admit();session=await self.session();mark=self.step('produce')
        try:
            sid=json.loads(await session.execute_tool('sandbox_create',{'runtime':'python'}))['sandbox_id']
            def revoke(name,owner,sid):
                if name=='install_readonly':
                    self.manager.after=None
                    self.query('UPDATE subagent_tasks SET cancel_requested=TRUE WHERE task_id=?',(self.task.task_id,))
            self.manager.after=revoke
            with self.assertRaises((PermissionError, LeaseLost, ProjectConflict)):
                await session.hooks.project_inputs.import_into(self.task.task_id,session.hooks.workspaces,{'sandbox_id':sid})
            self.assertFalse(any(path.endswith('/manifest.json') for path in self.manager.items[sid]['files']))
        finally:active_agent_step.reset(mark)

    async def invoke(self, execute_tool, sink, name, args):
        self.sequence+=1
        call='TEST_PROJECT_CALL_'+str(self.sequence)
        await sink(AgentLoopEvent('tool_started',self.sequence,tool_name=name,arguments=args,call_id=call))
        mark=active_tool_call_id.set(call)
        try: raw=await execute_tool(name,args)
        finally:active_tool_call_id.reset(mark)
        await sink(AgentLoopEvent('tool_finished',self.sequence,tool_name=name,arguments=args,result=raw,call_id=call))
        value=json.loads(raw)
        self.assertTrue(value.get('ok',True),value)
        return value

    async def project_model(self,prompt,messages,tools,execute_tool,**kwargs):
        self.model_calls+=1
        invoke=lambda name,args:self.invoke(execute_tool,kwargs['event_sink'],name,args)
        sid=(await invoke('sandbox_create',{'runtime':'python'}))['sandbox_id']
        await invoke('import_project_baseline',{'sandbox_id':sid})
        if '/acceptance_r' in active_agent_step.get():
            copied=await invoke('import_agent_artifact',{'step_id':'produce','artifact_index':0,'sandbox_id':sid})
            checked=await invoke('sandbox_exec',{'sandbox_id':sid,'command':'inspect-update '+copied['path'].removeprefix('/workspace/')})
            return json.dumps(dict(status='success',summary='TEST checked exact update and baseline',artifacts=[],findings=[],
                completed=[dict(description='TEST independently checked update',evidence_refs=[checked['_task_evidence']['ref']])],
                authorization=[],next_verification=[],metadata=dict(
                    artifact_reviews=[dict(artifact_key=copied['sha256'],status='passed',reason='TEST checked exact snapshot')],
                    criterion_reviews=[dict(criterion_index=0,status='passed',reason='TEST applied update',evidence_refs=[checked['_task_evidence']['ref']])])),ensure_ascii=False)
        made=await invoke('sandbox_exec',{'sandbox_id':sid,'command':'make-project-update'})
        return json.dumps(dict(status='success',summary='TEST produced project update',findings=[],
            completed=[dict(description='TEST project update',evidence_refs=[made['_task_evidence']['ref']])],
            authorization=[],next_verification=[],artifacts=[dict(handle=sid+':/workspace/'+ARTIFACT_NAME,name=ARTIFACT_NAME)]))

    def ready_pipeline(self):
        objective='TEST update pinned project note'
        decision=EntryDecision('delegate','execution','TEST project','',
            TaskContract(objective,(ARTIFACT_NAME,),(),('TEST verify update against baseline',),True,
                ({'criterion_index':0,'kind':'evidence'},),version=3),
            ({'id':'produce','agent':'coder','objective':objective,'deliverable':ARTIFACT_NAME,
              'depends_on':[],'required_tools':['sandbox_create','sandbox_exec']},))
        self.admit(decision=decision)
        self.dispatcher=OfficialSubAgentDispatcher(token=self.token,coordinator=self.coordinator,jobs=self.jobs,
            outbox=self.outbox,transaction=self.deliveries.transaction,database_calls=self.calls,
            admission=self.admission,profile=self.catalog.try_resolve,clock=lambda:self.now,
            execution_factory=self.sessions.prepare,project_finalizer=self.sessions.finalize,project_authority=self.sessions.assert_current)

    async def pipeline(self):
        with patch('src.plugins.ai_chat.adapters.qq_official.task_result.ask_deepseek_with_tools',new=AsyncMock(return_value='TEST foreground result')), \
             patch('src.plugins.ai_chat.subagents.ask_deepseek_with_tools',new=self.project_model), \
             patch('src.plugins.ai_chat.subagents.ask_deepseek',new=AsyncMock(return_value='TEST update accepted; workspace restore pending')), \
             patch('src.plugins.ai_chat.storage.jobs.time',SimpleNamespace(time=lambda:self.now)), \
             patch('src.plugins.ai_chat.project_publication.time',SimpleNamespace(time=lambda:self.now)), \
             patch('src.plugins.ai_chat.project_materialization.time',SimpleNamespace(time=lambda:self.now)):
            return await self.dispatcher.execute(self.task_job)

    async def test_real_pipeline_recovers_committed_publication_without_rerunning_model(self):
        self.ready_pipeline()
        publish=self.sessions.publications.publish
        def commit_then_interrupt(*args,**kwargs):
            publish(*args,**kwargs)
            raise RuntimeError('TEST lost response after committed publication')
        with patch.object(self.sessions.publications,'publish',side_effect=commit_then_interrupt):
            with self.assertRaises(JobDeferred):await self.pipeline()
        self.assertEqual(self.model_calls,2)
        self.assertEqual(len(self.query('SELECT * FROM project_task_publications')),1)
        self.assertEqual(self.query('SELECT * FROM project_task_materializations'),[])
        self.assertEqual(self.jobs.get(self.task_job.job_id).status,'running')
        with patch.object(self.files.artifacts.blobs,'read',side_effect=AssertionError('committed publication must not reread capture')):
            result=await self.pipeline()
        task=self.tasks.get(self.task.task_id)
        self.assertIn(task.status,{'completed','partial'},task.result)
        self.assertEqual(self.model_calls,2,task.result)
        publications=self.query('SELECT * FROM project_task_publications')
        self.assertEqual(len(publications),1,task.result)
        self.assertEqual(publications[0]['version_no'],2)
        self.assertEqual(len(self.query('SELECT * FROM project_task_materializations')),1)
        self.assertEqual(self.jobs.get(self.task_job.job_id).status,'running')
        self.assertEqual(self.query('SELECT * FROM subagent_deliveries'),[])
        self.assertTrue(result['plan_id'])
        owners={owner for name,owner,_ in self.manager.calls if name=='create'}
        self.assertEqual(len(owners),2)  # producer and project acceptance
        self.assertEqual(self.manager.items,{})  # Temporary project VMs are released.
        self.assertEqual(self.query("SELECT state FROM qq_official_generation_capacity WHERE state='reserved'"),[])

    async def test_missing_project_port_and_changed_grant_never_start_model(self):
        self.ready_pipeline()
        self.dispatcher.project_authority=None
        self.assertEqual((await self.pipeline())['status'],'needs_attention')
        self.dispatcher.project_authority=self.sessions.assert_current
        self.query('UPDATE qq_official_project_grants SET revision=revision+1')
        self.assertEqual((await self.pipeline())['status'],'needs_attention')
        self.assertEqual(self.model_calls,0)
        self.assertEqual(self.manager.calls,[])
        self.assertEqual(self.query('SELECT * FROM project_task_publications'),[])
        self.assertEqual(self.query('SELECT * FROM delivery_plans'),[])

    async def test_failed_project_task_reports_result_and_keeps_recovery_lock(self):
        self.ready_pipeline()
        self.tasks.set_task_state(self.task.task_id,'failed',error='TEST failure')
        result=await self.pipeline()
        self.assertTrue(result['plan_id'])
        self.assertEqual(self.model_calls,0)
        self.assertEqual(self.query('SELECT locked_task_id FROM projects WHERE project_id=?',
            (self.project['project_id'],))[0]['locked_task_id'],self.task.task_id)
        self.assertEqual(self.query('SELECT * FROM project_task_publications'),[])

    async def test_only_current_independent_acceptance_can_import_while_verifying(self):
        self.admit();session=await self.session();mark=self.step('produce')
        active_agent_step.reset(mark)
        self.query("UPDATE subagent_runs SET status='succeeded' WHERE task_id=?",(self.task.task_id,))
        self.tasks.set_task_state(self.task.task_id,'verifying')
        access=await session.hooks.project_inputs.resolve_access()
        for key,dependencies,ok in [('ordinary',('produce',),False),
                ('acceptance_r2_12345678',('produce',),False),
                ('acceptance_r1_12345678',('missing',),False),
                ('acceptance_r1_87654321',('produce',),True)]:
            run=self.tasks.create_run(self.task.task_id,TaskStep(key,'coder','TEST inspect','TEST result',dependencies=dependencies),
                allowed_tools=[],model_profile=self.profile.name)
            self.tasks.start_run(run.run_id)
            if ok:
                self.assertEqual(self.task_store.require_step(access,self.task.task_id,key),self.task.conversation_id)
            else:
                with self.assertRaises(ProjectConflict):self.task_store.require_step(access,self.task.task_id,key)
