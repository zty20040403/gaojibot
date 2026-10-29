from pathlib import Path
from src.plugins.ai_chat.agent.control import LeaseLost
from src.plugins.ai_chat.workers.durable_jobs import JobDeferred
from types import SimpleNamespace
from tests import test_qq_official_migration_activation as activation_fixture
from tests.qq_official_migration_journal_fixture import HostPorts
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from tests.test_qq_official_migration_services import MigrationServicesFixture
from tests.qq_official_migration_project_fixture import install_project_owner
from src.plugins.ai_chat.projects import ProjectStore, ProjectAccess, ProjectConflict
from src.plugins.ai_chat.project_blobs import ProjectFile
from src.plugins.ai_chat.project_tasks import ProjectTaskStore
from src.plugins.ai_chat.subagents import SubAgentStore, SubAgentCoordinator, TaskStep
from src.plugins.ai_chat.model_catalog import ModelProfile, ModelCatalog
from src.plugins.ai_chat.agent.contracts import ContextPacket
from src.plugins.ai_chat.adapters.qq_official.migration_preview import SourceRef, PreviewDenied
from src.plugins.ai_chat.adapters.qq_official.migration_types import MigrationDenied
from src.plugins.ai_chat.adapters.qq_official.migration_subagents import ConversionDenied, digest, PARKED
from src.plugins.ai_chat.project_background import BackgroundProjectProvider
from src.plugins.ai_chat.adapters.qq_official.project_authority import Operation
from src.plugins.ai_chat.adapters.qq_official.migration_project_tasks import ProjectTaskContinuations


from tests.test_qq_official_project_sessions import ProjectGuest
from tests.qq_official_file_fixture import PDF


class MigrationProjectGuest(ProjectGuest):
    async def exec(self,owner,sid,command,timeout_seconds=None,packages=None):
        if command=='make-project-update-and-report':
            result=await super().exec(owner,sid,'make-project-update',timeout_seconds,packages)
            self.own(owner,sid)['files']['report.pdf']=PDF
            return result
        if command.startswith('inspect-project-deliverables '):
            update,pdf=command.removeprefix('inspect-project-deliverables ').split(' ')
            result=await super().exec(owner,sid,'inspect-update '+update,timeout_seconds,packages)
            assert self.own(owner,sid)['files'][pdf]==PDF
            return result
        return await super().exec(owner,sid,command,timeout_seconds,packages)


