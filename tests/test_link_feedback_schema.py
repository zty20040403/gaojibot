"""Feedback plan constraints, not actual QQ delivery or sender verification."""
import secrets
import psycopg

from src.plugins.ai_chat.adapters.qq_official.plans import enqueue_plan
from src.plugins.ai_chat.adapters.qq_official.renderer import render_plan
from src.plugins.ai_chat.message_ir import MessageBody,TextNode
from src.plugins.ai_chat.platform_types import ActorKey
from tests import test_link_replies as fixture


class LinkFeedbackSchemaTests(fixture.LinkReplyTests):
    def plan(self,source,**changes):
        actor = ActorKey.parse(source['actor_key'])
        with self.tx() as cursor:
            ref = enqueue_plan(cursor,token=self.token,rendered=render_plan(MessageBody((TextNode(0,'TEST_FEEDBACK_SCHEMA'),)),self.group),
                requester=actor,logical_key='TEST_FEEDBACK_BASE_'+secrets.token_hex(4),source_scope_key=source['scope_key'],
                expires_at=self.now+30,now=self.now,admission=lambda *args:True)
        row = self.query('SELECT * FROM delivery_plans WHERE plan_id=?',(ref.plan_id,))[0]
        row.update(plan_id=secrets.token_hex(16),idempotency_key='qqp1:'+secrets.token_hex(32),purpose='link_feedback',payload_mode='ephemeral',
            digest_scheme='hmac_sha256_v1',digest_key_id='v1',manifest_json='{"v":1,"ephemeral":true}',manifest_digest='a'*64,
            link_reply_inbox_id=source['source_inbox_id'])
        row.update(changes)
        self.query('INSERT INTO delivery_plans('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))
        return row['plan_id']

    def source(self):return self.reply(self.accept(self.link_frame('/link list')))

    def test_one_plan_per_original_outcome_and_no_rebinding_or_deletion(self):
        source = self.source(); plan = self.plan(source)
        with self.assertRaises(psycopg.errors.UniqueViolation):self.plan(source)
        for field,value in (('link_reply_inbox_id',None),('source_scope_key','TEST_OTHER_SCOPE'),('expires_at',self.now+29)):
            with self.subTest(field=field),self.assertRaisesRegex(psycopg.Error,'immutable_link_feedback_plan'):
                self.query(f'UPDATE delivery_plans SET {field}=? WHERE plan_id=?',(value,plan))
        with self.assertRaisesRegex(psycopg.Error,'immutable_link_feedback_plan'):
            self.query('DELETE FROM delivery_plans WHERE plan_id=?',(plan,))

    def test_fixed_original_actor_and_physical_account_are_composite_foreign_keys(self):
        source = self.source()
        other = ActorKey(self.account,'group_member',self.group.native_conversation_id,'TEST_OTHER_FEEDBACK_ACTOR')
        with self.assertRaisesRegex(psycopg.errors.ForeignKeyViolation,'link_feedback_original_source'):
            self.plan(source,requester_actor_key=other.key)

    def test_feedback_cannot_use_persistent_body_or_smuggle_other_kind(self):
        for changes in (dict(payload_mode='persistent'),dict(link_reply_inbox_id=None),dict(part_count=2),
            dict(purpose='final',payload_mode='persistent',digest_scheme='sha256',digest_key_id=None)):
            with self.subTest(changes=changes),self.assertRaises(psycopg.Error):self.plan(self.source(),**changes)

    def test_original_time_scope_and_short_expiry_are_mandatory_before_claim(self):
        for changes in (dict(expires_at=self.now+31),dict(created_at=self.now-1),dict(created_at=self.now+600),
            dict(source_scope_key='TEST_OTHER_SCOPE'),dict(endpoint_generation=2),dict(route_epoch=2),
            dict(source_canonical_message_id=7),dict(turn_id=7)):
            with self.subTest(changes=changes),self.assertRaisesRegex(psycopg.Error,'link_feedback_original_source_required'):
                self.plan(self.source(),**changes)


for name in dir(fixture.LinkReplyTests):
    if name.startswith('test_') and name not in LinkFeedbackSchemaTests.__dict__:setattr(LinkFeedbackSchemaTests,name,None)
