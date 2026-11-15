import psycopg

from tests import test_conversation_link_schema as fixture


class LinkReceiptSchemaTests(fixture.ConversationLinkSchemaTests):
    def test_cancel_expiry_and_unknown_keep_late_acceptance_but_never_activate(self):
        for state,delivery in (('cancelled','pending'),('expired','pending'),('cancelled','unknown')):
            challenge = self.challenge(**self.confirmation(),state=state,delivery_state=delivery,code_hmac='')
            with self.subTest(state=state,delivery=delivery):
                self.query('''UPDATE qq_official_link_challenges SET delivery_state='committed',
                    delivery_receipt_hash=?,delivery_received_at=? WHERE challenge_id=?''',
                    (self.receipt()['delivery_receipt_hash'],self.now+700,challenge['challenge_id']))
                row = self.query('SELECT state,code_hmac,delivery_state,delivery_received_at FROM qq_official_link_challenges WHERE challenge_id=?',
                    (challenge['challenge_id'],))[0]
                self.assertEqual(row,dict(state=state,code_hmac='',delivery_state='committed',delivery_received_at=self.now+700))
                with self.assertRaisesRegex(psycopg.Error,'link_requires_bilateral_committed_proof'):self.link(challenge)
                with self.assertRaisesRegex(psycopg.Error,'immutable_link_challenge'):
                    self.query("UPDATE qq_official_link_challenges SET state='active',activated_at=? WHERE challenge_id=?",
                        (self.now+2,challenge['challenge_id']))
        self.assertEqual(self.query('SELECT count(*) AS n FROM qq_official_conversation_links')[0]['n'],0)

    def test_late_receipt_cannot_add_target_change_authority_or_replace_first_receipt(self):
        challenge = self.challenge(state='cancelled',code_hmac='')
        confirmation = self.confirmation()
        with self.assertRaisesRegex(psycopg.Error,'immutable_link_challenge'):
            self.query('UPDATE qq_official_link_challenges SET '+','.join(name+'=?' for name in confirmation)+
                ",delivery_state='committed',delivery_receipt_hash=?,delivery_received_at=? WHERE challenge_id=?",
                (*confirmation.values(),'a'*64,self.now+2,challenge['challenge_id']))
        self.query("UPDATE qq_official_link_challenges SET delivery_state='committed',delivery_receipt_hash=?,delivery_received_at=? WHERE challenge_id=?",
            ('a'*64,self.now+2,challenge['challenge_id']))
        for field,value in (('delivery_receipt_hash','b'*64),('delivery_received_at',self.now+3),('source_authority_hash','c'*64)):
            with self.subTest(field=field),self.assertRaisesRegex(psycopg.Error,'immutable_link_challenge'):
                self.query(f'UPDATE qq_official_link_challenges SET {field}=? WHERE challenge_id=?',(value,challenge['challenge_id']))

    def test_committed_flag_without_paired_hash_cannot_activate_and_bad_receipts_are_denied(self):
        challenge = self.challenge(**self.confirmation(),state='confirming',delivery_state='committed')
        with self.assertRaisesRegex(psycopg.Error,'link_activation_receipt'):
            self.query("UPDATE qq_official_link_challenges SET state='active',code_hmac='',activated_at=? WHERE challenge_id=?",
                (self.now+2,challenge['challenge_id']))
        for changes in (dict(delivery_receipt_hash='a'*64),dict(delivery_received_at=self.now+1),
                dict(delivery_receipt_hash='TEST_BAD',delivery_received_at=self.now+1,delivery_state='committed'),
                dict(delivery_receipt_hash='a'*64,delivery_received_at=self.now-1,delivery_state='committed')):
            with self.subTest(changes=changes),self.assertRaises(psycopg.Error):
                self.challenge(state='issued' if changes.get('delivery_state')=='committed' else 'sending',**changes)
        accepted = self.challenge(state='cancelled',delivery_state='committed',code_hmac='',**self.receipt())
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.challenge(state='cancelled',delivery_state='committed',code_hmac='',delivery_received_at=self.now+2,
                delivery_receipt_hash=accepted['delivery_receipt_hash'])


for name in dir(fixture.ConversationLinkSchemaTests):
    if name.startswith('test_') and name not in LinkReceiptSchemaTests.__dict__:setattr(LinkReceiptSchemaTests,name,None)