class ProjectContinuationFixture(MigrationServicesFixture):
    def setUp(self):
        super().setUp()
        from src.plugins.ai_chat.platform_types import EndpointKey
        from src.plugins.ai_chat.conversation_scope import ConversationScope
        from tests.qq_official_migration_mapping_fixture import rotate_mapping
        self.source=EndpointKey(self.source.account,'group',self.source.account.app_id)
        self.source_scope=ConversationScope('onebot-v11','group',self.source.account.app_id,bot_native_user_id=self.source.account.app_id)
        rotate_mapping(self,expected=0)


    async def asyncSetUp(self):
        await super().asyncSetUp()
        install_project_owner(self)
        from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
        with self.tx() as cursor:observe_ready(cursor,self.token,{'op':0,'t':'READY','d':{'session_id':'TEST_PROJECT_MIGRATED','user':{'id':'TEST_REAL_BOT','bot':True}}},self.now)
        temporary=TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.projects=ProjectStore(self.database,Path(temporary.name));self.addCleanup(self.projects.close)
        principal=self.ledger.ensure_principal_identity('onebot-v11',self.source_actor.native_user_id,'TEST old project owner')
        self.old_access=ProjectAccess(self.source_scope.with_actor(self.source_actor.native_user_id),principal,member=True)
        self.project=self.projects.create(self.old_access,'TEST original project',now=self.now)
        self.projects.snapshot(self.old_access,self.project['project_id'],[ProjectFile('note.txt',b'TEST content')],expected_version=self.project['version'],now=self.now)
        self.projects.open(self.old_access,self.project['project_id'],now=self.now)
        self.tasks=SubAgentStore(self.database);self.addCleanup(self.tasks.close)
        self.profile=ModelProfile(name='test',provider='test',protocol='openai-chat',model='test',base_url='http://127.0.0.1',api_key_required=False)
        self.catalog=ModelCatalog({'test':self.profile},default_profile='test')
        self.coordinator=SubAgentCoordinator(self.tasks,self.catalog,logger=Mock(),max_parallelism=1)
        self.execution_deadline=self.now+600
        self.engine=ProjectTaskContinuations(self.database,token=self.token,jobs=self.jobs,coordinator=self.coordinator,journal=self.service.journal,preview=self.service.preview,profile='test',admission=self.store.admitted,clock=lambda:self.now,grants=self.service.project_grants,projects=self.projects,file_admission=lambda c,**kw:kw['endpoint']==self.endpoint and kw['actor']==self.actor)
        self.session.domains['project_task']=self.engine
        self.migration=self.service.journal
        host=HostPorts(self)
        self.service.ports.verifier=SimpleNamespace(request=host.verification_request,plan=host.verification_plan,receipt=host.verification)
        self.session.guard.continuations=tuple(self.session.domains.values())
        self.request=dict(source_endpoint=self.source.key,source_scope=self.source_scope.key,source_actor=self.source_actor.key,target_endpoint=self.endpoint.key,target_actor=self.actor.key,issuer_actor=self.actor.key,project_id=self.project['project_id'],mapping_id=self.mapping_evidence.mapping_id,mapping_revision=self.mapping_evidence.revision,capabilities=['project.read','project.write','project.execute','project.deliver'],expires_at=self.now+900)


    async def asyncTearDown(self):
        if hasattr(self,'files'):await self.files.close()
        await super().asyncTearDown()


    async def stage(self):
        self.staged=await self.service.console.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,action='project_grant_stage',request=self.request)


    def source_task(self,pending_attempt=0,*,pipeline=False):
        packet=ContextPacket(self.source_scope.key,'TEST original conversation',int(self.source_actor.native_user_id),None,'TEST Update original project')
        version=self.projects.get(self.old_access,self.project['project_id'])['version']
        decision=None
        if pipeline:
            from src.plugins.ai_chat.agent.execution import EntryDecision,TaskContract
            from src.project_update import ARTIFACT_NAME
            decision=EntryDecision('workflow','execution','TEST project','',TaskContract(packet.objective,(ARTIFACT_NAME,'report.pdf'),(),('TEST verify update against baseline and independent PDF',),True,({'criterion_index':0,'kind':'evidence'},),version=3),
                ({'id':'first','agent':'analyst','objective':'TEST already inspected','deliverable':'TEST findings','depends_on':[],'required_tools':[]},
                 {'id':'produce','agent':'coder','objective':'TEST pending project edit','deliverable':ARTIFACT_NAME+' and report.pdf','depends_on':['first'],'required_tools':['sandbox_create','sandbox_exec']}))
        old=ProjectTaskStore(self.projects,self.tasks).prepare(self.old_access,self.project['project_id'],expected_version=version,base_version=1,request_key=digest('TEST source project request'),packet=packet,decision=decision,dispatch={'bot_id':self.source.account.app_id,'event':{'user_id':int(self.source_actor.native_user_id)},'profile':'test','deadline':self.execution_deadline},max_parallelism=1,max_steps=5,now=self.now)
        if pipeline:
            # prepare_task stores the requested EntryDecision checkpoint; the
            # actual Coordinator's planning phase separately persists plan_json
            # before creating runs. Model that already-planned old source too.
            self.tasks.set_task_state(old.task_id,'queued',plan={'mode':'workflow',
                'steps':list(decision.steps),'contract':decision.contract.as_payload()},now=self.now)
            old=self.tasks.get(old.task_id)
        first=self.tasks.create_run(old.task_id,TaskStep('first','analyst','TEST already inspected','TEST findings'),allowed_tools=(),model_profile='test',now=self.now)
        self.tasks.finish_run(first.run_id,'succeeded',result={'status':'success','summary':'TEST previous immutable findings','artifacts':[]},now=self.now)
        second=self.tasks.create_run(old.task_id,TaskStep('produce' if pipeline else 'second','coder','TEST pending project edit',ARTIFACT_NAME if pipeline else 'TEST project update',('first',)),allowed_tools=('sandbox_create','sandbox_exec') if pipeline else (),model_profile='test',now=self.now)
        if pending_attempt:self.query('UPDATE subagent_runs SET attempt=? WHERE run_id=?',(pending_attempt,second.run_id))
        self.old_task,self.first,self.second=old,first,second
        return SourceRef('project_task',str(old.task_id))


    async def plan(self,source):
        self.plan_value=await self.service.console.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,action='plan',request=dict(source_endpoint=self.source.key,source_scope=self.source_scope.key,target_endpoint=self.endpoint.key,items=[{'kind':source.kind,'key':source.key}],deadline=self.now+500))
        for action in ('freeze','drain'):await self.action(action)
        return self.plan_value


    async def action(self,action):
        return await self.service.console.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,action=action,request={'migration_id':self.plan_value['migration_id']},preview_digest=self.plan_value['preview_digest'])


    async def verified(self):
        from tests import test_qq_official_receipts as receipts
        constructor=receipts.OfficialAttemptContext
        physical=dict(self.query('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?',(self.endpoint.key,))[0])
        def context(token,expected,fields):
            material=dict(fields)
            material.update(endpoint_generation=physical['generation'],route_epoch=physical['route_epoch'],capability_revision=physical['capability_revision'])
            return constructor(token,expected,tuple(material.items()))
        with patch.object(receipts,'OfficialAttemptContext',side_effect=context):
            await activation_fixture.MigrationActivationTests.verified(self)


    def window(self,**kwargs):
        result=super().window(**kwargs)
        self.query('UPDATE qq_official_reply_windows w SET endpoint_generation=e.generation,route_epoch=e.route_epoch FROM qq_official_endpoints e WHERE w.endpoint_key=e.endpoint_key AND window_id=?',(result,))
        return result


    def provider(self,job,purpose='execute'):
        with self.tx() as cursor:
            return BackgroundProjectProvider.load(cursor,token=self.token,job=job,kind='task',binding_id=self.binding()['target_task_id'],admission=self.store.admitted,route_guard=self.service.project_grants._route,project_scope_guard=self.service.project_grants.project_scope_guard,clock=lambda:self.now,purpose=purpose,migration_authority=self.engine)


    def binding(self):return self.query('SELECT * FROM migration_subagent_continuations')[0]


    async def setup_pipeline(self):
        from dataclasses import replace
        from src.plugins.ai_chat.config import settings
        from src.plugins.ai_chat.project_lifecycle import ProjectLifecycle
        from src.plugins.ai_chat.adapters.qq_official.file_services import FileServiceConfig,FileServices
        from src.plugins.ai_chat.adapters.qq_official.project_artifacts import ProjectArtifactAuthority
        from src.plugins.ai_chat.adapters.qq_official.project_sessions import ProjectTaskSessions
        from src.plugins.ai_chat.adapters.qq_official.subagent_runtime import OfficialSubAgentDispatcher
        source=self.source_task(pipeline=True)
        # The controlled source workspace reports its actual empty live bytes;
        # preserved version blobs are charged separately by the real store.
        from src.plugins.ai_chat.project_usage import ProjectUsageStore
        project=self.projects.get(self.old_access,self.project['project_id'])
        ProjectUsageStore(self.projects).record(self.old_access,project['project_id'],
            dict(project_id=project['project_id'],generation=project['workspace_generation'],
                 fence=project['lifecycle_fence'],quota_mode='soft',quota_bytes=project['quota_bytes'],
                 used_bytes=0,sampled_at=self.now,complete=True),expected_version=project['version'],now=self.now)
        await self.stage();await self.plan(source);await self.action('switch')
        await self.verified();await self.action('complete')
        bound=self.binding();self.task=self.tasks.get(bound['target_task_id'])
        self.jobs.lease_seconds=3600
        self.assertIs(self.task.plan['contract']['delivery_required'],True)
        self.task_job=self.jobs.claim_due('TEST project pipeline',kinds=(self.jobs.get(bound['job_id']).kind,),now=self.now)[0]
        self.manager=MigrationProjectGuest();self.manager.pid=self.project['project_id'];self.manager.baseline=self.query('SELECT manifest_sha256 FROM project_versions WHERE project_id=?',(self.project['project_id'],))[0][0]
        self.authority=ProjectArtifactAuthority(token=self.token,clock=lambda:self.now,route_guard=self.service.project_grants._route,project_scope_guard=self.service.project_grants.project_scope_guard,migration_projects=self.engine)
        temp=TemporaryDirectory(prefix='TEST_migration_project_files_');self.addCleanup(temp.cleanup)
        self.runtime._stopping=False;self.runtime._token=self.token
        self.files=FileServices(self.runtime,self.token,None,FileServiceConfig(Path(temp.name),self.manager,generation_enabled=True,route_authority=lambda *_:True,source_authority=self.authority,min_free_bytes=0,link_origin='https://files.example',link_reachable=lambda:True))
        self.runtime.file_service=self.files
        project_runtime=SimpleNamespace(store=self.projects,lifecycle=ProjectLifecycle(self.projects,self.jobs),ready=True,closed=False,backends={'oci':SimpleNamespace(preflight=lambda _:None)})
        self.app=SimpleNamespace(project_runtime=project_runtime,subagent_store=self.tasks,subagent_coordinator=self.coordinator,model_catalog=self.catalog,logger=Mock(),db_call=self.calls,settings=replace(settings,projects_enabled=True,search_enabled=False),message_ledger=self.ledger,context_store=None,pin_store=None,semantic_recall=None,long_term_memory=None,skill_registry=SimpleNamespace(get=lambda _:None,list=lambda:[]),self_source=Mock())
        self.sessions=ProjectTaskSessions(self.runtime,self.token,self.app,authority=self.authority)
        self.service.ports.artifacts=SimpleNamespace(validate_bound_artifact=self.authority.validate_bound_artifact)
        self.dispatcher=OfficialSubAgentDispatcher(token=self.token,coordinator=self.coordinator,jobs=self.jobs,outbox=self.outbox,transaction=self.tx,database_calls=self.calls,admission=self.store.admitted,profile=self.catalog.try_resolve,clock=lambda:self.now,execution_factory=self.sessions.prepare,project_finalizer=self.sessions.finalize,project_authority=self.sessions.assert_current,migration_authority=self.engine)
        self.model_calls=0;self.sequence=0


    async def recorded_file(self,age_before_send=0):
        # This narrow fixture represents already recorded producer bytes; the
        # separate full pipeline proves the real Coordinator capture path.
        from tests.qq_official_file_fixture import PDF
        await self.setup_pipeline()
        with self.tx() as cursor:
            self.engine.begin_job(cursor,job=self.task_job,now=self.now)
        self.tasks.set_task_state(self.task.task_id,'running',now=self.now)
        with self.tx() as cursor:
            _,access=self.sessions._provider(cursor,self.task_job,self.tasks.get(self.task.task_id))
            source=self.authority.issue(cursor,access=access,project_id=self.project['project_id'])
        record=await self.calls(self.files.artifacts.register,source,content=PDF,
            filename='TEST-migrated-project.pdf',logical_request_key=digest('TEST recorded project artifact'))
        self.tasks.set_task_state(self.task.task_id,'completed',result={'summary':'TEST recorded checkpoint'},now=self.now)
        if age_before_send:
            import time
            epoch=self.now+age_before_send;started=time.monotonic()
            with patch.object(type(self),'now',property(lambda _:epoch+int(time.monotonic()-started)),create=True):
                await self.send_project_file(record.artifact_id)
        else:
            await self.send_project_file(record.artifact_id)


    async def sample_project_usage(self):
        from src.plugins.ai_chat.project_usage import ProjectUsageSampler,ProjectUsageStore
        # The fixture owns an empty original project workspace. Its immutable
        # saved baseline is charged independently by the real ProjectStore.
        # Sample it anew through the existing checked host API; never refresh a
        # source, grant, recipient timestamp or old observation in place.
        def access():
            with self.tx() as cursor:
                _,issued=self.sessions._provider(cursor,self.task_job,self.tasks.get(self.task.task_id))
                project=self.projects._get(cursor,issued,self.project['project_id'],operation='project.execute')
                return issued,dict(project)
        issued,project=await self.calls(access)
        async def sample(current):
            self.assertEqual(current['project_id'],self.project['project_id'])
            return dict(project_id=current['project_id'],generation=current['workspace_generation'],
                fence=current['lifecycle_fence'],quota_mode='soft',quota_bytes=current['quota_bytes'],
                used_bytes=sum(map(len,getattr(self.manager,'original_project_files',{}).values())),
                sampled_at=self.now,complete=True)
        sampler=ProjectUsageSampler(ProjectUsageStore(self.projects),
            {'oci':SimpleNamespace(sample_usage=sample)},resolve_access=lambda *_:None,enabled=True)
        await sampler.sample_one(project,source=issued)
        self.usage_sampled_at=self.now


    async def project_model(self,prompt,messages,tools,execute_tool,**kwargs):
        import json
        from src.project_update import ARTIFACT_NAME
        from src.plugins.ai_chat.agent.execution import active_agent_step
        self.model_calls+=1
        invoke=lambda name,args:self.invoke(execute_tool,kwargs['event_sink'],name,args)
        sid=(await invoke('sandbox_create',{'runtime':'python'}))['sandbox_id']
        await invoke('import_project_baseline',{'sandbox_id':sid})
        if '/acceptance_r' in active_agent_step.get():
            copies=[await invoke('import_agent_artifact',{'step_id':'produce','artifact_index':index,'sandbox_id':sid})
                    for index in (0,1)]
            checked=await invoke('sandbox_exec',{'sandbox_id':sid,'command':'inspect-project-deliverables '+
                ' '.join(item['path'].removeprefix('/workspace/') for item in copies)})
            return json.dumps(dict(status='success',summary='TEST checked exact update, PDF and baseline',
                artifacts=[],findings=[],completed=[dict(description='TEST independent output checks',
                evidence_refs=[checked['_task_evidence']['ref']])],authorization=[],next_verification=[],metadata=dict(
                    artifact_reviews=[dict(artifact_key=item['sha256'],status='passed',reason='TEST checked exact captured bytes') for item in copies],
                    criterion_reviews=[dict(criterion_index=0,status='passed',reason='TEST checked update and PDF',
                        evidence_refs=[checked['_task_evidence']['ref']])])),ensure_ascii=False)
        made=await invoke('sandbox_exec',{'sandbox_id':sid,'command':'make-project-update-and-report'})
        return json.dumps(dict(status='success',summary='TEST produced update and separate requested PDF',findings=[],
            completed=[dict(description='TEST actual project outputs',evidence_refs=[made['_task_evidence']['ref']])],
            authorization=[],next_verification=[],artifacts=[dict(handle=sid+':/workspace/'+name,name=name)
                for name in (ARTIFACT_NAME,'report.pdf')]))


    async def _assert_full_pipeline(self):
        from tests import test_qq_official_project_sessions as pipeline_fixture
        from unittest.mock import AsyncMock
        original_invoke=pipeline_fixture.ProjectSessionTests.invoke.__get__(self)
        async def traced_invoke(execute_tool,sink,name,args):
            print('PROJECT_TOOL_START',name,flush=True)
            if self.now-getattr(self,'usage_sampled_at',0)>=30:
                await self.sample_project_usage()
            result=await original_invoke(execute_tool,sink,name,args)
            print('PROJECT_TOOL_DONE',name,flush=True)
            return result
        self.invoke=traced_invoke
        model=self.project_model
        with patch('src.plugins.ai_chat.subagents.ask_deepseek_with_tools',new=model),patch('src.plugins.ai_chat.subagents.ask_deepseek',new=AsyncMock(return_value='TEST migrated project update verified')),patch('src.plugins.ai_chat.storage.jobs.time',SimpleNamespace(time=lambda:self.now)),patch('src.plugins.ai_chat.project_publication.time',SimpleNamespace(time=lambda:self.now)),patch('src.plugins.ai_chat.project_materialization.time',SimpleNamespace(time=lambda:self.now)),patch('src.plugins.ai_chat.project_usage.time',SimpleNamespace(time=lambda:self.now)):
            try:result=await self.dispatcher.execute(self.task_job)
            except JobDeferred as error:
                if error.__context__ is not None:raise error.__context__
                raise
        task=self.tasks.get(self.task.task_id)
        self.assertEqual(self.model_calls,2,{'result':result,'task':task.result,'log':self.coordinator.logger.mock_calls})
        self.assertIn(task.status,{'completed','partial'},task.result)
        self.assertEqual(len(self.query('SELECT * FROM project_task_publications')),1,task.result)
        self.assertEqual(len(self.query('SELECT * FROM project_task_materializations')),1,task.result)
        manifests=self.query('SELECT * FROM qq_official_file_artifacts')
        self.assertEqual(len(manifests),1,{'result':result,'task':task.result,'logs':self.coordinator.logger.mock_calls})
        manifest=manifests[0]
        self.assertTrue(manifest['filename'].endswith('report.pdf'))
        self.assertEqual(manifest['source_extension_kind'],'project');self.assertEqual(manifest['source_migration_item_id'],self.binding()['item_id'])
        self.assertTrue(all(manifest[key] is None for key in ('source_execution_id','source_canonical_message_id','source_native_message_id')))
        self.assertEqual(len(self.query('SELECT * FROM qq_official_project_artifacts')),1)
        self.assertEqual(len(self.query('SELECT * FROM qq_official_migration_artifacts')),1)
        self.assertEqual(self.query('SELECT * FROM subagent_deliveries'),[])
        self.assertTrue(result['plan_id'])
        self.assertEqual(self.tasks.runs(self.old_task.task_id)[0].status,'succeeded')
        self.assertEqual(self.tasks.runs(self.old_task.task_id)[1].attempt,0)
        self.assertEqual(self.manager.items,{})
        await self.send_project_file(manifest['artifact_id'])


    def fresh_target_message(self):
        import secrets
        from datetime import datetime,timezone
        from src.plugins.ai_chat.adapters.qq_official.ingest import process_inbox
        from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
        from src.plugins.ai_chat.adapters.qq_official.actor_evidence import recent_actor_evidence
        checkpoint=self.store.checkpoint(self.token)
        self.assertIsNotNone(checkpoint)
        native='TEST_NEW_PROJECT_TARGET_'+secrets.token_hex(8)
        accepted=self.store.accept({'op':0,'t':'GROUP_MESSAGE_CREATE','s':checkpoint.seq+1,
            'd':{'id':native,'group_openid':self.endpoint.native_conversation_id,
                 'author':{'member_openid':self.actor.native_user_id},
                 'content':'TEST target is ready for the existing project artifact',
                 'timestamp':datetime.fromtimestamp(self.now,timezone.utc).isoformat()}},
            token=self.token,session_id=checkpoint.session_id)
        self.assertIsNotNone(accepted.inbox_id)
        with self.tx() as cursor:
            processed=process_inbox(cursor,inbox_id=accepted.inbox_id,lease_token=self.token,
                ledger=self.ledger,jobs=self.jobs,epoch_guard=assert_epoch,admission=self.store.admitted,now=self.now)
            self.assertIsNotNone(processed.canonical_message_id)
            self.assertTrue(recent_actor_evidence(cursor,endpoint=self.endpoint,actor=self.actor,now=self.now))
        self.assertEqual(len(self.query("SELECT * FROM qq_official_reply_windows WHERE credential_value=? AND state='active'",(native,))),1)
        return native


    async def send_project_file(self, artifact_id):
        from dataclasses import replace
        from src.plugins.ai_chat.adapters.qq_official.file_services import NativeRouteEvidence
        from src.plugins.ai_chat.adapters.qq_official.file_artifacts import file_job_kind
        from src.plugins.ai_chat.adapters.qq_official.sender import OfficialSender
        from src.plugins.ai_chat.workers.durable_jobs import DurableJobWorker
        from src.qq_official.credentials import Credentials
        from src.qq_official.http import RestClient
        from src.qq_official.token import TokenManager
        from tests.test_qq_official_file_dispatcher import FileMediaFake
        fake=FileMediaFake();fake.app_id=self.account.app_id
        fake.clock=lambda:self.now;fake.receipt_ttl=1800
        await fake.start()
        tokens=TokenManager(Credentials(self.account.app_id,fake.app_secret),fake.base_url,allow_test_server=True)
        rest=RestClient(tokens,allow_test_server=True)
        async def current():await self.calls(self.files._fence_sync)
        sender=OfficialSender(self.account,self.token,self.outbox,rest,self.ledger,
            database_calls=self.calls,ensure_current=current,wall_clock=lambda:self.now)
        try:
            physical=self.query('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?',(self.endpoint.key,))[0]
            evidence=NativeRouteEvidence(self.endpoint,physical['generation'],physical['route_epoch'],
                physical['capability_revision'],1024*1024,self.now,self.now+1800)
            self.files.config=replace(self.files.config,native_evidence=lambda *_:evidence)
            self.files.dispatcher.rest=rest
            self.outbox.protected_payloads=self.files.payloads
            # Explicit loopback host policy permits the background result to
            # use a new actual window; it does not fabricate a source message.
            self.outbox.allow_newer_windows=True
            # Fresh input is a new authenticated frame through the real M2
            # receipt/ledger path. Never retimestamp an old source or grant.
            from src.plugins.ai_chat.adapters.qq_official.actor_evidence import recent_actor_evidence
            with self.tx() as cursor:
                fresh=recent_actor_evidence(cursor,endpoint=self.endpoint,actor=self.actor,now=self.now)
            worker=DurableJobWorker(self.jobs,logger=Mock(),database_calls=self.calls)
            worker.register(file_job_kind(self.account),self.files.dispatcher)
            if not fresh:
                original=dict(self.query('SELECT * FROM qq_official_file_artifacts WHERE artifact_id=?',(artifact_id,))[0])
                with patch('src.plugins.ai_chat.storage.jobs.time',SimpleNamespace(time=lambda:self.now)):
                    self.assertEqual(await worker.run_once(),1)
                waiting=self.query('SELECT * FROM qq_official_file_deliveries WHERE artifact_id=?',(artifact_id,))[0]
                waiting_job=self.jobs.get(waiting['job_id'])
                self.assertEqual(waiting_job.status,'pending',waiting_job)
                self.assertNotEqual(waiting['state'],'cancelled',waiting)
                self.assertEqual(fake.media_calls,[]);self.assertEqual(fake.received,[])
                self.assertEqual(self.query('SELECT * FROM qq_official_file_stage_attempts WHERE artifact_id=?',(artifact_id,)),[])
                self.assertEqual(dict(self.query('SELECT * FROM qq_official_file_artifacts WHERE artifact_id=?',(artifact_id,))[0]),original)
            source=self.fresh_target_message()
            fake.add_window('group',self.endpoint.native_conversation_id,source)
            if not fresh:
                import asyncio
                # The real existing job delay elapses. Do not edit its source,
                # deadline, attempt identity or next-attempt timestamp.
                await asyncio.sleep(max(0,waiting_job.next_attempt_at-self.now)+1)
            with patch('src.plugins.ai_chat.storage.jobs.time',SimpleNamespace(time=lambda:self.now)):
                self.assertEqual(await worker.run_once(),1)
            artifact=self.query('SELECT * FROM qq_official_file_deliveries WHERE artifact_id=?',(artifact_id,))[0]
            self.assertEqual(artifact['state'],'handed_off')
            self.assertEqual(len(fake.media_calls),1)
            self.assertEqual(fake.received,[])
            relation=self.query('SELECT * FROM qq_official_file_message_plans WHERE artifact_id=?',(artifact_id,))[0]
            original=self.outbox.finalize
            def late_receipt(*args,**kwargs):
                # Actual acceptance is immutable historical evidence: revoking
                # the grant after HTTP must not erase it or trigger a resend.
                self.query("UPDATE qq_official_project_grants SET state='revoked',revision=revision+1,revoked_at=? WHERE grant_id=?",(self.now,self.staged['grant_id']))
                return original(*args,**kwargs)
            with patch.object(self.outbox,'finalize',side_effect=late_receipt):
                outcome=await sender.send_delivery(relation['delivery_id'])
            self.assertEqual(outcome.status,'committed',outcome)
            self.assertEqual(len(fake.received),1)
            self.assertEqual(fake.received[0]['payload']['msg_type'],7)
            self.assertEqual(self.query('SELECT state FROM qq_official_file_deliveries WHERE artifact_id=?',(artifact_id,))[0]['state'],'message_accepted')
            await sender.send_delivery(relation['delivery_id'])
            self.assertEqual(len(fake.received),1)
            with self.tx() as cursor,self.assertRaises((ValueError,PermissionError,LeaseLost)):
                self.migration.current_artifact_plan_source(cursor,relation['plan_id'],now=self.now)
        finally:
            await sender.stop();await rest.aclose();await tokens.aclose();await fake.close()



