"""Only storage proof shape; fake metadata is not authenticated QQ issuance."""
import secrets
import psycopg

from src.plugins.ai_chat.adapters.qq_official.binding import BindingReducer
from src.plugins.ai_chat.adapters.qq_official.inbox import OfficialInboxStore
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.adapters.qq_official.plans import enqueue_plan
from src.plugins.ai_chat.adapters.qq_official.renderer import render_plan
from src.plugins.ai_chat.message_ir import MessageBody,TextNode
from tests import test_conversation_link_schema as fixture


class LinkNoticeSchemaTests(fixture.ConversationLinkSchemaTests):
    def setUp(self):
        super().setUp()
        self.store = OfficialInboxStore(self.database,jobs=None,binding=BindingReducer(b'TEST_LINK_NOTICE_SOURCE_PEPPER_12345',epoch_guard=assert_epoch),clock=lambda:self.now)
        self.addCleanup(self.store.close)

    def ingress(self,challenge,**changes):
        with self.tx() as cursor:
            endpoint = cursor.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?',(self.endpoint.key,)).fetchone()
            inbox = self.store._insert(cursor,self.account,'GROUP_AT_MESSAGE_CREATE',self.now,endpoint=self.endpoint,endpoint_row=endpoint,
                native_id='TEST_ORIGINAL_'+secrets.token_hex(4),status='processed',reason='link_control',echo='not_echo')
        values = dict(link_control_actor_key=self.actor.key,link_control_receipt_hash=challenge['source_receipt_hash'],link_control_platform_at=self.now)|changes
        self.query('UPDATE qq_official_inbound_events SET '+','.join(name+'=?' for name in values)+' WHERE inbox_id=?',(*values.values(),inbox))
        return inbox

    def plan(self,challenge,**changes):
        with self.tx() as cursor:
            ref = enqueue_plan(cursor,token=self.token,rendered=render_plan(MessageBody((TextNode(0,'TEST_BASE'),)),self.endpoint),requester=self.actor,
                logical_key='TEST_LINK_BASE_'+secrets.token_hex(4),source_scope_key=self.endpoint.scope.key,expires_at=self.now+30,now=self.now,admission=lambda *args:True)
        row = dict(self.query('SELECT * FROM delivery_plans WHERE plan_id=?',(ref.plan_id,))[0])
        row.update(plan_id=secrets.token_hex(16),idempotency_key='qqp1:'+secrets.token_hex(32),purpose='link_otp',payload_mode='ephemeral',
            digest_scheme='hmac_sha256_v1',digest_key_id='v1',manifest_json='{"v":1,"ephemeral":true}',manifest_digest='a'*64,link_challenge_id=challenge['challenge_id'])
        row.update(changes); self.insert('delivery_plans',row)
        return row['plan_id']

    def test_source_actor_receipt_and_original_frame_are_immutable_and_body_free(self):
        challenge = self.challenge(); inbox = self.ingress(challenge)
        self.query('UPDATE qq_official_link_challenges SET source_inbox_id=? WHERE challenge_id=?',(inbox,challenge['challenge_id']))
        for field,value in (('payload_json','TEST_PLAINTEXT_CODE'),('native_message_id','TEST_NEW_SOURCE'),('received_at',self.now+1),
                ('link_control_receipt_hash','b'*64),('link_control_actor_key','TEST_FAKE_ACTOR')):
            with self.subTest(field=field),self.assertRaises(psycopg.Error):
                self.query(f'UPDATE qq_official_inbound_events SET {field}=? WHERE inbox_id=?',(value,inbox))
        another = self.challenge(); wrong = self.ingress(another,link_control_actor_key='TEST_WRONG_ACTOR')
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            self.query('UPDATE qq_official_link_challenges SET source_inbox_id=? WHERE challenge_id=?',(wrong,another['challenge_id']))
        with self.assertRaisesRegex(psycopg.Error,'immutable_link_notice_source'):
            self.query('UPDATE qq_official_link_challenges SET source_inbox_id=NULL WHERE challenge_id=?',(challenge['challenge_id'],))

    def test_only_one_ephemeral_original_endpoint_plan_and_no_late_rebinding(self):
        challenge = self.challenge(); inbox = self.ingress(challenge); plan = self.plan(challenge)
        with self.assertRaises(psycopg.Error):
            self.query('UPDATE qq_official_link_challenges SET notice_plan_id=? WHERE challenge_id=?',(plan,challenge['challenge_id']))
        self.query('UPDATE qq_official_link_challenges SET source_inbox_id=?,notice_plan_id=? WHERE challenge_id=?',(inbox,plan,challenge['challenge_id']))
        with self.assertRaises(psycopg.errors.UniqueViolation):self.plan(challenge)
        with self.assertRaisesRegex(psycopg.Error,'immutable_link_notice_source'):
            self.query('UPDATE qq_official_link_challenges SET notice_plan_id=NULL WHERE challenge_id=?',(challenge['challenge_id'],))
        other = self.challenge(); origin = self.ingress(other); wrong = self.plan(other,endpoint_key=self.target.key)
        self.query('UPDATE qq_official_link_challenges SET source_inbox_id=? WHERE challenge_id=?',(origin,other['challenge_id']))
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            self.query('UPDATE qq_official_link_challenges SET notice_plan_id=? WHERE challenge_id=?',(wrong,other['challenge_id']))
        self.query("UPDATE qq_official_link_challenges SET state='cancelled',code_hmac='' WHERE challenge_id=?",(other['challenge_id'],))
        with self.assertRaisesRegex(psycopg.Error,'immutable_link_notice_source'):
            self.query('UPDATE qq_official_link_challenges SET notice_plan_id=? WHERE challenge_id=?',(wrong,other['challenge_id']))

    def test_other_purposes_or_payload_modes_cannot_smuggle_link_challenge(self):
        cases = ((dict(purpose='final',payload_mode='persistent',digest_scheme='sha256',digest_key_id=None),'link_notice_kind'),
            (dict(payload_mode='persistent'),'delivery_plans_check2'),(dict(link_challenge_id=None),'delivery_plans_check2'),
            (dict(ops_action_id='TEST_OPS_ID'),'delivery_plans_check2'))
        for changes,constraint in cases:
            with self.subTest(changes=changes),self.assertRaisesRegex(psycopg.Error,constraint):self.plan(self.challenge(),**changes)


for name in dir(fixture.ConversationLinkSchemaTests):
    if name.startswith('test_') and name not in LinkNoticeSchemaTests.__dict__:setattr(LinkNoticeSchemaTests,name,None)
