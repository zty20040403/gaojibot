"""Real authenticated-store TxA boundaries; no QQ HTTP, ledger send or model."""
from datetime import datetime,timezone
from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official.link_authority import LinkAuthority
from src.plugins.ai_chat.adapters.qq_official.link_ingress import LinkIngress
from src.plugins.ai_chat.platform_types import EndpointKey
from tests import test_qq_official_inbox as fixture


class LinkIngressTests(fixture.OfficialInboxTests):
    def setUp(self):
        super().setUp(); self.activate_group()
        self.links = LinkIngress(LinkAuthority(self.binding._pepper,clock=lambda:self.now,authority=self.authority))
        self.store.link_ingress = self.links

    def link_frame(self,text='/link',*,group='TEST_GROUP',role='admin',**options):
        return self.frame(content=text,group=group,author={'member_openid':'TEST_MEMBER','member_role':role},
            timestamp=datetime.fromtimestamp(self.now,timezone.utc).isoformat(),**options)

    def issue(self):
        with patch('src.plugins.ai_chat.adapters.qq_official.linking.secrets.randbelow',return_value=723891):return self.accept(self.link_frame())

    def challenge(self,accepted):
        return self.query('SELECT * FROM qq_official_link_challenges WHERE challenge_id=?',(accepted._link_input.challenge_id,))[0]

    def test_body_free_actual_gateway_capture_pins_original_not_later_reply_source(self):
        source = self.issue(); row = self.challenge(source)
        self.assertEqual((source.disposition,source._link_input.status),('control','sending'))
        self.assertEqual(row['source_inbox_id'],source.inbox_id)
        self.assertNotIn('723891',repr(source)+self.persisted_text()+str(row))
        self.assertEqual(self.count('durable_jobs'),0); self.assertEqual(self.count('messages'),0)
        self.now += 1; newer = self.accept(self.link_frame('TEST_LATER_ADMIN_MESSAGE'))
        with self.tx() as cursor:original = self.links.source(cursor,self.token,row)
        self.assertEqual(original['inbox_id'],source.inbox_id); self.assertNotEqual(original['inbox_id'],newer.inbox_id)
        proof = self.query('SELECT disposition FROM qq_official_gateway_receipts WHERE inbox_id=?',(source.inbox_id,))[0]
        self.assertEqual(proof['disposition'],'control')

    def test_duplicate_authenticated_source_and_fast_target_confirm_do_not_emit_again(self):
        frame = self.link_frame()
        with patch('src.plugins.ai_chat.adapters.qq_official.linking.secrets.randbelow',return_value=723891):source = self.accept(frame)
        frame['s'] += 1; duplicate = self.accept(frame)
        self.assertIsNone(duplicate._link_input)
        target = EndpointKey(self.account,'group','TEST_TARGET_GROUP'); self.activate_group(target)
        confirmation = self.accept(self.link_frame('/link 723891',group=target.native_conversation_id))
        self.assertEqual(confirmation._link_input.status,'confirming')
        row = self.challenge(source)
        self.assertEqual((row['state'],row['delivery_state'],row['target_endpoint_key']),('confirming','pending',target.key))
        self.assertEqual(self.query('SELECT count(*) AS n FROM qq_official_link_challenges')[0]['n'],1)
        self.assertEqual(self.query('SELECT count(*) AS n FROM qq_official_conversation_links')[0]['n'],0)
        self.assertNotIn('723891',self.persisted_text())

    def test_plain_authenticated_role_withdrawal_fences_original_link_without_using_history(self):
        source = self.issue(); self.now += 1
        self.accept(self.link_frame('TEST_WITHDRAW_ROLE',role='member'))
        with self.assertRaisesRegex(PermissionError,'authority_changed'),self.tx() as cursor:self.links.source(cursor,self.token,self.challenge(source))
        target = EndpointKey(self.account,'group','TEST_TARGET_GROUP'); self.activate_group(target)
        self.assertEqual(self.accept(self.link_frame('/link 723891',group=target.native_conversation_id))._link_input.status,'cancelled')
        self.assertEqual(self.challenge(source)['code_hmac'],'')

    def test_original_gateway_receipt_and_final_current_are_both_required_for_issuance_source(self):
        source = self.issue(); row = self.challenge(source)
        with patch.object(self.links.reducer,'current',side_effect=(True,False)):
            with self.assertRaisesRegex(PermissionError,'authority_changed'),self.tx() as cursor:self.links.source(cursor,self.token,row)
        with self.tx() as cursor:cursor.execute('DELETE FROM qq_official_gateway_receipts WHERE inbox_id=?',(source.inbox_id,))
        with self.assertRaisesRegex(PermissionError,'authenticated_receipt_unavailable'),self.tx() as cursor:self.links.source(cursor,self.token,row)

    def test_tx_a_capture_failure_rolls_back_role_challenge_and_checkpoint(self):
        frame = self.link_frame()
        with patch.object(self.links,'capture',side_effect=RuntimeError('TEST_CAPTURE_FAILURE')):
            with self.assertRaisesRegex(RuntimeError,'TEST_CAPTURE_FAILURE'):self.accept(frame)
        self.assertEqual(self.store.checkpoint(self.token).seq,0)
        for table in ('qq_official_link_roles','qq_official_link_challenges'):
            self.assertEqual(self.query(f'SELECT count(*) AS n FROM {table}')[0]['n'],0)
        with patch('src.plugins.ai_chat.adapters.qq_official.linking.secrets.randbelow',return_value=723891):
            self.assertEqual(self.accept(frame)._link_input.status,'sending')

    def test_fake_prefix_private_peer_and_bad_original_time_cannot_issue(self):
        fake = self.accept(self.link_frame('<@TEST_FAKE_BOT> /link 723891'))
        self.assertEqual(fake.disposition,'quarantined'); self.assertIsNone(fake._link_input)
        private = self.accept(self.frame(private=True,actor='TEST_NOT_OWNER',content='/link'))
        self.assertEqual(private._link_input.status,'denied')
        invalid = self.link_frame(); invalid['d']['timestamp'] = None
        denied = self.accept(invalid); self.assertEqual(denied._link_input.status,'denied')
        self.assertEqual(self.query('SELECT count(*) AS n FROM qq_official_link_challenges')[0]['n'],0)
        self.assertNotIn('723891',self.persisted_text())


for name in dir(fixture.OfficialInboxTests):
    if name.startswith('test_') and name not in LinkIngressTests.__dict__:setattr(LinkIngressTests,name,None)
