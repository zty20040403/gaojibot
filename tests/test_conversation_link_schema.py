"""Actual bilateral-proof constraints, not QQ authorization/receipt doubles."""
import secrets
import psycopg

from src.bot_storage import DatabaseError
from src.plugins.ai_chat.platform_types import AccountKey,ActorKey,EndpointKey
from tests.qq_official_outbox_fixture import OutboxFixture


class ConversationLinkSchemaTests(OutboxFixture):
    def setUp(self):
        super().setUp()
        self.target = EndpointKey(self.account,'group','TEST_LINK_TARGET'); self.provision(self.target)

    def query(self,sql,args=()):
        try:return super().query(sql,args)
        except DatabaseError as error:
            # Assert the actual PostgreSQL constraint, not its sanitized wrapper.
            if not isinstance(error.__cause__,psycopg.Error):raise
            raise error.__cause__ from error

    def insert(self,table,row):
        self.query(f"INSERT INTO {table}({','.join(row)}) VALUES ({','.join('?' for _ in row)})",tuple(row.values()))

    def challenge(self,**changes):
        row = dict(challenge_id='LC-'+secrets.token_hex(16),account_key=self.account.key,account_epoch=self.token.epoch,
            source_endpoint_key=self.endpoint.key,source_actor_key=self.actor.key,source_scope_key=self.endpoint.scope.key,
            source_generation=1,source_route_epoch=1,source_receipt_hash=secrets.token_hex(32),source_authority_hash=secrets.token_hex(32),
            code_hmac=secrets.token_hex(32),created_at=self.now,expires_at=self.now+600)
        row.update(changes); self.insert('qq_official_link_challenges',row)
        return row

    def confirmation(self):
        return dict(target_endpoint_key=self.target.key,target_actor_key=ActorKey(self.account,'group_member','TEST_LINK_TARGET','TEST_APPROVER').key,
            target_scope_key=self.target.scope.key,target_generation=1,target_route_epoch=1,
            target_receipt_hash=secrets.token_hex(32),target_authority_hash=secrets.token_hex(32),target_confirmed_at=self.now+1)

    def active(self):
        return self.challenge(**self.confirmation(),state='active',delivery_state='committed',code_hmac='',activated_at=self.now+2)

    def link(self,challenge,**changes):
        names = ('challenge_id','account_key','source_endpoint_key','target_endpoint_key','source_scope_key','target_scope_key',
            'source_generation','source_route_epoch','target_generation','target_route_epoch')
        row = {name:challenge[name] for name in names}
        row.update(link_id=secrets.token_hex(16),created_at=challenge.get('activated_at',self.now+2)); row.update(changes)
        self.insert('qq_official_conversation_links',row)
        return row

    def test_single_direction_requires_both_proofs_and_committed_delivery(self):
        pending = self.challenge(**self.confirmation(),state='confirming')
        with self.assertRaisesRegex(psycopg.Error,'link_requires_bilateral_committed_proof'):self.link(pending)
        with self.assertRaises(psycopg.Error):
            self.query("UPDATE qq_official_link_challenges SET state='active',code_hmac='',activated_at=? WHERE challenge_id=?",
                (self.now+2,pending['challenge_id']))
        self.query("UPDATE qq_official_link_challenges SET state='active',delivery_state='committed',code_hmac='',activated_at=? WHERE challenge_id=?",
            (self.now+2,pending['challenge_id']))
        pending['activated_at'] = self.now+2; link = self.link(pending)
        self.assertEqual(self.query('SELECT source_endpoint_key,target_endpoint_key FROM qq_official_conversation_links'),
            [dict(source_endpoint_key=self.endpoint.key,target_endpoint_key=self.target.key)])
        with self.assertRaisesRegex(psycopg.Error,'link_requires_bilateral_committed_proof'):
            self.link(pending,source_endpoint_key=self.target.key,target_endpoint_key=self.endpoint.key)
        with self.assertRaises(psycopg.Error):self.link(pending)
        self.assertEqual(self.query('SELECT link_id FROM qq_official_conversation_links')[0]['link_id'],link['link_id'])

    def test_cross_account_self_and_partial_target_proofs_are_rejected(self):
        other = EndpointKey(AccountKey('qq-official','TEST_OTHER_LINK_ACCOUNT','test'),'group','TEST_LINK_OTHER')
        self.provision(other)
        for target in (other,self.endpoint):
            with self.subTest(target=target.kind),self.assertRaises(psycopg.Error):
                self.challenge(**dict(self.confirmation(),target_endpoint_key=target.key),state='confirming')
        with self.assertRaises(psycopg.Error):self.challenge(target_endpoint_key=self.target.key,state='confirming')
        with self.assertRaises(psycopg.Error):
            self.challenge(**dict(self.confirmation(),target_confirmed_at=None),state='confirming')

    def test_source_authority_route_epoch_and_receipt_never_mutate(self):
        challenge = self.challenge()
        changes = dict(account_epoch=self.token.epoch+1,source_generation=2,source_route_epoch=2,
            source_scope_key=self.target.scope.key,source_actor_key='TEST_FAKE_ACTOR',source_receipt_hash='b'*64,
            source_authority_hash='c'*64,expires_at=self.now+599,code_hmac='d'*64)
        for field,value in changes.items():
            with self.subTest(field=field),self.assertRaisesRegex(psycopg.Error,'immutable_link_challenge'):
                self.query(f'UPDATE qq_official_link_challenges SET {field}=? WHERE challenge_id=?',(value,challenge['challenge_id']))
        self.assertEqual(self.query('SELECT source_scope_key FROM qq_official_link_challenges')[0]['source_scope_key'],self.endpoint.scope.key)

    def test_confirmed_target_and_terminal_delivery_cannot_be_replaced(self):
        challenge = self.challenge(**self.confirmation(),state='confirming')
        with self.assertRaisesRegex(psycopg.Error,'immutable_link_challenge'):
            self.query('UPDATE qq_official_link_challenges SET target_authority_hash=? WHERE challenge_id=?',('e'*64,challenge['challenge_id']))
        self.query("UPDATE qq_official_link_challenges SET state='cancelled',delivery_state='unknown',code_hmac='' WHERE challenge_id=?",
            (challenge['challenge_id'],))
        with self.assertRaisesRegex(psycopg.Error,'immutable_link_challenge'):
            self.query("UPDATE qq_official_link_challenges SET state='confirming',delivery_state='committed',code_hmac=? WHERE challenge_id=?",
                ('f'*64,challenge['challenge_id']))

    def test_expiry_missing_receipt_and_plaintext_code_do_not_pass_constraints(self):
        for changes in (dict(expires_at=self.now),dict(expires_at=self.now+601),dict(code_hmac='123456'),dict(source_receipt_hash='TEST_FAKE')):
            with self.subTest(changes=changes),self.assertRaises(psycopg.Error):self.challenge(**changes)
        with self.assertRaises(psycopg.Error):
            self.challenge(**self.confirmation(),state='active',delivery_state='committed',code_hmac='',activated_at=self.now+600)

    def test_link_revocation_requires_new_generation_and_never_reactivates_old_edge(self):
        challenge = self.active(); link = self.link(challenge)
        with self.assertRaisesRegex(psycopg.Error,'immutable_conversation_link'):
            self.query("UPDATE qq_official_conversation_links SET state='revoked',revoked_at=? WHERE link_id=?",(self.now+3,link['link_id']))
        self.query("UPDATE qq_official_conversation_links SET state='revoked',generation=generation+1,revoked_at=? WHERE link_id=?",
            (self.now+3,link['link_id']))
        with self.assertRaisesRegex(psycopg.Error,'immutable_conversation_link'):
            self.query("UPDATE qq_official_conversation_links SET state='active',revoked_at=NULL WHERE link_id=?",(link['link_id'],))
        with self.assertRaisesRegex(psycopg.Error,'immutable_conversation_link'):
            self.query('UPDATE qq_official_conversation_links SET target_scope_key=? WHERE link_id=?',(self.endpoint.scope.key,link['link_id']))
        with self.assertRaisesRegex(psycopg.Error,'immutable_link_challenge'):
            self.query("UPDATE qq_official_link_challenges SET state='issued',activated_at=NULL,code_hmac=? WHERE challenge_id=?",
                ('f'*64,challenge['challenge_id']))
        self.assertEqual(self.query('SELECT state,generation FROM qq_official_conversation_links'),[dict(state='revoked',generation=2)])
