"""Real PG/Tx/Outbox/RestClient+loopback FakePlatform tests for target-bot question notices."""
import asyncio
import threading
import unittest
from unittest.mock import patch

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official import turn_source
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.adapters.qq_official.link_ingress import LinkIngress
from src.plugins.ai_chat.adapters.qq_official.question_authority import QuestionAuthority
from src.plugins.ai_chat.adapters.qq_official.question_notices import QuestionNotices,render_notice
from src.plugins.ai_chat.adapters.qq_official.questions import Questions
from src.plugins.ai_chat.adapters.qq_official.send_store import OfficialSendStore
from src.plugins.ai_chat.adapters.qq_official.send_types import OutboxInvariantError
from src.plugins.ai_chat.adapters.qq_official.sender import OfficialSender
from src.plugins.ai_chat.adapters.qq_official.turn_platform import ExecutionBinding
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.platform_types import ActorKey
from src.qq_official.credentials import Credentials
from src.qq_official.http import RestClient
from src.qq_official.testing.fake_platform import FakePlatform
from src.qq_official.token import TokenManager
from tests import test_qq_official_sender as sender_fixture
from tests.conversation_question_fixture import QuestionFixture


class QuestionNoticeTests(QuestionFixture,unittest.IsolatedAsyncioTestCase):
    fast_sleep = sender_fixture.OfficialSenderTests.fast_sleep
    link_approver = 'TEST_QUESTION_LINK_APPROVER'

    async def asyncSetUp(self):
        self.calls = DatabaseCalls()
        self.deliveries = DeliveryStore(self.database); self.addCleanup(self.deliveries.close)
        self.outbox = OfficialSendStore(self.deliveries,admission=self.admission,
            pepper=b'TEST_QUESTION_NOTICE_PEPPER_1234567890',clock=lambda:self.now)
        self.binding = ExecutionBinding(turn_source,self.token,self.handle,self.job,self.admission)
        self.ingress = LinkIngress(self.auth,admission=self.admission)
        self.authority = QuestionAuthority(self.ingress,admission=self.admission)
        self.questions = Questions(self.authority,self.jobs)
        self.notices = QuestionNotices(self.questions,self.outbox)
        self.outbox.question_notice_authority = self.notices
        with self.tx() as cursor:
            observe_ready(cursor,self.token,{'d':{'user':{'id':'TEST_NOTICE_BOT','bot':True}}},self.now)
            cursor.execute("UPDATE qq_official_endpoints SET proactive_state='allowed' WHERE endpoint_key=?",(self.target.key,))
        self._qseq = 0
        self.new_running_question()
        self.fake = FakePlatform(clock=lambda:self.now)
        self.fake.app_id = self.account.app_id
        await self.fake.start()
        self.fake.proactive_allowed[('group',self.target.native_conversation_id)] = True
        self.tokens = TokenManager(Credentials(self.account.app_id,self.fake.app_secret),self.fake.base_url,allow_test_server=True)
        self.rest = RestClient(self.tokens,allow_test_server=True)
        self.sender = self.make_sender()

    async def asyncTearDown(self):
        await self.sender.stop()
        await self.calls.close()
        await self.rest.aclose()
        await self.tokens.aclose()
        await self.fake.close()

    async def ensure_current(self):
        def check():
            with self.tx() as cursor:assert_epoch(cursor,self.token)
        await self.calls(check)

    def make_sender(self,**kwargs):
        return OfficialSender(self.account,self.token,self.outbox,self.rest,self.ledger,
            database_calls=self.calls,ensure_current=self.ensure_current,
            wall_clock=lambda:self.now,poll_seconds=0.01,sleep=self.fast_sleep,**kwargs)

    def new_running_question(self):
        if getattr(self,'question',None) is not None and self.question['state'] in {'queued','running'}:
            with self.tx() as cursor:
                self.questions.close(cursor,self.token,self.question['question_id'],reason='cancelled')
        self._qseq += 1
        with self.tx() as cursor:
            submitted,_ = self.questions.submit(cursor,self.token,self.binding,self.edge['link_id'],f'TEST_PRIVATE_QUESTION_{self._qseq}')
        job = self.jobs.claim_due('TEST_NOTICE_WORKER',kinds=(account_job_kind('question',self.account.key),),now=self.now,limit=1)[0]
        with self.tx() as cursor:
            row,claimed = self.questions.claim(cursor,self.token,job)
        self.assertTrue(claimed)
        self.question,self.notice_job = row,job
        return row,job

    def queue(self,text='TEST_TARGET_PUBLIC_NOTICE'):
        with self.tx() as cursor:source,notice,ref = self.notices.prepare(cursor,self.token,self.notice_job,text)
        self.notice_id = source['notice_id']; self.ref = ref
        return ref.delivery_ids[0]

    def row(self):return self.query('SELECT * FROM qq_official_question_notices WHERE notice_id=?',(self.notice_id,))[0]

    async def test_confirmed_proactive_notice_is_one_ephemeral_target_bot_effect_without_durable_plaintext(self):
        delivery = self.queue('TEST_CONFIRMED_PUBLIC_NOTICE')
        self.assertEqual((self.row()['state'],self.row()['delivery_state']),('sending','pending'))
        outcome = await self.sender.send_delivery(delivery)
        self.assertEqual(outcome.status,'committed')
        row = self.row()
        self.assertEqual((row['state'],row['delivery_state']),('committed','committed'))
        self.assertTrue(row['receipt_id']); self.assertTrue(row['delivery_receipt_hash']); self.assertIsNotNone(row['delivery_received_at'])
        self.assertEqual(len(self.fake.received),1)
        sent = self.fake.received[0]
        self.assertEqual((sent['kind'],sent['target'],sent['payload']['content']),('group',self.target.native_conversation_id,'TEST_CONFIRMED_PUBLIC_NOTICE'))
        self.assertNotIn('msg_id',sent['payload']); self.assertNotIn('message_reference',sent['payload'])
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed'); self.assertEqual(len(self.fake.received),1)
        for table in ('delivery_plans','deliveries','delivery_attempts','messages','qq_official_question_notice_sources','qq_official_question_notices'):
            self.assertNotIn('TEST_CONFIRMED_PUBLIC_NOTICE',repr([dict(r) for r in self.query('SELECT * FROM '+table)]))
        self.assertIsNone(self.query('SELECT wire_payload_json FROM delivery_attempts')[0]['wire_payload_json'])
        with self.assertRaisesRegex(PermissionError,'question_notice_already_used'),self.tx() as cursor:
            self.notices.prepare(cursor,self.token,self.notice_job,'TEST_SECOND_NOTICE')

    async def test_proactive_unknown_or_denied_never_probes_or_borrows_passive_inbound_window(self):
        with self.tx() as cursor:
            cursor.execute('''INSERT INTO qq_official_inbound_events
                (inbox_id,account_key,endpoint_key,endpoint_generation,route_epoch,event_type,native_message_id,
                 status,echo_state,trigger_requested,received_at,created_at,updated_at)
                VALUES (?,?,?,1,1,'GROUP_AT_MESSAGE_CREATE','TEST_TARGET_HUMAN_MSG','processed','not_echo',FALSE,?,?,?)''',
                ('TEST_TARGET_HUMAN_INBOX',self.account.key,self.target.key,self.now,self.now,self.now))
        self.fake.add_window('group',self.target.native_conversation_id,'TEST_TARGET_HUMAN_MSG')
        for state in ('unknown','denied'):
            self.query('UPDATE qq_official_endpoints SET proactive_state=? WHERE endpoint_key=?',(state,self.target.key))
            with self.assertRaisesRegex(PermissionError,'question_notice_proactive_unavailable'),self.tx() as cursor:
                self.notices.prepare(cursor,self.token,self.notice_job,'TEST_BLOCKED_'+state)
        self.query("UPDATE qq_official_endpoints SET proactive_state='allowed' WHERE endpoint_key=?",(self.target.key,))
        for state in ('unknown','denied'):
            delivery = self.queue('TEST_REVOKED_PROACTIVE_'+state)
            self.query('UPDATE qq_official_endpoints SET proactive_state=? WHERE endpoint_key=?',(state,self.target.key))
            outcome = await self.sender.send_delivery(delivery)
            self.assertEqual((outcome.status,outcome.reason),('failed','proactive_disabled_no_reply_window'))
            self.assertEqual((self.row()['state'],self.row()['delivery_state']),('failed','failed'))
            self.assertEqual(self.fake.received,[])
            self.query("UPDATE qq_official_endpoints SET proactive_state='allowed' WHERE endpoint_key=?",(self.target.key,))
            self.new_running_question()

    async def test_known_auth_rate_and_proactive_rejections_get_one_attempt_and_no_retry(self):
        for idx,err in enumerate(((401,50002),(429,50002),(400,40034105))):
            if idx:
                self.query("UPDATE qq_official_endpoints SET proactive_state='allowed' WHERE endpoint_key=?",(self.target.key,))
                self.new_running_question()
            delivery = self.queue('TEST_ERR_'+str(idx))
            self.fake.next_errors.append(err)
            outcome = await self.sender.send_delivery(delivery)
            self.assertEqual(outcome.status,'failed')
            self.assertEqual((self.row()['state'],self.row()['delivery_state']),('failed','failed'))
            self.assertEqual(len(self.query('SELECT * FROM delivery_attempts WHERE delivery_id=?',(delivery,))),1)
            if err[1]==40034105:
                self.assertEqual(self.query('SELECT proactive_state FROM qq_official_endpoints WHERE endpoint_key=?',(self.target.key,))[0]['proactive_state'],'denied')

    async def test_missing_body_expired_unknown_and_lower_level_retry_never_resend(self):
        delivery = self.queue('TEST_LOST_EPHEMERAL')
        self.outbox.forget_ephemeral(delivery)
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'failed')
        self.assertEqual((self.row()['state'],self.row()['delivery_state']),('failed','failed'))
        self.assertEqual(self.fake.received,[])

        self.new_running_question(); delivery = self.queue('TEST_EXPIRED_NOTICE')
        self.now += 31
        self.assertNotIn(delivery,self.outbox.list_due(self.token))
        self.assertEqual((self.row()['state'],self.row()['delivery_state']),('expired','failed'))
        self.assertEqual(self.fake.received,[])

        self.new_running_question(); delivery = self.queue('TEST_UNKNOWN_NOTICE')
        self.fake.drop_next_response = True
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'ambiguous')
        self.assertEqual((self.row()['state'],self.row()['delivery_state']),('unknown','unknown'))
        await self.sender.stop(); self.sender = self.make_sender()
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'ambiguous')
        self.assertEqual(len(self.fake.received),1)

        self.new_running_question(); delivery = self.queue('TEST_NO_RETRY_PLAIN')
        self.assertEqual(self.outbox.fail(self.token,delivery,'authentication',retry=True,plain=True).status,'failed')
        self.assertEqual((self.row()['state'],self.row()['delivery_state']),('failed','failed'))
        self.assertEqual(self.outbox.get(self.token,delivery)['variant'],'primary')

    async def test_source_stop_bot_identity_change_or_question_close_blocks_http(self):
        delivery = self.queue('TEST_STOPPED_TARGET')
        self.query('INSERT INTO conversation_settings(scope_key,stopped,stop_generation) VALUES (?,TRUE,1)',(self.target.scope.key,))
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'cancelled')
        self.assertEqual((self.row()['state'],self.row()['delivery_state']),('cancelled','cancelled'))
        self.assertEqual(self.fake.received,[])

        self.query('DELETE FROM conversation_settings WHERE scope_key=?',(self.target.scope.key,))
        self.new_running_question(); delivery = self.queue('TEST_IDENTITY_CHANGED')
        with self.tx() as cursor:observe_ready(cursor,self.token,{'d':{'user':{'id':'TEST_OTHER_BOT','bot':True}}},self.now+1)
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'failed')
        self.assertEqual((self.row()['state'],self.row()['delivery_state']),('failed','failed'))
        self.assertEqual(self.fake.received,[])

        self.new_running_question(); delivery = self.queue('TEST_QUESTION_CLOSED')
        with self.tx() as cursor:self.questions.close(cursor,self.token,self.question['question_id'],reason='cancelled')
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'failed')
        self.assertEqual((self.row()['state'],self.row()['delivery_state']),('failed','failed'))
        self.assertEqual(self.fake.received,[])

        self.new_running_question(); delivery = self.queue('TEST_POST_RESERVATION_CLOSED')
        original_prepare = self.outbox.prepare
        def prepare_and_close(*args,**kwargs):
            reservation = original_prepare(*args,**kwargs)
            with self.tx() as cursor:self.questions.close(cursor,self.token,self.question['question_id'],reason='cancelled')
            return reservation
        with patch.object(self.outbox,'prepare',side_effect=prepare_and_close):
            outcome = await self.sender.send_delivery(delivery)
        self.assertEqual((outcome.status,outcome.reason),('failed','question_notice_source_unavailable'))
        self.assertEqual((self.row()['state'],self.row()['delivery_state']),('failed','failed'))
        self.assertEqual(self.fake.received,[])

    async def test_restart_with_captured_but_unapplied_receipt_keeps_fact_not_reusable_notice(self):
        delivery = self.queue('TEST_RESTART_UNAPPLIED')
        await self.sender.recover()
        reservation = self.outbox.prepare(self.token,delivery,send_mode='proactive')
        response = await self.rest.send_message(self.target.kind,self.target.native_conversation_id,reservation.payload())
        receipt = self.outbox.capture_receipt(reservation,response)
        self.assertEqual((self.row()['state'],self.row()['delivery_state']),('sending','pending'))
        with self.tx() as cursor:self.notices.recover(cursor,self.token)
        self.assertEqual((self.row()['state'],self.row()['delivery_state']),('unknown','unknown'))
        self.assertIn(receipt,self.outbox.recover(self.token))
        self.assertEqual(self.outbox.finalize(self.token,receipt,ledger=self.ledger).status,'committed')
        row = self.row()
        self.assertEqual((row['state'],row['delivery_state']),('unknown','committed'))
        self.assertTrue(row['delivery_receipt_hash']); self.assertEqual(len(self.fake.received),1)

    async def test_accepted_receipt_after_cancellation_remains_true_and_repeated_cancel_drains(self):
        delivery = self.queue('TEST_LATE_AFTER_CANCEL')
        original = self.rest.send_message
        async def send(*args,**kwargs):
            response = await original(*args,**kwargs)
            with self.tx() as cursor:self.notices.close(cursor,self.token,self.notice_id,reason='cancelled')
            return response
        with patch.object(self.rest,'send_message',side_effect=send):
            self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
        self.assertEqual((self.row()['state'],self.row()['delivery_state']),('cancelled','committed'))
        self.assertTrue(self.row()['delivery_receipt_hash'])

        self.new_running_question(); delivery = self.queue('TEST_REPEATED_CANCEL')
        entered,release = threading.Event(),threading.Event()
        original_capture = self.outbox.capture_receipt
        def capture(reservation,body):
            entered.set()
            if not release.wait(5):raise RuntimeError('TEST receipt barrier timed out')
            return original_capture(reservation,body)
        with patch.object(self.outbox,'capture_receipt',side_effect=capture):
            task = asyncio.create_task(self.sender.send_delivery(delivery))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait,3))
                worker = self.sender._workers[self.target.key]
                worker.cancel(); await asyncio.sleep(0)
                worker.cancel(); await asyncio.sleep(0)
                release.set()
                await asyncio.gather(worker,task,return_exceptions=True)
            finally:
                release.set()
                await self.sender.stop()
                await asyncio.gather(task,return_exceptions=True)
        self.assertEqual((self.row()['state'],self.row()['delivery_state']),('committed','committed'))
        self.assertTrue(self.row()['delivery_receipt_hash'])

    async def test_tampered_content_non_bot_actor_and_daily_proactive_limit_rejected(self):
        delivery = self.queue('TEST_TAMPERED_RAM')
        self.outbox._ephemeral[delivery] = render_notice(self.target,'TEST_TAMPERED_BODY')
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'failed')
        self.assertEqual(self.fake.received,[])

        self.new_running_question()
        with self.tx() as cursor:source,notice,rendered = self.notices.propose(cursor,self.token,self.notice_job,'TEST_DIRECT_ENQUEUE')
        with self.assertRaises(OutboxInvariantError),self.tx() as cursor:
            self.outbox.enqueue(self.token,rendered=rendered,requester=ActorKey(self.account,'group_member',self.target.native_conversation_id,'TEST_HUMAN'),
                logical_key='question-notice:'+source['notice_id'],source_scope_key=self.target.scope.key,purpose='question_notice',
                question_notice_id=source['notice_id'],expires_at=source['expires_at'],source_native_message_id='',transaction_cursor=cursor)
        with self.assertRaises(OutboxInvariantError),self.tx() as cursor:
            self.outbox.enqueue(self.token,rendered=rendered,requester=ActorKey.parse(notice['bot_actor_key']),
                logical_key='question-notice:'+source['notice_id'],source_scope_key=self.target.scope.key,purpose='question_notice',
                question_notice_id=source['notice_id'],expires_at=source['expires_at'],source_native_message_id='TEST_FAKE_REPLY',transaction_cursor=cursor)

        with patch('src.plugins.ai_chat.adapters.qq_official.send_store.GROUP_PROACTIVE_DAILY_LIMIT',2):
            for _ in range(2):
                self.new_running_question(); delivery = self.queue('TEST_QUOTA_CONSUME')
                self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
            self.new_running_question(); delivery = self.queue('TEST_QUOTA_EXCEEDED')
            outcome = await self.sender.send_delivery(delivery)
            self.assertEqual((outcome.status,outcome.reason),('failed','group_proactive_daily_limit'))
            self.assertEqual((self.row()['state'],self.row()['delivery_state']),('failed','failed'))
            self.assertEqual(len(self.fake.received),2)
