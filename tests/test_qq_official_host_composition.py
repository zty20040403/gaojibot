"""Protected host configuration -> actual account services, PG and loopback.

No factory is replaced by a permissive source callback. The generated/native
routes remain unavailable without explicit evidence; these tests do not grant a
real remote host, download origin, or QQ credential.
"""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from src.plugins.ai_chat.adapters.qq_official.host_config import HostConfiguration,HostConfigurationError
from src.plugins.ai_chat.adapters.qq_official.file_authority import ArtifactSourceAuthorities
from src.plugins.ai_chat.adapters.qq_official.project_artifacts import ProjectArtifactAuthority
from src.plugins.ai_chat.adapters.qq_official.migration_artifacts import MigrationArtifactAuthority
from src.plugins.ai_chat.adapters.qq_official.sender import OfficialSender
from src.plugins.ai_chat.model_catalog import ModelCatalog,ModelProfile
from src.plugins.ai_chat.platform_types import AccountKey
from src.plugins.ai_chat.subagents import SubAgentStore,SubAgentCoordinator
from tests.test_owned_sandbox import FakeManager
from tests.test_qq_official_migration_verification_host import VerificationHostFixture


class HostCompositionFixture(VerificationHostFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.service.close()
        profile=ModelProfile(name='test',provider='test',protocol='openai-chat',model='test',
            base_url='http://127.0.0.1',api_key_required=False)
        catalog=ModelCatalog({'test':profile},default_profile='test')
        tasks=SubAgentStore(self.database);self.addCleanup(tasks.close)
        self.context=SimpleNamespace(database=self.database,delivery_store=self.deliveries,db_call=self.calls,
            state_dir=Path(self.temp.name),
            mobile_authorization=SimpleNamespace(store=self.mapping_security),model_catalog=catalog,
            subagent_coordinator=SubAgentCoordinator(tasks,catalog,logger=Mock()),subagent_store=tasks,
            sandbox_manager=FakeManager(),fleet_client=None,project_runtime=None)
        self.runtime.application=self.context
        self.runtime._token=self.token;self.runtime._stopping=False
        self.files=None;self.approval=None
        self.raw={'version':1,'account':self.account.key,
            'onebot_accounts':[{'account':self.source.account.key,'deployment_digest':'f'*64}],
            'subagent_profile':'test',
            'files':{'private_directory':str(Path(self.temp.name)/'files'),'native_routes':[],
                'generation_enabled':True,'min_free_bytes':0}}
        self.config_path=Path(self.temp.name)/'host.json'
        self.config_path.touch(mode=0o600)
        self.onebot=SimpleNamespace(handlers=SimpleNamespace(chat=SimpleNamespace(_conversation_scope=lambda event:self.source_scope)))

    async def build(self,raw=None):
        self.config_path.write_text(json.dumps(raw or self.raw),encoding='utf-8')
        config=HostConfiguration.from_env({'AI_QQ_MIGRATION_ENABLED':'true',
            'AI_QQ_MIGRATION_CONFIG_FILE':str(self.config_path)})
        self.service=config.build(self.context,self.onebot)
        self.host=self.context.official_host
        await self.service.start()
        self.session=self.host.migration_factory(self.runtime,self.token)
        await self.calls(self.session.attach)
        self.runtime.migration_service=self.session
        self.approval=self.host.approval_factory(self.runtime,self.token)
        await self.calls(self.approval.attach,self.runtime,self.token)
        self.files=await self.host.files_factory(self.runtime,self.token,self.rest)
        self.runtime.file_service=self.files
        return config

    async def asyncTearDown(self):
        if self.files is not None:await self.files.close()
        if self.approval is not None:await self.approval.close()
        await super().asyncTearDown()



class HostCompositionTests(HostCompositionFixture):
    async def test_protected_loader_constructs_same_epoch_native_approval_and_composite_files(self):
        await self.build()
        self.assertIs(self.runtime.store.source_authority,self.session.native)
        self.assertIs(self.runtime.store.approval_store,self.approval.mobile.store)
        self.assertIs(self.approval.token,self.token)
        self.assertIs(self.session.task_sources.token,self.token)
        self.assertIs(self.session.task_sources.domains,self.session.domains)
        self.assertIn(self.session.task_sources,self.session.guard.continuations)
        self.assertNotIn(self.session.domains['subagent'],self.session.guard.continuations)
        self.assertIs(self.files.token,self.token)
        combined=self.files.config.source_authority
        self.assertIsInstance(combined,ArtifactSourceAuthorities)
        self.assertIsInstance(combined.resolve('project'),ProjectArtifactAuthority)
        self.assertIsInstance(combined.resolve('migration'),MigrationArtifactAuthority)
        self.assertIs(combined.resolve('migration'),self.session.artifact_authority)
        self.assertIs(combined.resolve('project').token,self.token)
        self.assertIs(self.files.generation.source_factory.__self__,self.session.artifact_authority)
        imports=self.session.domains['file']
        self.assertIs(imports.token,self.token)
        self.assertIs(imports.artifacts,self.files.artifacts)
        self.assertIs(imports.project_grants,self.service.project_grants)
        self.assertIs(self.session.artifact_authority.imports,imports)
        self.assertEqual(imports.legacy_root,self.context.state_dir/'subagent_artifacts')
        self.assertIsNone(self.approval.host_admission)  # absent explicit allowed service catalog
        with self.tx() as c:
            self.session.admission.require_inbox(c,self.accepted.inbox_id,endpoint=self.endpoint,
                actor=self.actor,operation='execute')
            self.assertTrue(self.host.file_admission(c,item_id='TEST_UNUSED_ROUTING_ONLY',
                endpoint=self.endpoint,actor=self.actor,now=self.now))
        # A configured file profile is not evidence that QQ accepts files.
        self.assertIsNone(self.host.evidence.get(self.endpoint.key))
        self.assertIsNone(self.session.project_sessions)  # ProjectRuntime has not started.
        self.assertFalse(self.fake.received)

    async def test_new_account_epoch_rebuilds_all_source_owners_and_old_files_cannot_write(self):
        await self.build()
        old=self.session;old_authority=self.session.artifact_authority;old_files=self.files
        await self.files.close();self.files=None
        await self.approval.close();self.approval=None
        self.runtime.outbox.approval_authority=None;self.runtime.store.approval_store=None
        await old.close()
        await self.sender.stop()
        self.account_lease.close()
        self.account_lease=self.lease();self.token=self.account_lease.acquire()
        self.runtime._token=self.token
        self.assertGreater(self.token.epoch,old.token.epoch)
        self.sender=OfficialSender(self.account,self.token,self.outbox,self.rest,self.ledger,
            database_calls=self.calls,ensure_current=self.ensure_current,wall_clock=lambda:self.now)
        self.runtime.sender=self.sender
        self.session=self.host.migration_factory(self.runtime,self.token)
        await self.calls(self.session.attach);self.runtime.migration_service=self.session
        self.approval=self.host.approval_factory(self.runtime,self.token)
        await self.calls(self.approval.attach,self.runtime,self.token)
        self.files=await self.host.files_factory(self.runtime,self.token,self.rest)
        self.runtime.file_service=self.files
        self.assertIsNot(self.session.artifact_authority,old_authority)
        self.assertIsNot(self.session.task_sources,old.task_sources)
        self.assertIs(self.session.task_sources.token,self.token)
        self.assertIs(self.files.generation.source_factory.__self__,self.session.artifact_authority)
        self.assertIs(self.approval.transport.token,self.token)
        self.assertFalse(old_files._local())
        with self.assertRaises(HostConfigurationError):self.host._session(self.runtime,old.token)
        self.assertFalse(self.fake.received)

    async def test_missing_file_configuration_is_disabled_and_foreign_account_fails_before_io(self):
        raw=dict(self.raw);raw.pop('files');raw.pop('subagent_profile')
        await self.build(raw)
        self.assertIsNone(self.files);self.assertIsNone(self.session.artifact_authority)
        other=AccountKey('qq-official','TEST_FOREIGN_HOST','test')
        # A fabricated token cannot select another configured account, even if
        # its fields resemble a valid token. No filesystem work precedes check.
        foreign=SimpleNamespace(account_key=other.key)
        with self.assertRaises(HostConfigurationError):self.host.migration_factory(self.runtime,foreign)
        self.assertFalse((Path(self.temp.name)/'files').exists())
        self.assertFalse(self.fake.received)

    async def test_old_native_contract_remains_frozen_and_cannot_authorize_new_uploader(self):
        endpoint=self.query('SELECT generation,route_epoch,capability_revision FROM qq_official_endpoints WHERE endpoint_key=?',
            (self.endpoint.key,))[0]
        self.raw['files']['native_routes']=[dict(endpoint=self.endpoint.key,
            endpoint_generation=endpoint['generation'],route_epoch=endpoint['route_epoch'],
            capability_revision=endpoint['capability_revision'],max_bytes=1000,
            verified_at=self.now,expires_at=self.now+100,contract='TEST_OLD_CONTRACT',file_type=4)]
        config=await self.build()
        self.assertEqual(config.native_routes[0].contract,'TEST_OLD_CONTRACT')
        artifact=dict(account_key=self.account.key,endpoint_key=self.endpoint.key,requester_actor_key=self.actor.key,
            endpoint_generation=endpoint['generation'],route_epoch=endpoint['route_epoch'],byte_size=100,draft=False)
        with self.tx() as cursor:self.assertFalse(self.files._route(cursor,artifact,'native'))
        self.assertEqual(self.files.config.native_evidence(None,self.endpoint).contract,'TEST_OLD_CONTRACT')
        self.assertFalse(self.fake.received)
