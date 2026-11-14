from dataclasses import replace
from datetime import datetime,timezone
import secrets
from unittest.mock import patch

from src.bot_storage import DatabaseError
from src.qq_official.events import parse_dispatch
from src.plugins.ai_chat.platform_types import AccountKey,ActorKey,EndpointKey
from src.plugins.ai_chat.adapters.qq_official.codec import decode_message
from src.plugins.ai_chat.adapters.qq_official.lease import LeaseLost
from src.plugins.ai_chat.adapters.qq_official.link_authority import LinkAuthority
from tests.qq_official_outbox_fixture import OutboxFixture


class LinkAuthorityTests(OutboxFixture):
    def setUp(self):
        super().setUp()
        self.auth = LinkAuthority(b'TEST_LINK_AUTHORITY_PEPPER_123456789',clock=lambda:self.now)

    def message(self,role='admin',*,endpoint=None,stamp=None,**changes):
        endpoint = endpoint or self.endpoint
        author = {'member_openid':self.actor.native_user_id,'member_role':role} if endpoint.kind=='group' else {'user_openid':endpoint.native_conversation_id}
        data = dict(id='TEST_LINK_MESSAGE_'+secrets.token_hex(4),author=author,content='TEST_CODE_BODY_NOT_STORED',
            timestamp=datetime.fromtimestamp(self.now if stamp is None else stamp,timezone.utc).isoformat())
        if endpoint.kind=='group':data['group_openid'] = endpoint.native_conversation_id
        data.update(changes)
        dispatch = parse_dispatch(dict(op=0,t='GROUP_AT_MESSAGE_CREATE' if endpoint.kind=='group' else 'C2C_MESSAGE_CREATE',s=1,id='TEST_LINK_EVENT',d=data))
        return decode_message(dispatch,endpoint.account,self.now)

    def proof(self,message=None,**options):
        message = message or self.message()
        with self.tx() as cursor:
            self.auth.observe(cursor,self.token,message)
            return self.auth.proof(cursor,self.token,message,**options)

    def current(self,proof):
        with self.tx() as cursor:return self.auth.current(cursor,self.token,proof)

    def test_body_free_original_role_and_stable_same_role_observations(self):
        proof = self.proof(); self.assertTrue(self.current(proof))
        self.now += 1; self.proof()
        self.assertTrue(self.current(proof))
        row = self.query('SELECT * FROM qq_official_link_roles')[0]
        self.assertEqual((row['role'],row['revision']),('admin',1))
        self.assertNotIn('TEST_CODE_BODY_NOT_STORED',str(row)+repr(self.auth)+str(proof))
        for field,value in (('authority_hash','a'*64),('receipt_hash','b'*64),('expires_at',proof['expires_at']+1)):
            with self.subTest(field=field):self.assertFalse(self.current(proof|{field:value}))

    def test_demotion_and_regrant_never_restore_original_permission_hash(self):
        proof = self.proof(); self.now += 1
        self.assertIsNone(self.proof(self.message('member'))); self.assertFalse(self.current(proof))
        self.now += 1; current = self.proof()
        self.assertTrue(self.current(current)); self.assertFalse(self.current(proof))
        self.assertEqual(self.query('SELECT revision FROM qq_official_link_roles')[0]['revision'],3)

    def test_late_privileged_and_same_second_conflicting_events_cannot_promote(self):
        proof = self.proof(); self.now += 1; self.proof(self.message('member'))
        self.assertIsNone(self.proof(self.message('admin',stamp=self.now-1)))
        self.assertIsNone(self.proof(self.message('admin')))
        self.assertFalse(self.current(proof))
        self.assertEqual(self.query('SELECT role FROM qq_official_link_roles')[0]['role'],'')
        self.now += 1; self.assertIsNotNone(self.proof())

    def test_forwarded_roles_missing_future_or_expired_time_are_not_permissions(self):
        forwarded = self.message('member',message_type=2,msg_elements=[{'author':{'member_openid':'TEST_OTHER','member_role':'admin'},'content':'TEST_LINK'}])
        self.assertIsNone(self.proof(forwarded))
        self.assertIsNone(self.proof(self.message('admin',message_type=2)))
        for message in (self.message(timestamp=None),self.message(stamp=self.now+1),self.message(stamp=self.now-600)):
            with self.subTest(source=message.timestamp_source):self.assertIsNone(self.proof(message))
        with self.assertRaisesRegex(ValueError,'authenticated_message'):self.proof({'role':'admin'})
        with self.assertRaisesRegex(ValueError,'authenticated_message'):
            self.proof(replace(self.message(),actor=ActorKey(self.account,'bot','','TEST_BOT')))

    def test_endpoint_scope_generation_cross_account_and_epoch_are_fenced(self):
        proof = self.proof()
        with self.assertRaisesRegex(PermissionError,'account_mismatch'):
            self.proof(self.message(endpoint=EndpointKey(AccountKey('qq-official','TEST_OTHER_APP','test'),'group','TEST_GROUP')))
        for column,delta in (('generation',1),('route_epoch',1)):
            self.query(f'UPDATE qq_official_endpoints SET {column}={column}+? WHERE endpoint_key=?',(delta,self.endpoint.key))
            self.assertFalse(self.current(proof)); proof = self.proof()
        self.assertFalse(self.current(proof|{'scope_key':'TEST_NOT_CURRENT_SCOPE'}))
        self.account_lease.close(); self.token = self.lease().acquire()
        self.assertFalse(self.current(proof))
        with self.assertRaises(LeaseLost),self.tx() as cursor:
            self.auth.current(cursor,replace(self.token,epoch=self.token.epoch-1),proof)

    def test_private_peer_has_no_owner_authority_and_regrant_changes_hash(self):
        private = EndpointKey(self.account,'private','TEST_LINK_OWNER'); self.provision(private)
        message = self.message(endpoint=private)
        self.assertIsNone(self.proof(message))
        with self.tx() as cursor:
            self.auth.owners.apply_trusted_config(cursor,self.account,('TEST_LINK_OWNER',),'TEST_CONFIG_REVISION')
        proof = self.proof(message); self.assertTrue(self.current(proof))
        with self.tx() as cursor:
            self.auth.owners.apply_trusted_config(cursor,self.account,(),'TEST_CONFIG_REVISION2')
        self.assertFalse(self.current(proof))
        self.query("UPDATE qq_official_owner_grants SET state='active',revision=revision+1,revoked_at=NULL WHERE account_key=?",(self.account.key,))
        self.assertFalse(self.current(proof)); self.assertIsNotNone(self.proof(message))
        self.now = proof['expires_at']; self.assertFalse(self.current(proof))

    def test_expiry_crossed_during_authority_lookup_is_checked_at_return(self):
        message = self.message(); proof = self.proof(message); original = self.auth._credentials
        def crossed(cursor,endpoint,actor):
            for credential in original(cursor,endpoint,actor):
                self.now = proof['expires_at']
                yield credential
        with patch.object(self.auth,'_credentials',crossed):
            with self.subTest(boundary='current'):self.assertFalse(self.current(proof))
            self.now = message.received_at
            with self.subTest(boundary='proof'):self.assertIsNone(self.proof(message))

    def test_role_evidence_revision_and_original_time_are_database_constraints(self):
        self.proof()
        for sql in ("UPDATE qq_official_link_roles SET role=''",'UPDATE qq_official_link_roles SET observed_at=observed_at-1',
                'DELETE FROM qq_official_link_roles'):
            with self.subTest(sql=sql),self.assertRaises(DatabaseError) as caught:self.query(sql)
            self.assertIn('immutable_link_role_fence',str(caught.exception.__cause__))
