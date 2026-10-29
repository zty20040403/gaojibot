"""Real database project background source/lease and physical RPC boundaries."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests import qq_official_project_fixture as fixture
from src.plugins.ai_chat import project_background as background
from src.plugins.ai_chat import project_lifecycle as lifecycle
from src.plugins.ai_chat import project_executor as executor
from src.plugins.ai_chat import project_io as io
from src.plugins.ai_chat import project_tasks as task_module
from src.plugins.ai_chat import project_runtime as runtime
from src.plugins.ai_chat import project_file_executor as files
from src.plugins.ai_chat.project_usage import ProjectUsageStore
from src.plugins.ai_chat.adapters.qq_official import execution
from src.plugins.ai_chat.adapters.qq_official.project_authority import Operation
from src.plugins.ai_chat.message_ir import MessageBody, TextNode


class BackgroundTests(fixture.ProjectFixture):
    def setUp(self):
        super().setUp()
        self.project=self.create()
        self.lifecycle=lifecycle.ProjectLifecycle(self.store,self.jobs,official_authority=self.authority)
        self.run=self.lifecycle.request(self.access,self.project['project_id'],'wake',
            expected_version=self.project['version'],request_key='TEST_WAKE',now=self.now)
        self.lifecycle_job=self.jobs.claim_due('TEST_LIFECYCLE',kinds=('project.lifecycle',),now=self.now)[0]

    def background_provider(self,job=None):
        with self.tx() as cursor:
            return background.BackgroundProjectProvider.load(cursor,token=self.token,
                job=job or self.lifecycle_job,kind='lifecycle',binding_id=self.run['job_id'],
                admission=self.admission,route_guard=self.route_guard,
                project_scope_guard=lambda *a,**k:False,clock=lambda:self.now)

    def authority(self,cursor,job):
        provider=background.BackgroundProjectProvider.load(cursor,token=self.token,job=job,
            kind='lifecycle',binding_id=job.job_id,admission=self.admission,
            route_guard=self.route_guard,project_scope_guard=lambda *a,**k:False,clock=lambda:self.now)
        access=provider.issue(cursor)
        provider.require(cursor,access.official_proof,project_id=self.project['project_id'],
                         operation=Operation.EXECUTE)
        return access

    def access_for(self,provider=None):
        with self.tx() as cursor:return (provider or self.background_provider()).issue(cursor)

    def test_actual_lifecycle_side_binding_and_recovery_after_foreground_finished(self):
        bound=self.query('SELECT * FROM qq_official_project_lifecycle_authority')[0]
        self.assertEqual(bound['source_execution_id'],self.handle.execution_id)
        self.assertEqual(bound['job_id'],self.lifecycle_job.job_id)
        self.assertEqual(bound['deadline_at'],self.now+3600)
        with self.tx() as cursor:
            execution.capture_result(cursor,**self.args(self.handle),body=MessageBody((TextNode(0,'done'),)),
                turn_id=self.query('SELECT journal_turn_id FROM qq_official_turn_executions')[0]['journal_turn_id'],status='succeeded')
        self.query("UPDATE durable_jobs SET status='succeeded',lease_owner='',lease_until=0 WHERE job_id=?",(self.job.job_id,))
        access=self.access_for()
        got,run=self.lifecycle.context(self.lifecycle_job,access=access,now=self.now)
        self.assertEqual(got['project_id'],self.project['project_id'])
        self.assertTrue(run['official_authority'])
        self.assertEqual(self.lifecycle.next_step(self.lifecycle_job,now=self.now)['operation'],'create_volume')

    def test_restart_cannot_adopt_new_grant_revision(self):
        old=self.background_provider();self.access_for(old)
        self.query('UPDATE qq_official_project_grants SET revision=revision+1')
        for provider in (old,self.background_provider()):
            access=self.access_for(provider)
            with self.assertRaises(PermissionError):self.store.get(access,self.project['project_id'])
        with self.assertRaises(PermissionError):self.lifecycle.next_step(self.lifecycle_job,now=self.now)

    def test_source_cancel_and_account_loss_and_exact_job_generation_deny(self):
        self.query("UPDATE qq_official_turn_executions SET state='needs_attention' WHERE execution_id=?",(self.handle.execution_id,))
        with self.assertRaises(PermissionError):self.background_provider()
        self.query("UPDATE qq_official_turn_executions SET state='executing' WHERE execution_id=?",(self.handle.execution_id,))
        self.query('UPDATE durable_jobs SET lease_generation=lease_generation+1 WHERE job_id=?',(self.lifecycle_job.job_id,))
        with self.assertRaises(PermissionError):self.background_provider()
        self.query('UPDATE durable_jobs SET lease_generation=lease_generation-1 WHERE job_id=?',(self.lifecycle_job.job_id,))
        self.account_lease.close()
        with self.assertRaises(PermissionError):self.background_provider()

    def test_missing_provider_and_route_revoke_precede_physical_intent(self):
        self.lifecycle.official_authority=None
        with self.assertRaises(PermissionError):self.lifecycle.next_step(self.lifecycle_job,now=self.now)
        self.lifecycle.official_authority=self.authority
        self.routes_live=False
        with self.assertRaises(PermissionError):self.lifecycle.next_step(self.lifecycle_job,now=self.now)
        self.assertEqual(self.query('SELECT * FROM project_lifecycle_steps'),[])

    def test_historical_observation_remains_recordable_after_grant_revocation(self):
        step=self.lifecycle.next_step(self.lifecycle_job,now=self.now)
        self.query("UPDATE qq_official_project_grants SET state='revoked',revision=revision+1,revoked_at=issued_at")
        with self.assertRaises(PermissionError):self.lifecycle.next_step(self.lifecycle_job,now=self.now)
        evidence={**{k:step['arguments'][k] for k in ('project_id','generation','fence')},
                  'verified':True,'observation':'TEST observed physical create','volume_id':'TEST_VOLUME',
                  'quota_bytes':step['arguments']['quota_bytes'],'quota_mode':'hard'}
        # Existing receipt validator remains authoritative; source revocation
        # does not prevent the exact observed receipt from being acknowledged.
        self.lifecycle.acknowledge(self.lifecycle_job,step,evidence,now=self.now)
        self.assertEqual(self.query('SELECT status FROM project_lifecycle_steps')[0]['status'],'succeeded')
        with self.assertRaises(PermissionError):self.lifecycle.next_step(self.lifecycle_job,now=self.now)

    def test_runtime_official_resolution_never_calls_onebot_or_numeric_policy(self):
        access=self.access_for()
        app=SimpleNamespace(settings=SimpleNamespace(projects_enabled=True,
            is_sandbox_user_allowed=lambda *a: self.fail('numeric legacy policy')))
        host=object.__new__(runtime.ProjectRuntime)
        host.closed=False;host.app=app;host.store=self.store;host.official_provider_factory=AsyncMock(return_value=access)
        import asyncio
        with patch.object(runtime,'get_bots',side_effect=AssertionError('OneBot must not load')):
            result=asyncio.run(host._execution_access(self.project,access.principal_id,job=self.lifecycle_job))
        self.assertIs(result,access)
        host.official_provider_factory=None
        with self.assertRaises(PermissionError):
            asyncio.run(host._execution_access(self.project,access.principal_id,job=self.lifecycle_job))

    def test_background_delivery_requires_real_recent_actor_receipt(self):
        provider=self.background_provider()
        request=SimpleNamespace(endpoint=self.endpoint,actor=self.actor,
            route_epoch=provider.route_epoch,endpoint_route_epoch=provider.handle.route_epoch)
        with self.tx() as cursor:
            cursor.execute('DELETE FROM qq_official_gateway_receipts WHERE inbox_id=?',(self.handle.inbox_id,))
            self.assertFalse(provider._fresh(cursor,request=request,source=provider,now=self.now,max_age_seconds=300))
            cursor.execute('''INSERT INTO qq_official_gateway_receipts
                (account_key,session_id,seq,event_type,disposition,inbox_id,received_at)
                VALUES (?,'TEST_PROJECT_FRESH',0,'GROUP_AT_MESSAGE_CREATE','inbox',?,?)''',
                (self.account.key,self.handle.inbox_id,self.now))
            self.assertTrue(provider._fresh(cursor,request=request,source=provider,now=self.now,max_age_seconds=300))
            self.assertFalse(provider._fresh(cursor,request=request,source=object(),now=self.now,max_age_seconds=300))
            self.assertFalse(provider._fresh(cursor,request=request,source=provider,now=self.now+301,max_age_seconds=300))
        self.routes_live=False
        with self.assertRaises(PermissionError):
            with self.tx() as cursor:
                provider._fresh(cursor,request=request,source=provider,now=self.now,max_age_seconds=300)

    def test_lifecycle_deadline_not_extended_by_recovery(self):
        deadline=self.query('SELECT deadline_at FROM qq_official_project_lifecycle_authority')[0]['deadline_at']
        self.now=deadline
        self.query('UPDATE durable_jobs SET lease_until=? WHERE job_id=?',(self.now+60,self.lifecycle_job.job_id))
        with self.assertRaises(PermissionError):self.background_provider()
        self.assertEqual(self.query('SELECT deadline_at FROM qq_official_project_lifecycle_authority')[0]['deadline_at'],deadline)

    def test_read_write_grants_do_not_admit_io_execution(self):
        # Refusal occurs before project active/focus validation: even a valid
        # source plus legacy creator does not manufacture EXECUTE capability.
        self.query("UPDATE qq_official_project_grants SET capabilities=ARRAY['project.read','project.write']")
        store=io.ProjectIOStore(self.store,ProjectUsageStore(self.store))
        with self.assertRaises(PermissionError):
            store.admit(self.access,self.project['project_id'],'sandbox_exec',request_key='a'*64,
                        arguments_sha256='b'*64,expected_version=1,now=self.now)
        self.assertEqual(self.query('SELECT * FROM project_io'),[])

    def test_actual_executor_records_receipt_then_stops_after_mid_rpc_revoke(self):
        test=self
        class Backend:
            calls=0
            def preflight(self, plan):
                pass
            async def execute(self, job, step, access):
                self.calls+=1
                test.query("UPDATE qq_official_project_grants SET state='revoked',revision=revision+1,revoked_at=issued_at")
                return {**{k:step['arguments'][k] for k in ('project_id','generation','fence')},
                    'verified':True,'observation':'TEST actual backend response',
                    'volume_id':'TEST_VOLUME','quota_bytes':step['arguments']['quota_bytes'],'quota_mode':'hard'}
        backend=Backend()
        async def resolve(project,principal_id,*,job):
            with self.tx() as cursor:return self.authority(cursor,job)
        actual=executor.ProjectLifecycleExecutor(self.lifecycle,{'oci':backend},resolve_access=resolve)
        import asyncio
        with self.assertRaises(PermissionError):asyncio.run(actual(self.lifecycle_job))
        self.assertEqual(backend.calls,1)

        receipts=self.query('SELECT * FROM project_lifecycle_steps')
        self.assertEqual(len(receipts),1)
        self.assertEqual(receipts[0]['status'],'succeeded')
        # A recovered executor cannot replay the confirmed effect or run the
        # next step by selecting a newly granted revision.
        self.query("UPDATE qq_official_project_grants SET state='active',revision=revision+1,revoked_at=NULL")
        with self.assertRaises(PermissionError):asyncio.run(actual(self.lifecycle_job))
        self.assertEqual(backend.calls,1)


    def test_official_snapshot_spools_without_database_lock_and_verifies_bytes(self):
        import asyncio
        import hashlib
        content=b'TEST snapshot bytes'
        item={'size':len(content),'sha256':hashlib.sha256(content).hexdigest()}
        handler=files.ProjectFileExecutor(self.lifecycle,SimpleNamespace(max_file_bytes=3),resolve_access=None)
        reads=[]
        async def read(offset,maximum):
            # A separate connection can acquire the project lock during RPC:
            # no scope/blob/account transaction spans network reading.
            with self.tx() as cursor:
                cursor.execute('SELECT * FROM projects WHERE project_id=? FOR UPDATE NOWAIT',
                               (self.project['project_id'],)).fetchone()
            reads.append(offset)
            return content[offset:offset+maximum]
        stream=asyncio.run(handler._spool_snapshot(self.lifecycle_job,item,read,quota_bytes=100))
        try:
            self.assertEqual(stream.read(),content)
            import os
            self.assertEqual(os.fstat(stream.fileno()).st_mode & 0o777,0o600)
            self.assertGreater(len(reads),1)
        finally:stream.close()

    def test_snapshot_spool_mid_read_revoke_and_cancel_cleanup_without_staging(self):
        import asyncio
        import hashlib
        handler=files.ProjectFileExecutor(self.lifecycle,SimpleNamespace(max_file_bytes=3),resolve_access=None)
        item={'size':3,'sha256':hashlib.sha256(b'ABC').hexdigest()}
        created=[];real=files.tempfile.TemporaryFile
        def track(*a,**k):
            stream=real(*a,**k);created.append(stream);return stream
        async def revoke(offset,maximum):
            self.query("UPDATE qq_official_project_grants SET state='revoked',revision=revision+1,revoked_at=issued_at")
            return b'ABC'
        with patch.object(files.tempfile,'TemporaryFile',side_effect=track):
            with self.assertRaises(PermissionError):
                asyncio.run(handler._spool_snapshot(self.lifecycle_job,item,revoke,quota_bytes=100))
        self.assertTrue(created[0].closed)
        self.assertEqual(self.query('SELECT * FROM project_snapshot_staging'),[])
        # Cancellation is propagated rather than treated as a new source or
        # permission to retry a stream; the local private spool is still closed.
        self.query("UPDATE qq_official_project_grants SET state='active',revision=revision-1,revoked_at=NULL")
        async def cancel(*a):raise asyncio.CancelledError
        with patch.object(files.tempfile,'TemporaryFile',side_effect=track):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(handler._spool_snapshot(self.lifecycle_job,item,cancel,quota_bytes=100))
        self.assertTrue(created[-1].closed)

    def test_snapshot_spool_wrong_digest_and_size_limit_deny(self):
        import asyncio
        handler=files.ProjectFileExecutor(self.lifecycle,SimpleNamespace(max_file_bytes=3),resolve_access=None)
        read=AsyncMock(return_value=b'ABC')
        with self.assertRaises(files.ProjectConflict):
            asyncio.run(handler._spool_snapshot(self.lifecycle_job,{'size':3,'sha256':'0'*64},read,quota_bytes=100))
        read.reset_mock()
        with self.assertRaises(files.ProjectConflict):
            asyncio.run(handler._spool_snapshot(self.lifecycle_job,{'size':101,'sha256':'0'*64},read,quota_bytes=100))
        read.assert_not_awaited()


class TaskTests(fixture.ProjectFixture):
    def setUp(self):
        super().setUp()
        from src.plugins.ai_chat.subagents import SubAgentStore
        from src.plugins.ai_chat.project_blobs import ProjectFile
        from src.plugins.ai_chat.agent.contracts import ContextPacket
        self.tasks=SubAgentStore(self.database);self.addCleanup(self.tasks.close)
        self.project=self.create()
        self.store.snapshot(self.access,self.project['project_id'],[ProjectFile('note.txt',b'TEST content')],
            expected_version=self.project['version'],now=self.now)
        self.store.open(self.access,self.project['project_id'],now=self.now)
        self.packet=ContextPacket(self.endpoint.scope.key,'TEST_PROJECT_TASK_CONVERSATION',None,
            self.message.canonical_message_id,'TEST Update the project',endpoint=self.endpoint,requester_actor=self.actor)
        self.dispatch={'platform':'qq-official','profile':'TEST_PROFILE','tool_profile':'official-projects-v1','deadline':self.now+600}
        self.task_store=task_module.ProjectTaskStore(self.store,self.tasks,official_dispatch=background.ProjectTaskDispatcher(self.jobs))

    def prepare_task(self,**changes):
        options=dict(expected_version=self.store.get(self.access,self.project['project_id'])['version'],
            base_version=1,request_key='f'*64,packet=self.packet,decision=None,dispatch=self.dispatch,
            max_parallelism=2,max_steps=4,now=self.now)
        options.update(changes)
        return self.task_store.prepare(self.access,self.project['project_id'],**options)

    def test_structured_task_authority_and_existing_job_are_one_transaction(self):
        task=self.prepare_task()
        authority=self.query('SELECT * FROM qq_official_project_task_authority')[0]
        dispatch=self.query('SELECT * FROM qq_official_subagent_dispatches')[0]
        self.assertIsNone(task.requester_user_id)
        self.assertEqual(task.endpoint,self.endpoint)
        self.assertEqual((authority['task_id'],authority['project_id']),(task.task_id,self.project['project_id']))
        self.assertEqual(dispatch['source_execution_id'],self.handle.execution_id)
        job=self.jobs.get(dispatch['job_id'])
        self.assertEqual(job.payload,{'task_id':task.task_id,'revision':1})
        self.assertEqual(job.scope_key,self.endpoint.scope.key)
        self.assertEqual(self.prepare_task().task_id,task.task_id)
        self.assertEqual(len(self.query('SELECT * FROM qq_official_subagent_dispatches')),1)
        self.assertEqual(self.task_store.baseline(self.access,task.task_id)['version_no'],1)

    def test_task_dispatch_failure_rolls_back_authority_and_task_and_project_lock(self):
        def reject(*a,**k):raise PermissionError('TEST unavailable')
        self.task_store.official_dispatch=reject
        with self.assertRaises(PermissionError):self.prepare_task()
        self.assertEqual(self.query('SELECT * FROM subagent_tasks'),[])
        self.assertEqual(self.query('SELECT * FROM qq_official_project_task_authority'),[])
        self.assertIsNone(self.store.get(self.access,self.project['project_id'])['locked_task_id'])

    def test_task_recovery_uses_fixed_grant_and_exact_worker_without_foreground_job(self):
        task=self.prepare_task();self.tasks.set_task_state(task.task_id,'running')
        dispatch=self.query('SELECT * FROM qq_official_subagent_dispatches')[0]
        from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
        job=self.jobs.claim_due('TEST_PROJECT_WORKER',kinds=(account_job_kind('subagent',self.account.key),),now=self.now)[0]
        self.query("UPDATE durable_jobs SET status='succeeded',lease_owner='',lease_until=0 WHERE job_id=?",(self.job.job_id,))
        def current():
            with self.tx() as cursor:
                provider=background.BackgroundProjectProvider.load(cursor,token=self.token,job=job,kind='task',
                    binding_id=task.task_id,admission=self.admission,route_guard=self.route_guard,
                    project_scope_guard=lambda *a,**k:False,clock=lambda:self.now)
                access=provider.issue(cursor)
                provider.require(cursor,access.official_proof,project_id=task.project_id,operation=Operation.EXECUTE)
                return access
        access=current()
        self.assertEqual(access.actor,self.actor)
        self.assertEqual(job.job_id,dispatch['job_id'])
        self.query('UPDATE qq_official_project_grants SET revision=revision+1')
        with self.assertRaises(PermissionError):current()

    def test_task_actual_actor_and_non_numeric_identity_do_not_accept_copied_packet(self):
        from dataclasses import replace
        actor=fixture.ActorKey(self.account,'group_member',self.endpoint.native_conversation_id,'TEST_OTHER_ACTOR')
        with self.assertRaises(PermissionError):self.prepare_task(packet=replace(self.packet,requester_actor=actor))
        self.assertEqual(self.query('SELECT * FROM subagent_tasks'),[])

    def test_project_background_metadata_selects_exact_proof_and_terminal_host_has_no_tools(self):
        from src.plugins.ai_chat.platform_types import BackgroundToolSource
        from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
        task=self.prepare_task();self.tasks.set_task_state(task.task_id,'running')
        job=self.jobs.claim_due('TEST_PROJECT_WORKER',kinds=(account_job_kind('subagent',self.account.key),),now=self.now)[0]
        def provider(purpose='execute'):
            with self.tx() as cursor:
                return background.BackgroundProjectProvider.load(cursor,token=self.token,job=job,kind='task',
                    binding_id=task.task_id,admission=self.admission,route_guard=self.route_guard,
                    project_scope_guard=lambda *a,**k:False,clock=lambda:self.now,purpose=purpose)
        source=BackgroundToolSource(self.endpoint,self.actor,'project',f'task:{task.task_id}',1)
        self.assertTrue(provider().matches_tool_source(source))
        self.assertFalse(provider().matches_tool_source(BackgroundToolSource(self.endpoint,self.actor,'subagent',f'task:{task.task_id}',1)))
        self.tasks.set_task_state(task.task_id,'completed')
        with self.assertRaises(PermissionError):provider()
        finalizer=provider('finalize')
        self.assertFalse(finalizer.matches_tool_source(source))
        with self.tx() as cursor:
            access=finalizer.issue(cursor)
            finalizer.require(cursor,access.official_proof,project_id=task.project_id,operation=Operation.EXECUTE)
        self.query('UPDATE subagent_tasks SET cancel_requested=TRUE WHERE task_id=?',(task.task_id,))
        with self.assertRaises(PermissionError):provider('finalize')
