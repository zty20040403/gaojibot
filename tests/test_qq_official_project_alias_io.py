"""Real migration grants, native ingress, project IO and snapshot worker.

Only Docker/kernel and QQ transport are fixtures. Alias permissions come from
the actual console grant and completed Journal, never a Boolean source guard.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
import time
import json
import unittest
from unittest.mock import patch

from tests.qq_official_outbox_fixture import OutboxFixture
from tests import test_qq_official_migration_files as files_fixture
from tests import test_project_runtime as runtime_fixture
from src.plugins.ai_chat.adapters.qq_official import turn_source as execution
from src.plugins.ai_chat.adapters.qq_official.binding import BindingReducer
from src.plugins.ai_chat.adapters.qq_official.inbox import OfficialInboxStore
from src.plugins.ai_chat.adapters.qq_official.ingest import process_inbox
from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.adapters.qq_official.migration_native_sources import NativeSources,SourceAdmission
from src.plugins.ai_chat.adapters.qq_official.migration_services import _Projects
from src.plugins.ai_chat.project_authorization import ForegroundProjectProvider
from src.plugins.ai_chat.project_background import LifecycleAccessResolver
from src.plugins.ai_chat.message_ir import MessageBody,TextNode
from src.plugins.ai_chat.projects import ProjectAccess
from src.plugins.ai_chat.turn_journal import TurnJournal


class ProjectAliasIOTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    owned=files_fixture.MigrationFileTests.owned
    new_engine=files_fixture.MigrationFileTests.new_engine
    ensure_current=files_fixture.MigrationFileTests.ensure_current
    rotate_mapping=files_fixture.MigrationFileTests.rotate_mapping
    start=files_fixture.MigrationFileTests.start
    freeze=files_fixture.MigrationFileTests.freeze
    convert=files_fixture.MigrationFileTests.convert
    complete=files_fixture.MigrationFileTests.complete
    tick=files_fixture.MigrationFileTests.tick
    start_project=files_fixture.MigrationFileTests.start_project
    complete_project=files_fixture.MigrationFileTests.complete_project
    asyncTearDown=files_fixture.MigrationFileTests.asyncTearDown

    def setUp(self):
        super().setUp();self.now=int(time.time())

    async def asyncSetUp(self):
        await runtime_fixture.ProjectRuntimeTests.asyncSetUp(self)
        self.project_runtime,self.project_worker,self.project_app=self.runtime,self.worker,self.app
        await files_fixture.MigrationFileTests.asyncSetUp(self)
        source=await files_fixture.MigrationFileTests.project_file(self)
        project=self.query('SELECT p.* FROM projects p JOIN project_file_deliveries f USING(project_id) WHERE f.delivery_id=?',
            (self.old_delivery.delivery_id,))[0]
        self.project_id=project['project_id']
        self.original_bot=project['bot_native_user_id']
        self.legacy_access=ProjectAccess(self.source_scope,project['creator_principal_id'],member=True)
        self.assertTrue(self.deliveries.cancel(self.old_delivery.delivery_id,now=self.now))
        # Actual project startup/wake happens while the source route is active.
        self.project_app.delivery_store=self.deliveries
        await self.project_runtime.start()
        # The runner fixture also owns an unused, generation-zero project in
        # this scope. Record its actual empty measurement before cutover.
        for peer in self.query('SELECT * FROM projects WHERE scope_key=?',(self.source_scope.key,)):
            if peer['project_id']!=self.project_id:
                await self.project_runtime.sampler.sample_one(dict(peer))
        self.project_runtime.lifecycle.request(self.legacy_access,self.project_id,'wake',
            expected_version=project['version'],request_key='TEST_ALIAS_WAKE',now=self.now)
        await self.project_worker.run_once()
        self.assertEqual(self.query('SELECT status FROM projects WHERE project_id=?',(self.project_id,))[0][0],'active')
        # A new captured legacy delivery provides the actual reviewed project
        # migration item; the cancelled pre-wake fixture is never resurrected.
        from src.plugins.ai_chat.adapters.qq_official.migration_preview import SourceRef
        from src.plugins.ai_chat.message_ir import MediaNode
        with self.tx() as c:
            original=c.execute('SELECT * FROM project_file_deliveries WHERE delivery_id=?',
                (self.old_delivery.delivery_id,)).fetchone()
            operation=c.execute('''INSERT INTO project_io
                (project_id,actor_principal_id,request_key,token,tool_name,arguments_sha256,sandbox_id,generation,fence,
                 mutating,reserved_bytes,state,result,evidence,created_at,updated_at)
                VALUES (?,?,?,?,'send_file_from_sandbox',?,'TEST_CAPTURED_GUEST',1,0,FALSE,0,'completed','{}','{}',?,?)
                RETURNING operation_id''',(self.project_id,project['creator_principal_id'],'1'*64,'2'*32,'3'*64,self.now,self.now)).fetchone()[0]
            delivery,_=self.deliveries.enqueue(idempotency_key='project-file:'+str(operation),
                source_scope_key=self.source_scope.key,target_scope=self.source_scope,
                body=MessageBody((MediaNode(0,'file',name='original.pdf',source_type='project_attachment'),)),
                transaction_cursor=c,now=self.now)
            c.execute('''INSERT INTO project_file_deliveries
                (delivery_id,operation_id,project_id,sha256,blob_sha256,byte_size,filename,path)
                VALUES (?,?,?,?,?,?,'original.pdf','TEST_PATH_NOT_EXPORTED')''',
                (delivery.delivery_id,operation,self.project_id,original['sha256'],original['blob_sha256'],original['byte_size']))
        source=SourceRef('file','project:'+str(delivery.delivery_id))
        # Restaging is an explicit console CAS, not mutation of a grant fixture.
        revoked=await self.project_grants.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,
            action='project_grant_revoke',request={'grant_id':self.grant['grant_id'],'expected_revision':1})
        self.grant=await self.project_grants.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,
            action='project_grant_stage',request=dict(source_endpoint=self.source.key,source_scope=self.source_scope.key,
                source_actor=self.source_actor.key,target_endpoint=self.endpoint.key,target_actor=self.actor.key,
                issuer_actor=self.actor.key,project_id=self.project_id,mapping_id=self.mapping_evidence.mapping_id,
                mapping_revision=self.mapping_evidence.revision,capabilities=['project.read','project.write','project.execute','project.deliver'],
                expires_at=self.now+600,expected_revision=revoked['revision']))
        plan=self.start_project(source);item,binding=self.convert(plan);self.complete_project(plan,item)
        from src.plugins.ai_chat.adapters.qq_official.migration_files import job_kind
        imported=self.jobs.claim_due('TEST_ALIAS_FILE_IMPORT',kinds=(job_kind(self.account),),now=self.now)[0]
        self.assertEqual(imported.job_id,binding['job_id'])
        await self.engine.execute(imported)
        self.inbox_store=OfficialInboxStore(self.database,jobs=self.jobs,clock=lambda:self.now,
            binding=BindingReducer(self.pepper,epoch_guard=assert_epoch,clock=lambda:self.now))
        self.addCleanup(self.inbox_store.close)
        native=NativeSources(self.routes);self.inbox_store.source_authority=native
        self.admission=SourceAdmission(self.inbox_store.admitted,native)
        self.inbox_store.admitted=self.admission
        self.inbox_store.accept({'op':0,'t':'READY','s':0,'d':{'session_id':'TEST_ALIAS'}},token=self.token,session_id='TEST_ALIAS')
        accepted=self.inbox_store.accept({'op':0,'t':'GROUP_AT_MESSAGE_CREATE','s':1,
            'd':{'id':'TEST_ALIAS_WRITE','group_openid':self.endpoint.native_conversation_id,
                'author':{'member_openid':self.actor.native_user_id},'content':'TEST edit retained project',
                'timestamp':datetime.fromtimestamp(self.now,timezone.utc).isoformat()}},token=self.token,session_id='TEST_ALIAS')
        self.assertEqual(accepted.disposition,'inbox',accepted)
        with self.tx() as c:
            result=process_inbox(c,inbox_id=accepted.inbox_id,lease_token=self.token,ledger=self.ledger,jobs=self.jobs,
                epoch_guard=assert_epoch,admission=self.admission,now=self.now)
        self.job=self.jobs.claim_due('TEST_ALIAS_NATIVE',kinds=(account_job_kind('turn',self.account.key),),now=self.now)[0]
        self.journal=TurnJournal(self.database);self.addCleanup(self.journal.close)
        with self.tx() as c:
            self.handle=execution.claim_execution(c,token=self.token,job=self.job,now=self.now,
                deadline_at=self.now+300,admission=self.admission).handle
        self.turn=self.journal.start_turn(self.endpoint.scope,trigger_canonical_message_id=result.canonical_message_id,
            objective='TEST retain edits',provider='test',model='test',started_at=self.now)
        with self.tx() as c:
            execution.begin_execution(c,token=self.token,handle=self.handle,job=self.job,now=self.now,
                journal_turn_id=self.turn.turn_id,admission=self.admission)
            epoch=c.execute('SELECT logical_route_epoch FROM qq_migration_native_sources WHERE inbox_id=?',
                (accepted.inbox_id,)).fetchone()[0]
        self.provider=ForegroundProjectProvider(token=self.token,handle=self.handle,job=self.job,admission=self.admission,
            route_epoch=epoch,route_guard=self.project_grants._route,
            project_scope_guard=self.project_grants.project_scope_guard,clock=lambda:self.now)
        with self.tx() as c:self.access=self.provider.issue(c)
        self.project_runtime.store.open(self.access,self.project_id,now=self.now)
        resolver=LifecycleAccessResolver(transaction=self.tx,token_for_account=lambda k:self.token if k==self.account.key else None,
            admission=self.admission,route_guard=self.project_grants._route,
            project_scope_guard=self.project_grants.project_scope_guard,clock=lambda:self.now)
        session=SimpleNamespace(project_sessions=SimpleNamespace(projects=self.project_runtime),token=self.token,project_resolver=resolver)
        host=SimpleNamespace(official={self.account.key:session},transaction=self.tx,database_calls=self.calls)
        router=_Projects(host,self.project_runtime)
        self.project_runtime.official_provider_factory=router
        self.project_runtime.lifecycle.official_authority=router.authority
        self.assertNotEqual(self.access.scope.bot_native_user_id,self.original_bot)

    async def admit(self,*,key='e'*64):
        project=self.project_runtime.store.get(self.access,self.project_id)
        await self.project_runtime.sampler.sample_one(project,source=self.access)
        self.now=int(time.time())
        project=self.project_runtime.store.get(self.access,self.project_id)
        return self.project_runtime.io.admit(self.access,self.project_id,'sandbox_write_file',
            request_key=key,arguments_sha256='f'*64,expected_version=project['version'],expected_bytes=14,
            journal_turn_id=self.turn.turn_id,native_message_id='TEST_ALIAS_WRITE',now=self.now)

    def finish(self):
        self.journal.finish_turn(self.turn.turn_id,status='succeeded',finished_at=self.now)
        with self.tx() as c:
            execution.capture_result(c,token=self.token,handle=self.handle,job=self.job,now=self.now,
                admission=self.admission,body=MessageBody((TextNode(0,'TEST saved'),)),turn_id=self.turn.turn_id,status='succeeded')
        self.jobs.mark_succeeded(self.job.job_id,self.job.lease_owner,lease_generation=self.job.lease_generation,now=self.now)

    async def write(self,*,key='e'*64):
        grant=await self.admit(key=key)
        target=self.project_runtime.io.dispatch(self.access,grant,now=self.now)
        self.assertEqual(target['owner'],self.source_scope.key)
        self.assertEqual(self.project_runtime.io.find(self.access,key,'sandbox_write_file','f'*64),grant)
        # The existing private runner validates owner/generation; only its
        # terminal kernel tee implementation is a local fixture.
        async def write(owner,sandbox_id,path,content,**kwargs):
            self.assertEqual(owner,self.source_scope.key)
            actual=self.volumes.binding(owner,self.project_id)/path
            actual.parent.mkdir(parents=True,exist_ok=True);actual.write_bytes(content)
            return len(content)
        with patch.object(self.driver,'write_file',side_effect=write):
            count=await self.client.write_file(target['owner'],target['sandbox_id'],'edit.txt',b'TEST saved IO\n')
        result={'ok':True,'written':count}
        self.project_runtime.io.observe_result(grant,result,now=self.now)
        project=self.project_runtime.store.get(self.access,self.project_id)
        evidence=await self.project_runtime.backends['oci'].io_evidence(project)
        self.project_runtime.io.complete(grant,result,evidence,now=self.now)
        self.assertEqual(self.project_runtime.io.receipt(self.access,grant)['result'],result)
        return grant

    async def test_actual_alias_io_terminal_turn_autosave_publishes_original_workspace_bytes(self):
        grant=await self.write()
        import psycopg
        from src.bot_storage.database import DatabaseError
        original=self.query('SELECT * FROM qq_official_project_io_authority WHERE operation_id=?',
            (grant.operation_id,))[0]
        for sql in (
            'UPDATE qq_official_project_io_authority SET grant_revision=grant_revision+1 WHERE operation_id=?',
            'DELETE FROM qq_official_project_io_authority WHERE operation_id=?',
        ):
            with self.subTest(statement=sql.split()[0]), self.assertRaises(DatabaseError) as error:
                with self.tx() as cursor:
                    cursor.execute(sql,(grant.operation_id,))
            self.assertIsInstance(error.exception.__cause__,psycopg.errors.CheckViolation)
            self.assertIn('qq_official_project_io_authority_immutable',str(error.exception.__cause__))
            self.assertEqual(self.query('SELECT * FROM qq_official_project_io_authority WHERE operation_id=?',
                (grant.operation_id,))[0],original)
        self.finish()
        await self.assert_snapshot(grant)

    async def test_aborted_first_reservation_never_hides_later_confirmed_mutation(self):
        first=await self.admit()
        self.project_runtime.io.abort(first,now=self.now)
        written=await self.write(key='9'*64);self.finish()
        await self.assert_snapshot(written)
        self.assertEqual(self.query('SELECT state FROM project_io WHERE operation_id=?',(first.operation_id,))[0][0],'aborted')

    async def assert_snapshot(self,grant):
        self.assertEqual(await self.project_runtime.autosave.run_once(now=self.now),1)
        tracker=self.query('SELECT * FROM project_turn_saves WHERE project_id=?',(self.project_id,))[0]
        self.assertEqual(tracker['state'],'queued',dict(tracker))
        self.assertEqual(self.query('SELECT source_io_operation_id FROM qq_official_project_lifecycle_authority WHERE job_id=?',
            (tracker['job_id'],))[0][0],grant.operation_id)
        await self.project_worker.run_once()
        saved=self.jobs.get(tracker['job_id'])
        self.assertEqual(saved.status,'succeeded',saved.last_error)
        version=self.query('SELECT * FROM project_versions WHERE project_id=? AND source_turn_id=?',
            (self.project_id,self.turn.turn_id))
        self.assertEqual(len(version),1)
        manifest=json.loads(self.project_runtime.store.blobs.read(version[0]['manifest_sha256']))
        blob=next(item['sha256'] for item in manifest['files'] if item['path']=='edit.txt')
        self.assertEqual(self.project_runtime.store.blobs.read(blob),b'TEST saved IO\n')
        self.assertEqual(self.query('SELECT bot_native_user_id,scope_key FROM projects WHERE project_id=?',
            (self.project_id,))[0],{'bot_native_user_id':self.original_bot,'scope_key':self.source_scope.key})

    async def test_revoked_original_grant_blocks_dispatch_and_cannot_replay_into_new_focus(self):
        grant=await self.admit()
        await self.project_grants.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,action='project_grant_revoke',
            request={'grant_id':self.grant['grant_id'],'expected_revision':self.grant['revision']})
        with self.assertRaises(PermissionError):self.project_runtime.io.dispatch(self.access,grant,now=self.now)
        with self.assertRaises(PermissionError):self.project_runtime.io.find(self.access,'e'*64,'sandbox_write_file','f'*64)
        self.assertEqual(self.query('SELECT state FROM project_io WHERE operation_id=?',(grant.operation_id,))[0][0],'reserved')

    async def test_changed_mapping_blocks_finished_turn_autosave_without_new_job(self):
        await self.write();self.finish();self.rotate_mapping()
        await self.project_runtime.autosave.run_once(now=self.now)
        tracker=self.query('SELECT * FROM project_turn_saves WHERE project_id=?',(self.project_id,))[0]
        self.assertEqual(tracker['state'],'pending');self.assertIsNone(tracker['job_id'])
        self.assertEqual(tracker['error_code'],'permission_unavailable')
        self.assertEqual(self.query('SELECT * FROM project_versions WHERE project_id=?',(self.project_id,)),[])
