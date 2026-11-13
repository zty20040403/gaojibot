"""Real consent/receipt/job fences, with a non-network control protocol double."""
import asyncio
import threading
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch

from src.plugins.ai_chat.adapters.qq_official.ops_execution import OpsExecution,summary
from src.plugins.ai_chat.ops_client import OpsTransportError,Reference
from tests.ops_action_fixture import OpsFixture
from tests import test_ops_replies as replies_fixture


class OpsSummaryTests(unittest.TestCase):
    def test_json_escaped_credentials_are_removed_before_audit_or_model_output(self):
        secret = 'TEST_"SECRET\\'+'x'*32
        client = SimpleNamespace(secret=secret,config=SimpleNamespace(base_url='https://control.example.test',secret_file='/TEST_PRIVATE'))
        value = {'status':'failed','result':{'stdout':secret,'nested':[secret,'https://control.example.test/path']}}
        output = summary(client,value)
        self.assertNotIn('TEST_',output); self.assertNotIn('control.example.test',output)
        self.assertEqual(json.loads(output)['result']['stdout'],'[redacted]')


class OpsExecutionTests(OpsFixture,unittest.IsolatedAsyncioTestCase):
    inbound = replies_fixture.OpsReplyTests.inbound
    make_sender = replies_fixture.OpsReplyTests.make_sender
    ensure_current = replies_fixture.OpsReplyTests.ensure_current
    fast_sleep = replies_fixture.OpsReplyTests.fast_sleep
    prepare = replies_fixture.OpsReplyTests.prepare
    settle = replies_fixture.OpsReplyTests.settle
    offer = replies_fixture.OpsReplyTests.offer
    accept = replies_fixture.OpsReplyTests.accept
    action = replies_fixture.OpsReplyTests.action
    asyncTearDown = replies_fixture.OpsReplyTests.asyncTearDown

    async def asyncSetUp(self):
        await replies_fixture.OpsReplyTests.asyncSetUp(self)
        self.trace,self.before_approve,self.waiting = [],None,None
        self.remote_result = {'status':'succeeded','result':{'stdout':'TEST_SECRET_'*4+' https://control.example.test /TEST_PRIVATE_TOKEN '+('汉'*10000)}}
        self.current = AsyncMock()
        self.execution = OpsExecution(self.notices,transaction=self.tx,calls=self.calls,current=self.current,
            client_factory=self.make_client,sleep=lambda seconds:asyncio.sleep(0))

    def make_client(self,config,plan,parameters,**kwargs):
        client = SimpleNamespace(config=config,secret=config.secret(),binding=config.binding(plan,config.secret()))
        ref = Reference('op_'+'d'*32,'e'*64,1)
        async def prepare():
            await kwargs['current'](); self.trace.append('prepare')
            if self.before_approve is not None:await self.before_approve()
            return ref
        async def approve(value):
            await kwargs['current'](); self.assertEqual(value,ref); self.trace.append('approve')
            if self.waiting is not None:await self.waiting()
            return {'status':'queued'}
        async def status(value):
            await kwargs['current'](); self.assertEqual(value,ref); self.trace.append('status')
            return self.remote_result
        client.prepare,client.approve,client.status = prepare,approve,status
        return client

    async def approved(self):
        await self.offer(); self.accept(); self.assertEqual(self.action()['state'],'approved')

    async def execute(self):
        return await self.execution.execute(self.proposal.action['action_id'])

    async def test_only_verified_consent_and_receipt_execute_once_and_summary_is_bounded(self):
        self.assertEqual((await self.execute())['state'],'sending'); self.assertEqual(self.trace,[])
        await self.approved(); row = await self.execute()
        self.assertEqual(row['state'],'succeeded'); self.assertEqual(row['remote_id'],'op_'+'d'*32)
        self.assertLessEqual(len(row['output_summary'].encode()),16384)
        for private in ('TEST_SECRET','control.example.test','TEST_PRIVATE_TOKEN'):
            self.assertNotIn(private,row['output_summary'])
        self.assertIn('已截断',row['output_summary'])
        self.assertEqual((await self.execute())['state'],'succeeded')
        self.assertEqual(self.trace,['prepare','approve','status'])

    async def test_changed_parameters_after_consent_cannot_change_the_execution(self):
        await self.approved()
        self.query('UPDATE ops_actions SET parameters_json=? WHERE action_id=?',('{"unit":"TEST B.service"}',self.proposal.action['action_id']))
        with self.assertRaises(PermissionError):await self.execute()
        self.assertEqual(self.action()['state'],'cancelled'); self.assertEqual(self.trace,[])

    async def test_terminal_output_still_requires_the_original_live_owner_grant(self):
        await self.approved(); self.assertEqual((await self.execute())['state'],'succeeded')
        self.query("UPDATE qq_official_owner_grants SET state='revoked',revision=revision+1")
        with self.assertRaises(PermissionError):await self.execute()
        self.assertEqual(self.action()['state'],'succeeded'); self.assertEqual(self.trace,['prepare','approve','status'])

    async def test_fast_reply_without_a_confirmed_notice_cannot_execute(self):
        ref = self.prepare()
        self.outbox.prepare(self.token,ref.delivery_ids[0],send_mode='passive',msg_id='TEST_SOURCE',msg_seq=1)
        self.accept(); self.assertEqual(self.action()['state'],'approved')
        self.assertEqual((await self.execute())['state'],'cancelled'); self.assertEqual(self.trace,[])

    async def test_owner_revocation_after_prepare_preserves_reference_but_never_approves(self):
        await self.approved()
        async def revoke():self.query("UPDATE qq_official_owner_grants SET state='revoked',revision=revision+1")
        self.before_approve = revoke
        with self.assertRaises(PermissionError):await self.execute()
        self.assertEqual(self.action()['state'],'unknown'); self.assertEqual(self.action()['remote_id'],'op_'+'d'*32)
        self.assertEqual(self.trace,['prepare'])

    async def test_lost_approval_response_is_unknown_and_not_retried(self):
        await self.approved()
        async def lost():raise OpsTransportError('ops_control_request_interrupted')
        self.waiting = lost
        self.assertEqual((await self.execute())['state'],'unknown')
        self.assertEqual((await self.execute())['state'],'unknown')
        self.assertEqual(self.trace,['prepare','approve'])

    async def test_parallel_duplicate_does_not_send_a_second_remote_intent(self):
        await self.approved(); entered,release = asyncio.Event(),asyncio.Event()
        async def wait():entered.set(); await release.wait()
        self.waiting = wait
        pending = asyncio.create_task(self.execute())
        try:
            await asyncio.wait_for(entered.wait(),3)
            self.assertEqual((await self.execute())['state'],'executing')
            self.assertEqual(self.trace,['prepare','approve'])
        finally:release.set()
        self.assertEqual((await pending)['state'],'succeeded')

    async def test_cancellation_after_committed_claim_drains_cleanup_and_never_dispatches(self):
        await self.approved(); claimed,release = threading.Event(),threading.Event()
        original = self.execution.claim
        def delayed(action):
            result = original(action); claimed.set()
            if not release.wait(5):raise RuntimeError('TEST_RELEASE_TIMEOUT')
            return result
        with patch.object(self.execution,'claim',side_effect=delayed):
            pending = asyncio.create_task(self.execute())
            try:
                self.assertTrue(await asyncio.to_thread(claimed.wait,3))
                pending.cancel(); await asyncio.sleep(0); pending.cancel()
            finally:release.set()
            with self.assertRaises(asyncio.CancelledError):await pending
        self.assertEqual(self.action()['state'],'unknown'); self.assertEqual(self.trace,[])

    async def test_interrupted_remote_call_does_not_claim_rollback_or_success(self):
        await self.approved(); entered,closed = asyncio.Event(),asyncio.Event()
        async def wait():
            entered.set()
            try:await asyncio.Future()
            finally:closed.set()
        self.waiting = wait
        pending = asyncio.create_task(self.execute()); await asyncio.wait_for(entered.wait(),3)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):await pending
        self.assertTrue(closed.is_set()); self.assertEqual(self.action()['state'],'unknown')
        self.assertEqual(self.trace,['prepare','approve'])
