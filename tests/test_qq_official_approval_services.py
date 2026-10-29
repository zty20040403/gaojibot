"""Actual epoch/native source/owner binding behind the trusted host factory."""
from types import SimpleNamespace
from unittest.mock import Mock

import httpx

from src.bot_security.store import SecurityError
from src.plugins.ai_chat.adapters.qq_official import execution
from src.plugins.ai_chat.adapters.qq_official.admin_bindings import AdminBindingStore
from src.plugins.ai_chat.adapters.qq_official.approval_services import approval_services_factory
from src.plugins.ai_chat.adapters.qq_official.host_authority import AllowedService
from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
from src.plugins.ai_chat.adapters.qq_official.migration_types import MigrationDenied
from src.plugins.ai_chat.fleet_client import FleetControlClient
from src.plugins.ai_chat.subagents import SubAgentStore
from tests.test_qq_official_migration_verification_host import VerificationHostFixture


class ApprovalServicesTests(VerificationHostFixture):
    async def test_existing_security_view_and_fleet_transport_keep_account_authority_separate(self):
        tasks=SubAgentStore(self.database);self.addCleanup(tasks.close)
        self.runtime.application=SimpleNamespace(subagent_store=tasks,db_call=self.calls)
        job=self.jobs.claim_due('TEST_FACTORY',kinds=(account_job_kind('turn',self.account.key),),now=self.now)[0]
        with self.tx() as c:
            handle=execution.claim_execution(c,token=self.token,job=job,now=self.now,deadline_at=self.now+120,
                admission=self.store.admitted).handle
        def no_http(request):raise AssertionError('no host request is authorized by construction')
        original=FleetControlClient('https://test.invalid','/TEST_NONEXISTENT_TOKEN',
            transport=httpx.MockTransport(no_http))
        self.addAsyncCleanup(original.close)
        original.authorization=Mock()
        self.runtime.sender=self.sender
        factory=approval_services_factory(security=self.mapping_security,
            services=(AllowedService('testhost','test.service','TEST_BACKEND'),),fleet_client=original)
        service=factory(self.runtime,self.token)
        try:
            self.assertIsNot(service.mobile.store,self.mapping_security)
            self.assertIs(service.mobile.store._database,self.mapping_security._database)
            self.assertIsNone(self.mapping_security.transport)
            self.assertIsNot(service.host_session_factory.client,original)
            self.assertIs(service.host_session_factory.client._client,original._client)
            self.assertIsNot(service.host_session_factory.client.authorization,original.authorization)
            from src.bot_security.passwords import hash_password
            password_hash=hash_password('TEST_FACTORY_PASSWORD_123456')
            with self.tx() as c:
                self.assertFalse(service.host_admission(c,handle))
                # Trusted setup creates a real local official admin binding to
                # the original C2C owner; the group link came from bind reducer.
                owner=c.execute('SELECT revision FROM qq_official_owner_grants WHERE c2c_actor_key=?',
                    (self.owner.key,)).fetchone()
                admins=AdminBindingStore(clock=lambda:self.now,control_guard=lambda c,**kw:kw['authorization'] is self)
                admins.create_official_admin(c,username='test_factory_admin',password_hash=password_hash,
                    endpoint=self.private,actor=self.owner,owner_revision=owner['revision'],authorization=self)
                self.assertTrue(service.host_admission(c,handle))
            # Logical freeze rejects the original source; config cannot re-sign
            # it with a new epoch or turn an old text task into a host task.
            await self.verifying()
            with self.tx() as c:
                with self.assertRaises(MigrationDenied):service.host_admission(c,handle)
        finally:await service.close()

    async def test_default_off_and_missing_epoch_are_closed(self):
        class NoAccess:
            def __getattribute__(self,name):raise AssertionError(name)
        self.assertIsNone(approval_services_factory()(NoAccess(),NoAccess()))
        service=approval_services_factory(security=self.mapping_security)(self.runtime,self.token)
        try:
            self.assertIsNone(service.host_admission)
            self.assertIsNone(service.host_session_factory)
        finally:await service.close()
        with self.assertRaises(SecurityError):
            approval_services_factory(security=self.mapping_security)(SimpleNamespace(database=self.database,sender=self.sender,migration_service=None),self.token)
