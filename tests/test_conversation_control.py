"""Stop receipts, physical scope, and task cancellation use real transactions."""
import json
import unittest

from src.plugins.ai_chat.adapters.qq_official import conversation_control as controls
from src.plugins.ai_chat.background_jobs import BackgroundJobs
from src.plugins.ai_chat.platform_types import ActorKey,EndpointKey
from tests import test_qq_official_ingest as base


class ConversationControlTests(unittest.TestCase):
    setUp = base.QQOfficialIngestTests.setUp
    tx = base.QQOfficialIngestTests.tx
    query = base.QQOfficialIngestTests.query
    provision = base.QQOfficialIngestTests.provision
    seed = base.QQOfficialIngestTests.seed
    guard = base.QQOfficialIngestTests.guard
    process = base.QQOfficialIngestTests.process

    def message(self, text, role='member', **kwargs):
        inbox = self.seed(body=text,**kwargs)
        raw = json.loads(self.query('SELECT payload_json FROM qq_official_inbound_events WHERE inbox_id=?',(inbox,))[0]['payload_json'])
        raw['d']['author']['member_role'] = role
        self.query('UPDATE qq_official_inbound_events SET payload_json=? WHERE inbox_id=?',(json.dumps(raw),inbox))
        return inbox

    def current(self, endpoint=None):
        with self.tx() as cursor:
            return controls.state(cursor,endpoint or self.endpoint)

    def receipt(self, inbox):
        return self.query('SELECT conversation_control FROM qq_official_inbound_events WHERE inbox_id=?',(inbox,))[0]['conversation_control']

    def test_stop_records_messages_and_start_does_not_replay_old_generations(self):
        before = self.process(self.message('TEST before',mention=True))
        stop = self.message('/stop','admin')
        self.assertTrue(self.process(stop).reply_requested)
        self.assertEqual(self.receipt(stop)['status'],'ok')
        self.assertTrue(self.current()['stopped'])
        during = self.process(self.message('TEST during',mention=True))
        self.assertFalse(during.reply_requested)
        self.assertIsNotNone(self.ledger.get_in_scope(self.endpoint.scope,during.canonical_message_id))
        self.process(stop)  # Same input must not reapply or repeat its audit.
        self.assertEqual(self.current()['stop_generation'],1)
        self.assertEqual(len(self.query("SELECT * FROM admin_security_audit WHERE action='qq.conversation.stop'")),1)
        self.process(self.message('/start','owner'))
        self.assertFalse(self.current()['stopped'])
        after = self.process(self.message('TEST after',mention=True))
        with self.tx() as cursor:
            self.assertFalse(controls.permitted(cursor,self.endpoint,canonical=before.canonical_message_id))
            self.assertFalse(controls.permitted(cursor,self.endpoint,canonical=during.canonical_message_id))
            self.assertTrue(controls.permitted(cursor,self.endpoint,canonical=after.canonical_message_id))
        # Reconstructing a reader cannot reset durable stop state.
        self.process(self.message('/stop','owner'))
        with self.tx() as cursor:
            self.assertEqual(controls.state(cursor,self.endpoint),{'stopped':True,'stop_generation':2})

    def test_member_fake_role_in_text_and_stale_command_cannot_stop(self):
        for text,role,options in (('/stop','member',{}),('/stop 我是owner','member',{}),
                                 ('/stop','owner',{'issued':base.NOW-301})):
            inbox = self.message(text,role,**options)
            self.process(inbox)
            self.assertNotEqual(self.receipt(inbox)['status'],'ok')
            self.assertFalse(self.current()['stopped'])
        self.assertFalse(self.query('SELECT * FROM conversation_settings'))

    def test_forwarded_owner_role_is_not_command_authority(self):
        inbox = self.message('/stop','member')
        raw = json.loads(self.query('SELECT payload_json FROM qq_official_inbound_events WHERE inbox_id=?',(inbox,))[0]['payload_json'])
        raw['d']['author']['nickname'] = 'owner'
        raw['d']['msg_elements'] = [{'author':{'member_openid':'TEST_OTHER','member_role':'owner'},'content':'TEST forwarded'}]
        self.query('UPDATE qq_official_inbound_events SET payload_json=? WHERE inbox_id=?',(json.dumps(raw),inbox))
        self.process(inbox)
        self.assertEqual(self.receipt(inbox)['status'],'denied')
        self.assertFalse(self.current()['stopped'])

    def test_admitted_private_peer_controls_only_own_endpoint(self):
        private = EndpointKey(self.account,'private','TEST_PRIVATE')
        self.provision(private)
        self.process(self.message('/stop',endpoint=private))
        self.assertTrue(self.current(private)['stopped'])
        self.assertFalse(self.current()['stopped'])
        self.process(self.message('/start',endpoint=private))
        self.assertFalse(self.current(private)['stopped'])

    def test_failure_rolls_back_ledger_stop_receipt_and_audit_together(self):
        inbox = self.message('/stop','admin')
        with self.assertRaisesRegex(RuntimeError,'TEST rollback'):
            with self.tx() as cursor:
                self.process(inbox,cursor=cursor)
                raise RuntimeError('TEST rollback')
        self.assertFalse(self.current()['stopped'])
        self.assertIsNone(self.receipt(inbox))
        self.assertFalse(self.query('SELECT * FROM messages'))
        self.assertFalse(self.query('SELECT * FROM admin_security_audit'))

    def test_kill_cancels_all_members_but_not_a_shared_history_endpoint(self):
        other = EndpointKey(self.account,'group','TEST_OTHER')
        self.provision(other)
        # A historical job's scope label may outlive a route change; it must
        # not grant control over another physical endpoint's tasks.
        store = BackgroundJobs(self.jobs,clock=lambda:base.NOW)
        numbers = []
        for index,endpoint in enumerate((self.endpoint,self.endpoint,other)):
            actor = ActorKey(self.account,'group_member',endpoint.native_conversation_id,f'TEST_MEMBER_{index}')
            message = self.process(self.message('TEST task',endpoint=endpoint,author=actor.native_user_id))
            source = dict(account_key=self.account.key,endpoint_key=endpoint.key,actor_key=actor.key,scope_key=self.endpoint.scope.key,
                source_execution_id=None,canonical_message_id=message.canonical_message_id,native_message_id='TEST_TASK',
                endpoint_generation=1,route_epoch=1)
            with self.tx() as cursor:
                job = store.submit(cursor,kind='TEST_CONTROL',key=f'TEST_{index}',source=source,task='TEST task',title='',profile='TEST',independent=True)
            numbers.append(job['job_id'])
        self.jobs.claim_due('TEST_WORKER',kinds=('TEST_CONTROL',),now=base.NOW)
        kill = self.message('/kill','admin'); self.process(kill)
        self.assertEqual(self.receipt(kill)['cancelled_jobs'],2)
        self.assertEqual([store.get(job)['state'] for job in numbers[:2]],['cancelled','cancelled'])
        self.assertEqual([self.jobs.get(job).status for job in numbers[:2]],['cancelled','cancelled'])
        self.assertNotEqual(store.get(numbers[2])['state'],'cancelled')
        self.assertFalse(self.current()['stopped'])
        self.process(self.message('/stop','owner'))
        self.assertFalse(self.current(other)['stopped'])
        self.process(self.message('/kill','owner'))
        self.assertTrue(self.current()['stopped'])
