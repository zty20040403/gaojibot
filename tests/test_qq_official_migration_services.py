"""Real host assembly: dedicated baseline, console grant, TxA and M3 loopback."""
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official.binding import BindingReducer
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.inbox import OfficialInboxStore
from src.plugins.ai_chat.adapters.qq_official.ingest import process_inbox
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.adapters.qq_official.migration_services import MigrationServices,OneBotAccount
from src.plugins.ai_chat.adapters.qq_official.migration_types import MigrationDenied
from src.plugins.ai_chat.adapters.qq_official.outbox import OfficialOutbox
from src.plugins.ai_chat.adapters.qq_official.renderer import render_plan
from src.plugins.ai_chat.adapters.qq_official.sender import OfficialSender
from src.plugins.ai_chat.message_ir import MessageBody,TextNode
from src.qq_official.credentials import Credentials
from src.qq_official.http import RestClient
from src.qq_official.token import TokenManager
from src.qq_official.testing.fake_platform import FakePlatform
from tests.qq_official_migration_journal_fixture import JournalFixture


class MigrationServicesFixture(JournalFixture,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls=DatabaseCalls()
        self.source_connection.close()
        self.service=MigrationServices(database=self.database,transaction=self.deliveries.transaction,
            database_calls=self.calls,security=self.mapping_security,
            onebot_accounts=(OneBotAccount(self.source.account,'f'*64),),
            canonical_scope=lambda event:self.source_scope,clock=lambda:self.now)
        self.store=OfficialInboxStore(self.database,jobs=self.jobs,clock=lambda:self.now,
            binding=BindingReducer(self.pepper,epoch_guard=assert_epoch,clock=lambda:self.now))
        self.addCleanup(self.store.close)
        self.outbox=OfficialOutbox(self.deliveries,admission=self.store.admitted,pepper=self.pepper,clock=lambda:self.now)
        self.runtime=SimpleNamespace(store=self.store,outbox=self.outbox,application=None,
            deliveries=self.deliveries,jobs=self.jobs,database=self.database,_calls=self.calls)
        self.session=self.service.official_factory(self.runtime,self.token)
        await self.calls(self.session.attach)
        self.runtime.migration_service=self.session
        self.addAsyncCleanup(self.service.close)
        await self.service.start()
        self.sequence=0
        self.store.accept({'op':0,'t':'READY','s':0,'d':{'session_id':'TEST_ASSEMBLY'}},token=self.token,session_id='TEST_ASSEMBLY')

    async def asyncTearDown(self):
        await self.service.close()
        await self.calls.close()

    def accept(self):
        self.sequence+=1
        return self.store.accept({'op':0,'t':'GROUP_AT_MESSAGE_CREATE','s':self.sequence,
            'd':{'id':'TEST_ASSEMBLY_'+str(self.sequence),'group_openid':self.endpoint.native_conversation_id,
                'author':{'member_openid':self.actor.native_user_id},'content':'TEST verified source',
                'timestamp':datetime.fromtimestamp(self.now,timezone.utc).isoformat()}},
            token=self.token,session_id='TEST_ASSEMBLY')


class MigrationServicesTests(MigrationServicesFixture):
    async def test_domain_dispatch_accepts_database_rows_but_never_unissued_proofs(self):
        from src.plugins.ai_chat.adapters.qq_official.migration_services import _Domain
        from src.plugins.ai_chat.adapters.qq_official.migration_types import MigrationItemProof
        request=dict(source_endpoint=self.source.key,source_scope=self.source_scope.key,
            target_endpoint=self.endpoint.key,items=[],deadline=self.now+300)
        plan=await self.service.console.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,
            action='plan',request=request)
        # This assertion checks routing only; real domain admission is covered
        # by the journal/activation integration and is never replaced here.
        selected=object();self.session.domains['file']=selected
        with self.tx() as cursor:
            row=cursor.execute("SELECT migration_id,'file' AS kind FROM migration_journals WHERE migration_id=?",
                (plan['migration_id'],)).fetchone()
            dispatch=self.service.ports._domain(row)
            self.assertIs(dispatch._get(cursor,row),selected)
            for forged in (SimpleNamespace(source_kind='file',migration_id=plan['migration_id']),
                           object.__new__(MigrationItemProof)):
                with self.assertRaises(MigrationDenied):self.service.ports._domain(forged)
                with self.assertRaises(MigrationDenied):_Domain(self.service,'file')._get(cursor,forged)

    async def test_actual_baseline_and_cookie_csrf_console_use_same_service(self):
        row=self.query('SELECT * FROM migration_runtime_baselines WHERE account_key=?',(self.source.account.key,))[0]
        holder=self.service.baselines[self.source.account.key]
        self.assertEqual((row['backend_pid'],row['deployment_digest']),(holder.pid,'f'*64))
        self.assertIs(self.service.admin_service(),self.service.console)
        request=dict(source_endpoint=self.source.key,source_scope=self.source_scope.key,
            target_endpoint=self.endpoint.key,items=[],deadline=self.now+300)
        result=await self.service.console.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,
            action='plan',request=request)
        self.assertEqual(self.query('SELECT phase FROM migration_routes WHERE route_id=?',(result['route_id'],))[0]['phase'],'planned')
        with self.assertRaises(MigrationDenied):
            await self.service.console.mutate(cookie=self.mapping_cookie,csrf='TEST_WRONG_CSRF',action='freeze',
                request={'migration_id':result['migration_id']},preview_digest=result['preview_digest'])
        await self.service.console.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,action='freeze',
            request={'migration_id':result['migration_id']},preview_digest=result['preview_digest'])
        self.assertEqual(self.accept().reason,'migration_route_unavailable')

    async def test_actual_ingress_guard_and_m3_loopback_receipt_survive_later_freeze(self):
        accepted=self.accept()
        with self.tx() as c:
            ingested=process_inbox(c,inbox_id=accepted.inbox_id,lease_token=self.token,ledger=self.ledger,
                jobs=self.jobs,epoch_guard=assert_epoch,admission=self.store.admitted,now=self.now)
            observe_ready(c,self.token,{'op':0,'t':'READY','d':{'session_id':'TEST_ASSEMBLY','user':{'id':'TEST_REAL_BOT','bot':True}}},self.now)
        ref=self.outbox.enqueue(self.token,rendered=render_plan(MessageBody((TextNode(0,'TEST reply'),)),self.endpoint),
            requester=self.actor,logical_key='TEST_ASSEMBLY_RESULT',source_scope_key=self.endpoint.scope.key,
            source_native_message_id='TEST_ASSEMBLY_1',source_canonical_message_id=ingested.canonical_message_id,
            expires_at=self.now+200)
        fake=FakePlatform(clock=lambda:self.now);fake.app_id=self.account.app_id
        await fake.start()
        tokens=TokenManager(Credentials(self.account.app_id,fake.app_secret),fake.base_url,allow_test_server=True)
        rest=RestClient(tokens,allow_test_server=True)
        async def fence():
            def current():
                with self.tx() as c:assert_epoch(c,self.token)
            await self.calls(current)
        sender=OfficialSender(self.account,self.token,self.outbox,rest,self.ledger,
            database_calls=self.calls,ensure_current=fence,wall_clock=lambda:self.now)
        fake.add_window('group',self.endpoint.native_conversation_id,'TEST_ASSEMBLY_1')
        try:
            sent=await sender.send_delivery(ref.delivery_ids[0])
            self.assertEqual(sent.status,'committed');self.assertEqual(len(fake.received),1)
            plan=await self.service.console.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,action='plan',
                request=dict(source_endpoint=self.source.key,source_scope=self.source_scope.key,
                    target_endpoint=self.endpoint.key,items=[],deadline=self.now+300))
            await self.service.console.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,action='freeze',
                request={'migration_id':plan['migration_id']},preview_digest=plan['preview_digest'])
            self.assertEqual((await sender.send_delivery(ref.delivery_ids[0])).status,'committed')
            self.assertEqual(len(fake.received),1)
        finally:
            await sender.stop();await rest.aclose();await tokens.aclose();await fake.close()

    async def test_close_removes_only_its_epoch_bindings_and_cannot_guess_account(self):
        self.assertIs(self.outbox.source_guard,self.session.guard)
        await self.session.close()
        self.assertIsNone(self.outbox.source_guard);self.assertIsNone(self.store.source_authority)
        self.assertNotIn(self.account.key,self.service.routes.tokens)
        with self.tx() as c:
            with self.assertRaises(MigrationDenied):self.service.baseline_source(c,self.endpoint,self.now)
        # A native-only host may start without any OneBot account. It must not
        # borrow another live service's baseline, even from the same database.
        native=MigrationServices(database=self.database,transaction=self.tx,database_calls=self.calls,
            security=self.mapping_security,onebot_accounts=(),canonical_scope=lambda event:self.source_scope,
            clock=lambda:self.now)
        try:
            await native.start_native()
            self.assertEqual(native.baselines,{})
            self.assertIsNone(native._renewal)
            session=native.official_factory(self.runtime,self.token)
            await self.calls(session.attach)
            self.assertIs(native.routes.tokens[self.account.key],self.token)
            baseline=dict(self.query('SELECT * FROM migration_runtime_baselines WHERE account_key=?',
                (self.source.account.key,))[0])
            routes=[dict(row) for row in self.query('SELECT * FROM migration_routes ORDER BY route_id')]
            journals=[dict(row) for row in self.query('SELECT * FROM migration_journals ORDER BY migration_id')]
            with self.tx() as cursor:
                for endpoint in (self.source,self.endpoint):
                    with self.assertRaisesRegex(MigrationDenied,'migration_source_account_unconfigured'):
                        native.baseline_source(cursor,endpoint,self.now)
            with self.assertRaisesRegex(MigrationDenied,'migration_source_account_unconfigured'):
                await native.console.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,action='plan',
                    request=dict(source_endpoint=self.source.key,source_scope=self.source_scope.key,
                        target_endpoint=self.endpoint.key,items=[],deadline=self.now+300))
            self.assertEqual([dict(row) for row in self.query('SELECT * FROM migration_routes ORDER BY route_id')],routes)
            self.assertEqual([dict(row) for row in self.query('SELECT * FROM migration_journals ORDER BY migration_id')],journals)
            self.assertEqual(dict(self.query('SELECT * FROM migration_runtime_baselines WHERE account_key=?',
                (self.source.account.key,))[0]),baseline)
        finally:
            await native.close()
        self.assertIsNone(self.outbox.source_guard);self.assertIsNone(self.store.source_authority)
        self.assertNotIn(self.account.key,native.routes.tokens)
        with self.tx() as cursor:
            self.assertIs(self.service.baseline_source(cursor,self.source,self.now),
                self.service.baselines[self.source.account.key])

    async def test_actual_file_factory_uses_current_migration_authority(self):
        from pathlib import Path
        import tempfile
        from unittest.mock import Mock
        from src.plugins.ai_chat.model_catalog import ModelCatalog,ModelProfile
        from src.plugins.ai_chat.subagents import SubAgentStore,SubAgentCoordinator
        from src.plugins.ai_chat.adapters.qq_official.file_services import FileServiceConfig
        from tests.test_owned_sandbox import FakeManager
        tasks=SubAgentStore(self.database);self.addCleanup(tasks.close)
        profile=ModelProfile(name='test',provider='test',protocol='openai-chat',model='test',
            base_url='http://127.0.0.1',api_key_required=False)
        catalog=ModelCatalog({'test':profile},default_profile='test')
        coordinator=SubAgentCoordinator(tasks,catalog,logger=Mock())
        await self.session.close()
        self.runtime.application=SimpleNamespace(subagent_coordinator=coordinator,model_catalog=catalog)
        self.service.subagent_profile='test'
        self.service.file_admission=lambda c,**kwargs:False
        self.session=self.service.official_factory(self.runtime,self.token)
        await self.calls(self.session.attach)
        self.runtime.migration_service=self.session
        with tempfile.TemporaryDirectory(prefix='TEST_assembly_files_') as temp:
            config=FileServiceConfig(Path(temp),FakeManager(),generation_enabled=True,min_free_bytes=0)
            files=await self.service.file_services_factory(config)(self.runtime,self.token,None)
            try:
                self.assertIs(files.config.source_authority.resolve('migration'),self.session.artifact_authority)
                self.assertIs(files.generation.source_factory.__self__,self.session.artifact_authority)
                self.assertIs(self.session.domains['subagent'].file_admission,self.service.file_admission)
                with self.assertRaises(MigrationDenied):
                    self.session.file_configuration(files.config)
            finally:await files.close()

    async def test_project_start_after_official_epoch_binds_existing_consumers(self):
        from pathlib import Path
        from unittest.mock import Mock
        import tempfile
        from tests.test_project_runtime import ProjectRuntimeTests
        from tests.test_owned_sandbox import FakeManager
        from src.plugins.ai_chat.subagents import SubAgentStore
        from src.plugins.ai_chat.adapters.qq_official.file_services import FileServiceConfig
        from src.plugins.ai_chat.adapters.qq_official.project_artifacts import ProjectArtifactAuthority
        # Real ProjectRuntime.start + real local Unix sandbox runner; official
        # epoch was already attached by this fixture before project creation.
        official=self.runtime
        await ProjectRuntimeTests.asyncSetUp(self)
        projects=self.runtime
        self.runtime=official
        self.app.project_runtime=projects
        self.app.subagent_store=SubAgentStore(self.database);self.addCleanup(self.app.subagent_store.close)
        official.application=self.app
        self.assertIsNone(self.session.project_sessions)
        with self.assertRaises(MigrationDenied):await self.service.configure_project_runtime(projects)
        self.assertIsNone(self.session.project_sessions)
        with tempfile.TemporaryDirectory(prefix='TEST_assembly_projects_') as temp:
            authority=ProjectArtifactAuthority(token=self.token,clock=lambda:self.now)
            files=await self.service.file_services_factory(FileServiceConfig(Path(temp),FakeManager(),
                source_authority=authority,min_free_bytes=0))(official,self.token,None)
            official.file_service=files
            official.subagent_service=SimpleNamespace(projects=None)
            try:
                self.assertFalse(projects.ready)
                from unittest.mock import AsyncMock
                from src.plugins.ai_chat.onebot_workers import start_background_tasks
                class StopAfterReady(Exception):pass
                self.app.migration_service=self.service
                self.app.mcp_runtime=SimpleNamespace(start=AsyncMock(side_effect=StopAfterReady))
                with self.assertRaises(StopAfterReady):
                    await start_background_tasks(self.app,self.handlers,Mock())
                self.assertIs(projects.lifecycle.official_authority.__self__,self.service.project_router)
                self.assertIs(projects.task_admission.bindings.official_dispatch.__self__,self.service.project_router)
                self.assertIs(official.project_service,self.session.project_sessions)
                self.assertIs(official.subagent_service.projects,self.session.project_sessions)
                self.assertIs(projects.official_file_delivery.__self__,self.service.project_router)
                with self.tx() as cursor:
                    with self.assertRaises(MigrationDenied):
                        projects.official_provider_factory.authority(cursor,Mock(job_id=987654321))
                await self.session.close()
                self.assertIsNone(official.subagent_service.projects)
            finally:await files.close()


class PureImportTests(unittest.TestCase):
    def test_official_assembly_and_project_registry_do_not_import_platform_sdk(self):
        import subprocess
        import sys
        source="""
import sys
class Block:
    def find_spec(self,name,path=None,target=None):
        if name=='nonebot' or name.startswith('nonebot.'):
            raise AssertionError('SDK import attempted')
sys.meta_path.insert(0,Block())
import src.plugins.ai_chat.adapters.qq_official.migration_services
import src.plugins.ai_chat.adapters.qq_official.approval_services
from src.plugins.ai_chat.tools import _neutral_registry
_neutral_registry()
assert not any(name=='nonebot' or name.startswith('nonebot.') for name in sys.modules)
"""
        subprocess.run([sys.executable,'-c',source],check=True,capture_output=True,text=True,timeout=15)
