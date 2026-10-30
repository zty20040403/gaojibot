"""Authenticated TxA -> existing Mobile quorum -> real M3 -> shared Fleet path."""
import json
import re
from datetime import datetime, timezone
from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official.approval import OfficialApprovalService
from src.plugins.ai_chat.adapters.qq_official.binding import BindingReducer
from src.plugins.ai_chat.adapters.qq_official.inbox import OfficialInboxStore
from src.plugins.ai_chat.adapters.qq_official.ingest import process_inbox
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from tests.qq_official_host_fixture import HostFixture
from tests.qq_official_approval_fixture import SECRET


class ApprovalIngressTests(HostFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.inbox_store = OfficialInboxStore(self.database, jobs=self.jobs,
            binding=BindingReducer(SECRET, epoch_guard=assert_epoch, authority=self.owners, clock=lambda:self.now),
            authority=self.owners, clock=lambda:self.now)
        self.addCleanup(self.inbox_store.close)
        self.inbox_store.approval_store = self.store
        self.session, self.seq = 'TEST_APPROVAL_INGRESS_SESSION', 0
        self.inbox_store.accept({'op':0,'t':'READY','s':0,'d':{'session_id':self.session}},
            token=self.token, session_id=self.session)
        self.service = OfficialApprovalService(token=self.token,mobile=self.mobile)

    def frame(self,text,*,actor='TEST_ADMIN_A',group=None,mentions=None):
        self.seq += 1
        data={'id':'TEST_APPROVAL_INPUT_'+str(self.seq),'content':text,
              'author':{'member_openid' if group else 'user_openid':actor},
              'timestamp':datetime.fromtimestamp(self.now,timezone.utc).isoformat()}
        if group:data['group_openid']=group
        if mentions is not None:data['mentions']=mentions
        return {'op':0,'t':'GROUP_AT_MESSAGE_CREATE' if group else 'C2C_MESSAGE_CREATE',
                's':self.seq,'id':'TEST_APPROVAL_EVENT_'+str(self.seq),'d':data}

    async def accept(self,text=None,*,frame=None,**kwargs):
        frame = frame or self.frame(text,**kwargs)
        if frame['t']=='C2C_MESSAGE_CREATE':
            self.fake.add_window('user',frame['d']['author']['user_openid'],frame['d']['id'])
        result=await self.calls(self.inbox_store.accept,frame,token=self.token,session_id=self.session)
        if result.approval is not None:await self.service.consume(result.approval)
        return result

    def code(self):
        return re.search(r'一次性口令：([0-9]{6})',self.fake.received[-1]['payload']['content'])[1]

    def assert_private(self,*secrets):
        for table in ('qq_official_inbound_events','qq_official_admin_control_receipts',
                      'admin_approvals','qq_official_admin_otp_sources','delivery_plans','deliveries',
                      'delivery_attempts','messages','durable_jobs'):
            encoded=json.dumps(self.query('SELECT * FROM '+table),default=str)
            for value in secrets:
                with self.subTest(table=table):self.assertNotIn(value,encoded)

    async def test_real_ingress_confirmation_and_review_reach_existing_shared_host(self):
        record=(await self.prepare())['operation'];first_code=self.code()
        confirmed=await self.accept(first_code)
        self.assertEqual(confirmed.approval.decision,'accepted')
        approval=self.query("SELECT * FROM admin_approvals WHERE kind='server_task'")[0]
        self.assertEqual(approval['status'],'awaiting_quorum')
        frame=self.frame('复核 '+approval['approval_id'],actor='TEST_ADMIN_B')
        reviewed=await self.accept(frame=frame)
        self.assertEqual(reviewed.approval.decision,'sending');second_code=self.code()
        self.assertEqual(len(self.fake.received),2)
        duplicate=await self.accept(frame=frame)
        self.assertEqual(duplicate.disposition,'duplicate');self.assertIsNone(duplicate.approval)
        self.assertEqual(len(self.fake.received),2)
        self.assertEqual((await self.accept(second_code,actor='TEST_ADMIN_B')).approval.decision,'accepted')
        self.assertTrue(await self.mobile.run_once())
        self.assertTrue((await self.approve(record))['submitted'])
        self.assertEqual(len([r for r in self.http_calls if r[0]=='POST' and r[1].endswith('/approve')]),1)
        rows=self.query('SELECT * FROM qq_official_admin_control_receipts')
        self.assertEqual(len(rows),3)
        self.assertEqual({r['source_session_id'] for r in rows},{self.session})
        for row in rows:
            with self.tx() as c:
                result=process_inbox(c,inbox_id=row['inbox_id'],lease_token=self.token,ledger=self.ledger,
                    jobs=self.jobs,epoch_guard=assert_epoch,admission=self.inbox_store.admitted,now=self.now)
            self.assertEqual(result.status,'processed')
        self.assert_private(first_code,second_code)

    async def test_missing_factory_unknown_and_group_forgery_never_store_body(self):
        self.inbox_store.approval_store=None
        for text in ('654321','确认 AP-UNKNOWN 654321','<@!TEST_FAKE> 654321',
                     '<@!TEST_FAKE> /confirm AP-ABCDEF123456 654321'):
            result=await self.accept(text)
            self.assertIn(result.disposition,{'control','quarantined'})
        self.inbox_store.approval_store=self.store
        for text in ('654321','确认 AP-ABCDEF123456 654321'):
            result=await self.accept(text,group='TEST_OTHER_GROUP')
            self.assertEqual(result.disposition,'quarantined')
        result=await self.accept('确认 AP-ABCDEF123456 654321')
        self.assertEqual(result.approval.decision,'denied')
        self.assertFalse(self.fake.received)
        self.assert_private('654321','AP-UNKNOWN','AP-ABCDEF123456','TEST_FAKE')

    async def test_trusted_prefix_is_control_but_forged_prefix_never_confirms(self):
        await self.prepare();code=self.code()
        fake=await self.accept('<@!TEST_BOT> '+code)
        self.assertEqual(fake.disposition,'quarantined')
        good=await self.accept('<@!TEST_BOT> '+code,mentions=[{'id':'TEST_BOT','is_you':True,'bot':True}])
        self.assertEqual(good.approval.decision,'accepted')
        self.assert_private(code)

    async def test_control_reduction_and_gateway_receipt_rollback_together(self):
        await self.prepare();code=self.code();frame=self.frame(code)
        from src.plugins.ai_chat.adapters.qq_official import inbox as module
        actual=module.apply_control
        def failed(*args,**kwargs):
            actual(*args,**kwargs)
            raise RuntimeError('TEST_AFTER_REDUCTION')
        with patch.object(module,'apply_control',side_effect=failed):
            with self.assertRaises(RuntimeError):await self.accept(frame=frame)
        self.assertEqual(self.query("SELECT status FROM admin_approvals WHERE kind='server_task'")[0]['status'],'pending')
        self.assertFalse(self.query('SELECT * FROM qq_official_gateway_receipts WHERE session_id=? AND seq=?',(self.session,frame['s'])))
        self.assertEqual((await self.accept(frame=frame)).approval.decision,'accepted')

    async def test_old_pending_control_is_scrubbed_before_ledger_or_turn(self):
        frame=self.frame('ordinary test text')
        accepted=await self.accept(frame=frame)
        self.assertEqual(accepted.disposition,'inbox')
        frame['d']['content']='654321'
        self.query('UPDATE qq_official_inbound_events SET payload_json=? WHERE inbox_id=?',
                   (json.dumps(frame),accepted.inbox_id))
        with self.tx() as c:
            result=process_inbox(c,inbox_id=accepted.inbox_id,lease_token=self.token,ledger=self.ledger,
                jobs=self.jobs,epoch_guard=assert_epoch,admission=self.inbox_store.admitted,now=self.now)
        self.assertEqual(result.status,'quarantined')
        row=self.query('SELECT * FROM qq_official_inbound_events WHERE inbox_id=?',(accepted.inbox_id,))[0]
        self.assertIsNone(row['payload_json']);self.assertIsNone(row['turn_text']);self.assertIsNone(row['canonical_message_id'])
        self.assert_private('654321')

    async def test_runtime_factory_attaches_for_one_epoch_and_consumes_before_ingest(self):
        import asyncio
        from contextlib import asynccontextmanager
        from types import SimpleNamespace
        from unittest.mock import Mock
        from src.plugins.ai_chat.adapters.qq_official.runtime import OfficialRuntime
        from src.plugins.ai_chat.adapters.qq_official.config import OfficialConfig, LoadedOfficialConfig
        from src.plugins.ai_chat.adapters.qq_official.approval import ApprovalOtpSender
        from tests.test_qq_official_runtime import ControlledGateway
        opened=asyncio.Queue();created=[]
        @asynccontextmanager
        async def protocol(loaded,**options):
            gateway=ControlledGateway(**options,ordinal=1)
            gateway.rest_client=self.rest
            await opened.put(gateway)
            try:yield gateway
            finally:await gateway.stop()
        def factory(current,token):
            self.mobile.official_sender=ApprovalOtpSender(sender=current.sender,transport=self.transport,store=self.store)
            service=OfficialApprovalService(token=token,mobile=self.mobile)
            created.append(service)
            return service
        runtime=OfficialRuntime(OfficialConfig(),self.database,logger=Mock(),protocol_factory=protocol,
            approval_factory=factory,probe_seconds=.01)
        runtime.store=self.inbox_store
        runtime.loaded=LoadedOfficialConfig(account=self.account,credentials=self.tokens.credentials,
            api_base=self.fake.base_url,owner_openids=('TEST_ADMIN_A','TEST_ADMIN_B'),
            private_policy='allowlist',private_allowed_openids=(),binding_pepper=SECRET,
            allow_test_server=True)
        runtime.jobs=self.jobs;runtime.deliveries=self.deliveries;runtime.outbox=self.outbox
        runtime.ledger=self.ledger;runtime.journal=self.journal;runtime._token=self.token
        task=asyncio.create_task(runtime._serve(SimpleNamespace(probe=lambda:True),self.token,
            self.inbox_store.checkpoint(self.token)))
        try:
            gateway=await asyncio.wait_for(opened.get(),3)
            await asyncio.wait_for(gateway.ready.wait(),3)
            self.assertTrue(runtime.status()['approval_transport_connected'])
            self.assertIsNotNone(runtime.admin_identity_service)
            self.assertIs(self.inbox_store.admin_identity_service,runtime.admin_identity_service)
            await self.prepare();code=self.code()
            await gateway.emit(code,owner='TEST_ADMIN_A')
            self.assertEqual(self.query("SELECT status FROM admin_approvals WHERE kind='server_task'")[0]['status'],'awaiting_quorum')
            self.assertEqual(len(self.query('SELECT * FROM qq_official_admin_control_receipts')),1)
            self.assert_private(code)
            await gateway.finish()
            await asyncio.wait_for(task,5)
        finally:
            if not task.done():
                task.cancel();await asyncio.gather(task,return_exceptions=True)
            await runtime._calls.close()
        self.assertEqual(len(created),1)
        self.assertIsNone(runtime.approval_service)
        self.assertIsNone(runtime.admin_identity_service)
        self.assertIsNone(self.inbox_store.admin_identity_service)
        self.assertIsNone(self.inbox_store.approval_store)
        self.assertIsNone(self.outbox.approval_authority)
        self.assertTrue(created[0]._closed)

    async def test_expired_approval_is_consumed_without_body_or_effect(self):
        await self.prepare();code=self.code()
        request=self.query("SELECT * FROM admin_approvals WHERE kind='server_task'")[0]
        self.query('UPDATE admin_approvals SET expires_at=? WHERE approval_id=?',(self.now,request['approval_id']))
        result=await self.accept('确认 '+request['approval_id']+' '+code)
        self.assertEqual(result.approval.decision,'denied')
        self.assertIn(self.query('SELECT status FROM admin_approvals WHERE approval_id=?',(request['approval_id'],))[0]['status'],
                      {'expired','cancelled'})
        self.assertEqual(len(self.fake.received),1)
        self.assertFalse([r for r in self.http_calls if r[0]=='POST' and r[1].endswith('/approve')])
        self.assert_private(code)
