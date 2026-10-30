"""Protected host configuration and the real late ProjectRuntime composition.

PostgreSQL, the existing runtime and journal are real. Only the private Unix
runner's Docker/kernel backend is local fake; no source/grant callback permits
an action merely because this composition test selected a profile.
"""
import time

from src.plugins.ai_chat.adapters.qq_official.migration_types import MigrationDenied
from src.plugins.ai_chat.adapters.qq_official.migration_project_tasks import ProjectTaskContinuations
from src.plugins.ai_chat.adapters.qq_official.migration_project_artifacts import ProjectMigrationArtifacts
from src.plugins.ai_chat.project_runtime import ProjectRuntime
from tests import test_qq_official_project_continuation as continuation_fixture
from tests.test_qq_official_host_composition import HostCompositionFixture
from tests import test_project_runtime as project_fixture


class ProjectHostCompositionTests(HostCompositionFixture):
    source_task=continuation_fixture.ProjectContinuationFixture.source_task
    stage=continuation_fixture.ProjectContinuationFixture.stage
    plan=continuation_fixture.ProjectContinuationFixture.plan
    action=continuation_fixture.ProjectContinuationFixture.action
    binding=continuation_fixture.ProjectContinuationFixture.binding

    @property
    def now(self):
        return int(time.time())+self._clock_offset

    @now.setter
    def now(self,value):
        # Start the inherited fixture on real wall time BEFORE it writes its
        # first mapping/account. Never backdate an already-persisted mapping.
        self._clock_offset=value-int(time.time()) if hasattr(self,'_clock_offset') else 0

    async def project_runtime(self):
        official,context=self.runtime,self.context
        await project_fixture.ProjectRuntimeTests.asyncSetUp(self)
        projects=self.runtime
        self.runtime=official
        app=self.app
        for key in ('mobile_authorization','model_catalog','subagent_store','subagent_coordinator','fleet_client'):
            setattr(app,key,getattr(context,key))
        app.delivery_store=self.deliveries
        app.db_call=self.calls
        app.project_runtime=projects
        self.context=app
        official.application=app
        self.assertIs(projects.app,app)
        self.assertIsInstance(projects,ProjectRuntime)
        self.assertFalse(projects.ready)
        return projects

    async def test_protected_host_late_binds_one_actual_project_engine_everywhere(self):
        projects=await self.project_runtime()
        await self.build()
        self.assertNotIn('project_task',self.session.domains)
        self.assertIsNone(self.session.project_sessions)
        with self.assertRaises(MigrationDenied):
            await self.service.configure_project_runtime(projects)
        await projects.start()
        await self.service.configure_project_runtime(projects)
        engine=self.session.domains['project_task']
        authority=self.files.config.source_authority.resolve('project')
        self.assertIsInstance(engine,ProjectTaskContinuations)
        for key,value in dict(database=self.runtime.database,token=self.token,jobs=self.runtime.jobs,
                coordinator=self.context.subagent_coordinator,journal=self.service.journal,
                preview=self.service.preview,admission=self.session.admission,projects=projects.store,
                grants=self.service.project_grants,file_admission=self.service.file_admission).items():
            self.assertIs(getattr(engine,key),value,key)
        self.assertIs(authority.migration_projects,engine)
        self.assertIsInstance(authority.migration_artifacts,ProjectMigrationArtifacts)
        self.assertIs(authority.migration_artifacts.engine,engine)
        self.assertIs(authority.migration_artifacts.authority,authority)
        self.assertIs(self.session.project_resolver.migration_authority,engine)
        self.assertIs(self.session.project_sessions.authority,authority)
        self.assertIs(self.session.project_sessions.projects,projects)
        self.assertIs(self.runtime.project_service,self.session.project_sessions)
        self.assertIs(self.session.task_sources.domains['project_task'],engine)
        self.assertIn(self.session.task_sources,self.session.guard.continuations)
        self.assertNotIn(engine,self.session.guard.continuations)
        # Native and migrated tasks share the real dispatcher, while each keeps
        # its original source authority; adding the domain does not replace M4.
        self.assertIs(self.runtime.store.source_authority,self.session.native)
        self.assertIs(self.runtime.store.admitted,self.session.admission)
        await self.service.configure_project_runtime(projects)
        self.assertIs(self.session.domains['project_task'],engine)
        self.assertFalse(self.fake.received)

    async def test_no_migration_profile_keeps_native_projects_without_new_domain(self):
        projects=await self.project_runtime()
        raw=dict(self.raw);raw.pop('subagent_profile')
        await self.build(raw)
        await projects.start()
        await self.service.configure_project_runtime(projects)
        self.assertNotIn('project_task',self.session.domains)
        self.assertIsNone(self.session.project_resolver.migration_authority)
        self.assertIsNone(self.session.project_sessions.authority.migration_projects)
        self.assertIs(self.runtime.project_service,self.session.project_sessions)
        self.assertIs(self.runtime.store.source_authority,self.session.native)

    async def test_missing_file_configuration_does_not_install_project_migration(self):
        projects=await self.project_runtime()
        raw=dict(self.raw);raw.pop('files')
        await self.build(raw)
        await projects.start()
        await self.service.configure_project_runtime(projects)
        self.assertNotIn('project_task',self.session.domains)
        self.assertIsNone(self.session.project_sessions)
        self.assertFalse(self.session.configure_project_runtime(projects))

    async def test_host_binding_rejects_a_changed_original_grant_service(self):
        projects=await self.project_runtime()
        await self.build();await projects.start()
        await self.service.configure_project_runtime(projects)
        engine=self.session.domains['project_task']
        original=self.service.project_grants
        self.service.project_grants=object()
        try:
            with self.assertRaises(MigrationDenied):
                self.session.configure_project_runtime(projects)
        finally:self.service.project_grants=original
        self.assertIs(self.session.domains['project_task'],engine)
        self.assertIs(engine.grants,original)
        self.assertTrue(self.session.configure_project_runtime(projects))

    async def test_missing_coordinator_fails_before_publishing_partial_engine(self):
        projects=await self.project_runtime()
        await self.build();await projects.start()
        original=self.context.subagent_coordinator
        self.context.subagent_coordinator=None
        try:
            with self.assertRaises(MigrationDenied):
                self.session.configure_project_runtime(projects)
        finally:self.context.subagent_coordinator=original
        self.assertNotIn('project_task',self.session.domains)
        authority=self.files.config.source_authority.resolve('project')
        self.assertIsNone(authority.migration_projects)
        self.assertIsNone(authority.migration_artifacts)
        self.assertIsNone(self.session.project_sessions)
        self.assertTrue(self.session.configure_project_runtime(projects))

    async def test_factory_engine_routes_real_console_migration_and_rechecks_original_grant(self):
        from src.plugins.ai_chat.adapters.qq_official.admin_bindings import AdminBindingStore
        from src.plugins.ai_chat.adapters.qq_official.authority import OwnerAuthority
        from src.plugins.ai_chat.adapters.qq_official import turn_source as execution
        from src.plugins.ai_chat.adapters.qq_official.file_artifacts import foreground_source,file_job_kind
        from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
        from src.plugins.ai_chat.adapters.qq_official.native_media import CONTRACT
        from src.plugins.ai_chat.adapters.qq_official.project_authority import Denied,Operation
        from src.plugins.ai_chat.agent.control import LeaseLost
        from src.plugins.ai_chat.adapters.qq_official.migration_subagents import ConversionDenied
        from src.plugins.ai_chat.message_ir import MessageBody,TextNode
        from src.plugins.ai_chat.projects import ProjectAccess
        from src.plugins.ai_chat.project_blobs import ProjectFile
        from tests.qq_official_file_fixture import PDF
        from tests.qq_official_migration_mapping_fixture import rotate_mapping
        projects=await self.project_runtime()
        # Bootstrap only binds the existing authenticated console account to
        # its already-confirmed C2C owner. It grants no project by itself.
        with self.tx() as cursor:
            grant=OwnerAuthority(clock=lambda:self.now).owner_for_actor(cursor,self.private,self.owner)
            admins=AdminBindingStore(clock=lambda:self.now,control_guard=lambda c,**kw:kw['authorization'] is self)
            admins.bind_existing(cursor,admin_account_id='admin',admin_version=1,endpoint=self.private,
                actor=self.owner,owner_revision=grant.revision,authorization=self)
        self.mapping_inbox=self.accepted.inbox_id
        rotate_mapping(self)
        physical=self.query('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?',(self.endpoint.key,))[0]
        self.raw['files']['native_routes']=[dict(endpoint=self.endpoint.key,endpoint_generation=physical['generation'],
            route_epoch=physical['route_epoch'],capability_revision=physical['capability_revision'],
            max_bytes=1024*1024,verified_at=self.now,expires_at=self.now+600,contract=CONTRACT,file_type=4)]
        await self.build();await projects.start();await self.service.configure_project_runtime(projects)
        # OfficialRuntime installs this exact port immediately after its host
        # factory returns (runtime.py); this fixture owns no supervisor loop.
        self.runtime.outbox.protected_payloads=self.files.payloads
        self.projects=projects.store
        self.tasks=self.context.subagent_store;self.coordinator=self.context.subagent_coordinator
        self.jobs=self.runtime.jobs
        self.engine=self.session.domains['project_task'];self.migration=self.service.journal
        principal=self.ledger.ensure_principal_identity('onebot-v11',self.source_actor.native_user_id,'TEST old project owner')
        self.old_access=ProjectAccess(self.source_scope.with_actor(self.source_actor.native_user_id),principal,member=True)
        self.project=self.projects.create(self.old_access,'TEST actual factory original project',now=self.now)
        self.projects.snapshot(self.old_access,self.project['project_id'],[ProjectFile('note.txt',b'TEST original bytes')],
            expected_version=self.project['version'],now=self.now)
        self.projects.open(self.old_access,self.project['project_id'],now=self.now)
        self.execution_deadline=self.now+600
        self.request=dict(source_endpoint=self.source.key,source_scope=self.source_scope.key,source_actor=self.source_actor.key,
            target_endpoint=self.endpoint.key,target_actor=self.actor.key,issuer_actor=self.actor.key,
            project_id=self.project['project_id'],mapping_id=self.mapping_evidence.mapping_id,
            mapping_revision=self.mapping_evidence.revision,
            capabilities=['project.read','project.write','project.execute','project.deliver'],expires_at=self.now+900)
        # An actual native execution registers the bounded verification PDF
        # before freezing old ingress. No fabricated native ID or grant is used.
        job=self.jobs.claim_due('TEST_FACTORY_VERIFY',kinds=(account_job_kind('turn',self.account.key),),now=self.now)[0]
        with self.tx() as cursor:
            handle=execution.claim_execution(cursor,token=self.token,job=job,now=self.now,
                deadline_at=self.now+200,admission=self.store.admitted).handle
        turn=self.journal.start_turn(self.endpoint.scope,trigger_canonical_message_id=self.ingested.canonical_message_id,
            objective='TEST verification PDF',provider='test',model='test',started_at=self.now)
        args=dict(token=self.token,handle=handle,job=job,now=self.now,admission=self.store.admitted)
        with self.tx() as cursor:
            execution.begin_execution(cursor,**args,journal_turn_id=turn.turn_id)
            native=foreground_source(cursor,**args,pending_deadline_at=self.now+1000)
        artifact=await self.calls(self.files.artifacts.register,native,content=PDF,filename='verify.pdf',logical_request_key='a'*64)
        with self.tx() as cursor:
            execution.capture_result(cursor,**args,body=MessageBody((TextNode(0,'TEST verification'),)),turn_id=turn.turn_id,status='succeeded')
        self.jobs.mark_succeeded(job.job_id,job.lease_owner,now=self.now,lease_generation=job.lease_generation)
        source=self.source_task()
        await self.stage();await self.plan(source);await self.action('switch');await self.action('verify')
        self.migration_plan=self.plan_value
        text=await self.session.request_verification(cookie=self.mapping_cookie,csrf=self.mapping_csrf,
            migration_id=self.plan_value['migration_id'],kind='text',inbox_id=self.accepted.inbox_id)
        self.assertEqual((await self.sender.send_delivery(text.delivery_ids[0])).status,'committed')
        await self.session.maintain()
        await self.session.request_verification(cookie=self.mapping_cookie,csrf=self.mapping_csrf,
            migration_id=self.plan_value['migration_id'],kind='file',inbox_id=self.accepted.inbox_id,
            artifact_id=artifact.artifact_id)
        file_job=self.jobs.claim_due('TEST_FACTORY_FILE',kinds=(file_job_kind(self.account),),now=self.now)[0]
        self.assertEqual((await self.files.dispatcher(file_job))['status'],'handed_off')
        delivery=self.query('SELECT delivery_id FROM qq_official_file_message_plans WHERE artifact_id=?',(artifact.artifact_id,))[0][0]
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
        self.now+=5;await self.session.maintain();await self.action('complete')
        bound=self.binding()
        task_job=self.jobs.claim_due('TEST_FACTORY_PROJECT',kinds=(account_job_kind('subagent',self.account.key),),now=self.now)[0]
        self.assertEqual(task_job.job_id,bound['job_id'])
        router=self.session.task_sources
        with self.tx() as cursor:
            self.assertTrue(router.begin_job(cursor,job=task_job,now=self.now))
            tool_source=router.tool_source(cursor,task_id=bound['target_task_id'],job=task_job,now=self.now)
            task=self.tasks.get(bound['target_task_id'])
            provider,access=self.session.project_sessions._provider(cursor,task_job,task)
            self.assertIs(provider.migration_authority,self.engine)
            self.assertEqual(tool_source.source_kind,'project')
            self.assertEqual(tool_source.source_ref,'task:'+str(task.task_id))
            resource_scope=provider.require(cursor,access.official_proof,project_id=task.project_id,operation=Operation.EXECUTE)
            self.assertEqual(resource_scope.key,self.source_scope.key)
            self.assertEqual(provider.binding['project_id'],self.project['project_id'])
            project=cursor.execute('SELECT bot_native_user_id FROM projects WHERE project_id=?',
                (task.project_id,)).fetchone()
            self.assertEqual(project['bot_native_user_id'],self.source.account.app_id)
        # The same real router and provider must reject the frozen original
        # grant after revocation. Neither profile nor later host config renews it.
        original=provider._pin
        with self.tx() as cursor:ref,_=original(cursor,self.project['project_id'])
        self.query("UPDATE qq_official_project_grants SET state='revoked',revision=revision+1,revoked_at=? WHERE grant_id=?",
            (self.now,ref.grant_id))
        for check in (
            lambda c:router.tool_source(c,task_id=task.task_id,job=task_job,now=self.now),
            lambda c:self.session.project_sessions._provider(c,task_job,task)):
            with self.tx() as cursor,self.assertRaises((Denied,LeaseLost,MigrationDenied,ConversionDenied)):
                check(cursor)
