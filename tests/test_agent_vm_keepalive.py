"""Six-hour agents must not lose their VM to the two-hour idle cleaner."""
import asyncio
import unittest
from unittest.mock import AsyncMock

from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.background_tools import workspace_owner
from src.plugins.ai_chat.platform_types import EndpointKey
from src.plugins.ai_chat.message_ir import MessageBody,TextNode
from tests import test_vm_lifecycle as fixture
from tests.qq_official_outbox_fixture import OutboxFixture


class AgentVMKeepaliveTests(OutboxFixture,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixture.VMLifecycleTests.asyncSetUp(self)
        self.manager._conversation_locks = {}

    async def test_active_agent_holds_owner_vm_even_after_two_hours_and_token_replacement(self):
        ledger = MessageLedger(self.database); self.addCleanup(ledger.close)
        message = ledger.record_message(self.endpoint.scope,native_message_id='TEST_AGENT',sender_native_user_id='TEST_MEMBER',
            sender_display='TEST',body=MessageBody((TextNode(0,'TEST long job'),)),occurred_at=self.now,
            actor=self.actor,source_endpoint=self.endpoint)
        source = dict(account_key=self.account.key,endpoint_key=self.endpoint.key,actor_key=self.actor.key,
            scope_key=self.endpoint.scope.key,source_execution_id=None,canonical_message_id=message.canonical_message_id,
            native_message_id='TEST_AGENT',endpoint_generation=1,route_epoch=1)
        with self.tx() as cursor:
            row = self.service.store.submit(cursor,kind='TEST_AGENT',key='TEST_LONG',source=source,task='TEST',title='TEST',profile='TEST_MODEL')
        self.service.store.start(row['job_id']); self.now += 7201
        # VM-token rotation/replacement cannot erase a human agent's owner hold.
        self.lifecycle.store.issue(self.endpoint,'s654321',self.lifecycle.store.authenticate(self.token_value).boot_id,expected_route=(1,1))
        self.assertFalse(await self.lifecycle.stopping(self.owner,'s123456',automatic=True))
        other = workspace_owner(EndpointKey(self.account,'group','TEST_OTHER'))
        self.assertTrue(await self.lifecycle.stopping(other,'s654322',automatic=True))
        self.service.store.finish(row['job_id'],state='completed',error='',report='TEST')
        self.assertTrue(await self.lifecycle.stopping(self.owner,'s123456',automatic=True))

    async def test_late_agent_start_waits_for_in_progress_owner_cleanup_without_blocking_other_owners(self):
        self.manager.ensure_default = AsyncMock(return_value={'sandbox_id':'s123456'})
        self.manager.start_owned = AsyncMock()
        lock = self.manager._conversation_locks.setdefault(self.owner,asyncio.Lock())
        async with lock:
            task = asyncio.create_task(self.service.start_vm({'endpoint_key':self.endpoint.key}))
            try:
                await asyncio.sleep(.02)
                self.manager.ensure_default.assert_not_awaited()
                self.assertFalse(task.done())
                other = EndpointKey(self.account,'group','TEST_OTHER')
                await asyncio.wait_for(self.service.start_vm({'endpoint_key':other.key}),1)
                self.manager.start_owned.assert_awaited_once_with(workspace_owner(other),'s123456')
                self.manager.start_owned.reset_mock()
            except BaseException:
                task.cancel(); await asyncio.gather(task,return_exceptions=True)
                raise
        self.assertEqual(await asyncio.wait_for(task,3),'s123456')
        self.manager.start_owned.assert_awaited_once_with(self.owner,'s123456')
