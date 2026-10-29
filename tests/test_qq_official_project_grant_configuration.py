"""Configured grant lifetime reaches actual project, command and console writes."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from src.plugins.ai_chat.adapters.qq_official import execution
from src.plugins.ai_chat.adapters.qq_official.host_config import configured_migration_factory
from src.plugins.ai_chat.adapters.qq_official.migration_types import MigrationDenied
from src.plugins.ai_chat.adapters.qq_official.project_authority import Denied, ProjectAuthority
from src.plugins.ai_chat.adapters.qq_official.project_commands import CommandProjectProvider
from src.plugins.ai_chat.application.project_grant_commands import GrantCommand
from src.plugins.ai_chat.projects import ProjectAccess, ProjectStore
from tests.qq_official_migration_project_fixture import install_project_owner
from tests.qq_official_project_fixture import ProjectFixture
from tests.test_qq_official_migration_services import MigrationServicesFixture


class GrantConfigurationTests(unittest.TestCase):
    def test_authority_rejects_invalid_caps_before_any_database_work(self):
        for value in (True,0,59,86401,1.5,'300'):
            with self.subTest(value=value),self.assertRaises(Denied):
                ProjectAuthority(max_grant_ttl_seconds=value)


class ForegroundLifetimeTests(ProjectFixture):
    def test_creation_uses_configured_expiry_and_new_policy_does_not_reissue_old_grant(self):
        self.provider=self.provider_for(max_grant_ttl_seconds=300)
        with self.tx() as cursor:self.access=self.provider.issue(cursor)
        project=self.create()
        grant=dict(self.query('SELECT * FROM qq_official_project_grants')[0])
        self.assertEqual(grant['expires_at']-grant['issued_at'],300)
        self.now+=61
        self.provider=self.provider_for(max_grant_ttl_seconds=60)
        with self.tx() as cursor:self.access=self.provider.issue(cursor)
        self.assertEqual(self.store.get(self.access,project['project_id'])['project_id'],project['project_id'])
        current=self.query('SELECT * FROM qq_official_project_grants')[0]
        self.assertEqual((current['revision'],current['issued_at'],current['expires_at']),
                         (grant['revision'],grant['issued_at'],grant['expires_at']))


class CommandLifetimeTests(ProjectFixture):
    def claim(self,purpose='final',kind='chat'):
        return super().claim(purpose=purpose,kind='command')

    def begin(self,handle):
        with self.tx() as cursor:execution.begin_command_execution(cursor,**self.args(handle))

    def provider_for(self,**changes):
        return CommandProjectProvider(**(dict(token=self.token,handle=self.handle,job=self.job,
            admission=self.admission,route_epoch=0,route_guard=self.route_guard,
            clock=lambda:self.now,max_grant_ttl_seconds=60)|changes))

    def test_explicit_command_cannot_bypass_cap_via_its_separate_issuance_authority(self):
        project=self.create();grant=self.query('SELECT * FROM qq_official_project_grants')[0]
        command=GrantCommand('issue',project['project_id'],self.message.canonical_message_id,
            frozenset({'project.read'}),61,grant['grant_id'],1)
        with self.tx() as cursor,self.assertRaises(Denied):self.provider.grant_command(cursor,command)
        self.assertEqual(self.query('SELECT revision FROM qq_official_project_grants')[0][0],1)
        with self.tx() as cursor:result=self.provider.grant_command(cursor,replace(command,ttl_seconds=60))
        self.assertEqual(result['grant']['revision'],2)
        current=self.query('SELECT * FROM qq_official_project_grants')[0]
        self.assertEqual(current['expires_at']-current['issued_at'],60)
        self.assertEqual(current['capabilities'],['project.read'])


class ConsoleLifetimeTests(MigrationServicesFixture):
    async def test_startup_capture_bounds_new_migration_stage_without_extending_an_existing_grant(self):
        # Build through the real protected config factory, then use its actual
        # immutable startup value in the live console fixture.
        from types import SimpleNamespace
        with TemporaryDirectory() as directory:
            path=Path(directory)/'host.json'
            path.write_text(json.dumps(dict(version=1,account=self.account.key,
                onebot_accounts=[dict(account=self.source.account.key,deployment_digest='e'*64)])))
            path.chmod(0o600)
            env=dict(AI_QQ_MIGRATION_ENABLED='true',AI_QQ_MIGRATION_CONFIG_FILE=str(path),
                AI_QQ_OFFICIAL_ENABLED='true',AI_QQ_OFFICIAL_PROJECT_GRANT_TTL_SECONDS='60',
                AI_QQ_OFFICIAL_FILE_LINK_TTL_SECONDS='120')
            factory=configured_migration_factory(env)
            env['AI_QQ_OFFICIAL_PROJECT_GRANT_TTL_SECONDS']='86400'
            env['AI_QQ_OFFICIAL_FILE_LINK_TTL_SECONDS']='3600'
            context=SimpleNamespace(database=self.database,delivery_store=self.deliveries,
                db_call=self.calls,mobile_authorization=SimpleNamespace(store=self.mapping_security))
            captured=factory(context,SimpleNamespace(handlers=SimpleNamespace(chat=None)))
            try:
                self.assertEqual(captured.project_grant_ttl_seconds,60)
                self.assertEqual(context.official_host.file_link_ttl_seconds,120)
                self.service.project_grant_ttl_seconds=captured.project_grant_ttl_seconds
            finally:
                await captured.close()
        install_project_owner(self)
        with TemporaryDirectory() as directory:
            projects=ProjectStore(self.database,Path(directory));self.addCleanup(projects.close)
            principal=self.ledger.ensure_principal_identity('onebot-v11',self.source_actor.native_user_id,'TEST source')
            project=projects.create(ProjectAccess(self.source_scope.with_actor(self.source_actor.native_user_id),
                principal,member=True),'TEST bounded migration',now=self.now)
            request=dict(source_endpoint=self.source.key,source_scope=self.source_scope.key,
                source_actor=self.source_actor.key,target_endpoint=self.endpoint.key,target_actor=self.actor.key,
                issuer_actor=self.actor.key,project_id=project['project_id'],mapping_id=self.mapping_evidence.mapping_id,
                mapping_revision=self.mapping_evidence.revision,capabilities=['project.read'],expires_at=self.now+61)
            with self.assertRaises(MigrationDenied):
                await self.service.console.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,
                    action='project_grant_stage',request=request)
            self.assertEqual(self.query('SELECT * FROM qq_official_project_grants'),[])
            result=await self.service.console.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,
                action='project_grant_stage',request=request|{'expires_at':self.now+60})
            original=self.query('SELECT * FROM qq_official_project_grants')[0]
            self.service.project_grant_ttl_seconds=86400
            with self.assertRaises(MigrationDenied):
                await self.service.console.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,
                    action='project_grant_stage',request=request|{'expected_revision':result['revision'],'expires_at':self.now+600})
            current=self.query('SELECT * FROM qq_official_project_grants')[0]
            self.assertEqual((current['revision'],current['expires_at']),(original['revision'],original['expires_at']))