class ProjectContinuationTests(ProjectContinuationFixture):
    async def test_actual_console_stage_then_switch_keeps_original_and_parks_new_exact_project(self):
        source=self.source_task();await self.stage();await self.plan(source);await self.action('switch')
        bound=self.binding();target=self.tasks.get(bound['target_task_id'])
        self.assertEqual((target.project_id,target.project_base_version),(self.project['project_id'],1))
        self.assertIsNone(target.trigger_message_id);self.assertIsNone(target.requester_user_id)
        self.assertEqual(target.migration_item_id,bound['item_id'])
        self.assertEqual((self.jobs.get(bound['job_id']).attempts,self.jobs.get(bound['job_id']).next_attempt_at),(0,PARKED))
        self.assertEqual(self.tasks.runs(self.old_task.task_id)[0].status,'succeeded')
        self.assertEqual(self.tasks.runs(self.old_task.task_id)[1].status,'skipped')
        self.assertEqual(self.tasks.runs(target.task_id)[0].result['summary'],'TEST previous immutable findings')
        self.assertEqual(self.query('SELECT locked_task_id FROM projects')[0][0],target.task_id)
        release=self.query('SELECT * FROM project_task_releases')[0]
        self.assertEqual((release['task_id'],release['revision'],release['task_status']),(self.old_task.task_id,1,'cancelled'))
        original=self.query('SELECT * FROM project_task_bindings WHERE task_id=?',(self.old_task.task_id,))[0]
        actual=self.query('SELECT * FROM project_task_bindings WHERE task_id=?',(target.task_id,))[0]
        for key in ('project_id','base_version','manifest_sha256','generation','fence'):self.assertEqual(original[key],actual[key])
        authority=self.query('SELECT * FROM qq_official_project_task_authority')[0]
        self.assertEqual((authority['grant_id'],authority['grant_revision'],authority['route_epoch']),(self.staged['grant_id'],1,2))
        await self.action('switch');self.assertEqual(self.binding()['target_task_id'],target.task_id)
        self.assertEqual(self.jobs.claim_due('TEST early',kinds=(self.jobs.get(bound['job_id']).kind,),now=self.now),[])


    async def test_file_delivery_contract_is_frozen_from_the_original_plan(self):
        source=self.source_task(pipeline=True)
        original=self.tasks.get(self.old_task.task_id).plan['contract']
        self.assertIs(original['delivery_required'],True)
        self.assertIn('report.pdf',original['deliverables'])
        await self.stage();await self.plan(source);await self.action('switch')
        target=self.tasks.get(self.binding()['target_task_id'])
        self.assertEqual(target.plan['contract'],original)
        self.assertEqual(self.jobs.get(self.binding()['job_id']).next_attempt_at,PARKED)
        self.assertEqual(self.query('SELECT * FROM qq_official_file_artifacts'),[])


    async def test_real_delivery_selector_keeps_pdf_and_internal_update_separate(self):
        from dataclasses import replace
        from src.project_update import ARTIFACT_NAME
        from src.plugins.ai_chat.subagents import _delivery_outcomes,_outcome_from_run,tool_enabled
        source=self.source_task(pipeline=True);await self.stage();await self.plan(source);await self.action('switch')
        target=self.tasks.get(self.binding()['target_task_id'])
        self.assertIs(target.plan['contract']['delivery_required'],True)
        self.assertTrue(tool_enabled('send_file_from_sandbox'))
        producer=next(run for run in self.tasks.runs(target.task_id) if run.step_key=='produce')
        self.assertEqual(producer.role,'coder')
        # Selection is a pure projection over real stored task/run identities;
        # these synthetic result DTOs are never captured, registered or sent.
        result={'status':'success','artifacts':[{'name':ARTIFACT_NAME},{'name':'report.pdf'}]}
        outcome=_outcome_from_run(target,replace(producer,status='succeeded',result=result))
        completed={'produce':outcome}
        visible=_delivery_outcomes(target,completed,include_project_updates=False)
        self.assertEqual([a['name'] for item in visible for a in item.result['artifacts']],['report.pdf'])
        acceptance=_delivery_outcomes(target,completed)
        self.assertEqual([a['name'] for item in acceptance for a in item.result['artifacts']],[ARTIFACT_NAME,'report.pdf'])
        self.assertEqual(self.query('SELECT * FROM qq_official_file_artifacts'),[])


    async def test_conversion_crash_rolls_back_project_lock_source_and_new_task(self):
        source=self.source_task();await self.stage();await self.plan(source)
        original=self.engine._bind_target
        def fail(*args,**kwargs):
            original(*args,**kwargs)
            raise RuntimeError('TEST commit interruption')
        with patch.object(self.engine,'_bind_target',side_effect=fail),self.assertRaises(RuntimeError):await self.action('switch')
        self.assertEqual(self.query('SELECT phase FROM migration_routes')[0][0],'draining')
        self.assertEqual(self.query('SELECT locked_task_id FROM projects')[0][0],self.old_task.task_id)
        self.assertEqual(self.tasks.get(self.old_task.task_id).status,'queued')
        self.assertEqual(self.tasks.runs(self.old_task.task_id)[1].status,'pending')
        self.assertEqual(self.query('SELECT * FROM migration_subagent_continuations'),[])
        self.assertEqual(self.query('SELECT * FROM project_task_releases'),[])
        self.assertEqual(self.query('SELECT count(*) FROM subagent_tasks')[0][0],1)
        await self.action('switch');self.assertEqual(len(self.query('SELECT * FROM migration_subagent_continuations')),1)


    async def test_corrupt_original_manifest_cannot_release_source_or_transfer_lock(self):
        source=self.source_task();await self.stage();await self.plan(source)
        manifest=self.query('SELECT manifest_sha256 FROM project_versions WHERE project_id=? AND version_no=1',
            (self.project['project_id'],))[0]['manifest_sha256']
        path=self.projects.blobs.path(manifest)
        original=path.read_bytes();mode=path.stat().st_mode & 0o777
        try:
            path.chmod(0o600)
            path.write_bytes(b'TEST corrupted original manifest')
            with self.assertRaises((ValueError,PermissionError,ProjectConflict,OSError)):
                await self.action('switch')
        finally:
            path.write_bytes(original);path.chmod(mode)
        self.assertEqual(self.query('SELECT * FROM project_task_releases'),[])
        self.assertEqual(self.query('SELECT * FROM migration_subagent_continuations'),[])
        self.assertEqual(self.tasks.get(self.old_task.task_id).status,'queued')
        self.assertEqual(self.query('SELECT locked_task_id FROM projects')[0][0],self.old_task.task_id)


    async def test_revoked_exact_grant_prevents_conversion_without_reselecting(self):
        source=self.source_task();await self.stage();await self.plan(source)
        await self.service.console.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,action='project_grant_revoke',request={'grant_id':self.staged['grant_id'],'expected_revision':1})
        with self.assertRaises((MigrationDenied,ConversionDenied,PermissionError,PreviewDenied)):await self.action('switch')
        self.assertEqual(self.query('SELECT locked_task_id FROM projects')[0][0],self.old_task.task_id)
        self.assertEqual(self.query('SELECT * FROM migration_subagent_continuations'),[])


    async def test_unknown_original_step_never_becomes_target_job(self):
        source=self.source_task(pending_attempt=1);await self.stage();await self.plan(source)
        await self.action('switch')
        self.assertEqual(self.query('SELECT * FROM migration_subagent_continuations'),[])
        self.assertEqual(self.query('SELECT classification FROM migration_items')[0][0],'unknown')
        self.assertEqual(self.tasks.runs(self.old_task.task_id)[1].attempt,1)


    async def test_complete_activates_only_exact_job_and_original_staged_grant(self):
        source=self.source_task();await self.stage();await self.plan(source);await self.action('switch')
        await self.verified();await self.action('complete')
        bound=self.binding();job=self.jobs.claim_due('TEST target project',kinds=(self.jobs.get(bound['job_id']).kind,),now=self.now)[0]
        with self.tx() as cursor:self.engine.begin_job(cursor,job=job,now=self.now)
        for invalid_now in (True, -1, float('nan'), float('inf')):
            with self.tx() as cursor,self.assertRaises(LeaseLost):
                dispatch=cursor.execute('SELECT * FROM qq_official_subagent_dispatches WHERE task_id=?',(bound['target_task_id'],)).fetchone()
                target=cursor.execute('SELECT * FROM subagent_tasks WHERE task_id=?',(bound['target_task_id'],)).fetchone()
                self.engine.assert_current(cursor,binding=dispatch,task=target,job=job,now=invalid_now)
        provider=self.provider(job)
        with self.tx() as cursor:
            access=provider.issue_for_project(cursor,self.project['project_id'],operation=Operation.EXECUTE)
            snap=provider.require(cursor,access.official_proof,project_id=self.project['project_id'],operation=Operation.EXECUTE)
        self.assertEqual(snap.key,self.source_scope.key)
        with self.tx() as cursor:self.assertEqual(provider._pin(cursor,self.project['project_id'])[0].grant_id,self.staged['grant_id'])
        self.assertIsNone(self.tasks.get(bound['target_task_id']).trigger_message_id)
        self.assertEqual(self.projects.get(access,self.project['project_id'])['project_id'],self.project['project_id'])
        self.query('UPDATE qq_official_project_grants SET revision=revision+1 WHERE grant_id=?',(self.staged['grant_id'],))
        with self.assertRaises((MigrationDenied,PermissionError,ConversionDenied,LeaseLost)):self.provider(job)
        with self.tx() as cursor,self.assertRaises((MigrationDenied,PermissionError,ConversionDenied,LeaseLost)):
            provider.issue_for_project(cursor,self.project['project_id'])


    async def test_completion_without_receipts_and_stale_job_cannot_start_project(self):
        source=self.source_task();await self.stage();await self.plan(source);await self.action('switch')
        with self.assertRaises(MigrationDenied):await self.action('complete')
        self.assertEqual(self.jobs.get(self.binding()['job_id']).next_attempt_at,PARKED)
        await self.verified();await self.action('complete')
        job=self.jobs.claim_due('TEST exact project job',kinds=(self.jobs.get(self.binding()['job_id']).kind,),now=self.now)[0]
        with self.tx() as cursor:self.engine.begin_job(cursor,job=job,now=self.now)
        self.provider(job)
        self.query('UPDATE durable_jobs SET lease_generation=lease_generation+1 WHERE job_id=?',(job.job_id,))
        with self.assertRaises((MigrationDenied,PermissionError,ConversionDenied,LeaseLost)):self.provider(job)


    async def test_source_read_composition_cannot_extend_lease_or_survive_return(self):
        from contextvars import copy_context
        source=self.source_task();await self.stage();await self.plan(source);await self.action('switch')
        await self.verified();await self.action('complete')
        bound=self.binding();job=self.jobs.claim_due('TEST read composition',kinds=(self.jobs.get(bound['job_id']).kind,),now=self.now)[0]
        provider=self.provider(job)
        original_current=provider._current
        original_now=self.now
        self.query('UPDATE durable_jobs SET lease_until=? WHERE job_id=?',(self.now+1,job.job_id))
        crossed=False
        def cross_after_check(cursor):
            nonlocal crossed
            value=original_current(cursor)
            if not crossed:
                crossed=True;self.now+=1
            return value
        try:
            with patch.object(provider,'_current',side_effect=cross_after_check):
                with self.tx() as cursor,self.assertRaises((PermissionError,LeaseLost,ValueError)):
                    provider.issue_for_project(cursor,self.project['project_id'])
        finally:self.now=original_now
        self.query('UPDATE durable_jobs SET lease_until=? WHERE job_id=?',(self.now+600,job.job_id))
        contexts=[];principal=provider.authority._principal
        def capture_context(*args,**kwargs):
            contexts.append(copy_context())
            return principal(*args,**kwargs)
        with self.tx() as cursor:
            with patch.object(provider.authority,'_principal',side_effect=capture_context):
                provider.issue_for_project(cursor,self.project['project_id'])
            self.assertTrue(contexts)
            cursor.execute('UPDATE durable_jobs SET lease_generation=lease_generation+1 WHERE job_id=?',(job.job_id,))
            # Same cursor, transaction and second; copied Context still cannot
            # reuse the completed call's source check after the job changes.
            with self.assertRaises((PermissionError,LeaseLost,ValueError)):
                contexts[0].run(provider._current,cursor)


    async def test_same_call_new_second_rechecks_once_but_expiry_still_denies(self):
        from src.plugins.ai_chat import project_background as background
        source=self.source_task();await self.stage();await self.plan(source);await self.action('switch')
        await self.verified();await self.action('complete')
        bound=self.binding();job=self.jobs.claim_due('TEST same call second',kinds=(self.jobs.get(bound['job_id']).kind,),now=self.now)[0]
        provider=self.provider(job)
        original=background.assert_epoch;checks=[]
        def record(cursor,token):
            checks.append(self.now)
            return original(cursor,token)
        with patch.object(background,'assert_epoch',side_effect=record):
            with self.tx() as cursor,provider._read_composition(cursor):
                self.assertEqual(len(checks),1)
                self.now+=1
                provider._current(cursor);provider._current(cursor)
                self.assertEqual(len(checks),2)
                # No business mutation occurs in the composition: only the
                # actual pre-existing job lease expiry crosses the clock.
                self.now=int(job.lease_until)
                with self.assertRaises((ValueError,PermissionError,LeaseLost)):
                    provider._current(cursor)
                with self.assertRaises((ValueError,PermissionError,LeaseLost)):
                    provider._current(cursor)
                self.assertEqual(len(checks),4)


    async def test_advancing_clock_single_source_cost(self):
        import sys
        import time
        from collections import Counter
        from src.bot_storage.database import _LivePostgresCursor
        source=self.source_task();await self.stage();await self.plan(source);await self.action('switch')
        await self.verified();await self.action('complete')
        bound=self.binding();job=self.jobs.claim_due('TEST moving clock cost',kinds=(self.jobs.get(bound['job_id']).kind,),now=self.now)[0]
        with self.tx() as cursor:self.engine.begin_job(cursor,job=job,now=self.now)
        base=self.now;start=time.monotonic()
        original_execute=_LivePostgresCursor.execute
        def measure(label,operation):
            delta=Counter();began=time.monotonic()
            def counted(cursor,*args,**kwargs):
                frame=sys._getframe(1)
                delta[(Path(frame.f_code.co_filename).name,frame.f_code.co_name,frame.f_lineno)]+=1
                return original_execute(cursor,*args,**kwargs)
            with patch.object(_LivePostgresCursor,'execute',new=counted):
                value=operation()
            print('SOURCE_COST',label,'SECONDS',round(time.monotonic()-began,3),
                  'SQL',sum(delta.values()),'TOP',delta.most_common(7),flush=True)
            return value
        with patch.object(type(self),'now',property(lambda _:base+int(time.monotonic()-start)),create=True):
            with self.tx() as cursor:
                dispatch=dict(cursor.execute('SELECT * FROM qq_official_subagent_dispatches WHERE task_id=?',(bound['target_task_id'],)).fetchone())
                task=dict(cursor.execute('SELECT * FROM subagent_tasks WHERE task_id=?',(bound['target_task_id'],)).fetchone())
                measure('engine.assert_current',lambda:self.engine.assert_current(cursor,binding=dispatch,task=task,job=job,now=self.now))
            provider=measure('BackgroundProvider.load',lambda:self.provider(job))
            with self.tx() as cursor:
                measure('BackgroundProvider._current',lambda:provider._current(cursor))
            with self.tx() as cursor:
                measure('BackgroundProvider.issue_for_project',lambda:provider.issue_for_project(cursor,self.project['project_id']))


    async def test_recorded_project_artifact_uses_real_file_worker_and_historical_receipt(self):
        await self.recorded_file()


    async def test_recorded_artifact_waits_for_fresh_input_without_reissuing_source(self):
        await self.recorded_file(age_before_send=301)


    async def test_usage_sampler_refreshes_only_measured_project_bytes(self):
        await self.setup_pipeline()
        before=dict(self.query('SELECT * FROM qq_official_project_grants WHERE grant_id=?',(self.staged['grant_id'],))[0])
        source_before=dict(self.query('SELECT * FROM qq_official_subagent_dispatches WHERE task_id=?',(self.task.task_id,))[0])
        with self.tx() as cursor:self.engine.begin_job(cursor,job=self.task_job,now=self.now)
        self.tasks.set_task_state(self.task.task_id,'running',now=self.now)
        self.now+=91
        with patch('src.plugins.ai_chat.project_usage.time',SimpleNamespace(time=lambda:self.now)):
            await self.sample_project_usage()
        project=self.query('SELECT * FROM projects WHERE project_id=?',(self.project['project_id'],))[0]
        self.assertEqual((project['used_bytes'],project['sampled_at']),(0,self.now))
        self.assertEqual(dict(self.query('SELECT * FROM qq_official_project_grants WHERE grant_id=?',(self.staged['grant_id'],))[0]),before)
        source_after=dict(self.query('SELECT * FROM qq_official_subagent_dispatches WHERE task_id=?',(self.task.task_id,))[0])
        for key in ('execution_deadline_at','migration_result_deadline_at','endpoint_generation','route_epoch'):
            self.assertEqual(source_after[key],source_before[key])


    async def test_actual_resumed_coordinator_pinned_project_update_and_m6_manifest(self):
        import time
        await self.setup_pipeline()
        epoch=self.now;started=time.monotonic()
        # Every original expiry remains unchanged. Authorization, usage, source
        # and fresh-actor checks see real elapsed seconds throughout the chain.
        with patch.object(type(self),'now',property(lambda _:epoch+int(time.monotonic()-started)),create=True):
            await self._assert_full_pipeline()
