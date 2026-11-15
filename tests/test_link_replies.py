"""Body-free reply source evidence; this step deliberately sends no QQ message."""
from unittest.mock import patch
import psycopg

from src.bot_storage import DatabaseError
from src.plugins.ai_chat.adapters.qq_official.lease import DedicatedAccountLease,LeaseLost
from src.plugins.ai_chat.platform_types import AccountKey,EndpointKey
from tests import test_link_ingress as fixture


class LinkReplyTests(fixture.LinkIngressTests):
    def setUp(self):
        super().setUp(); self.replies = self.links.replies; self.replies.admission = self.store.admitted

    def reply(self,accepted):
        return self.query('SELECT * FROM qq_official_link_replies WHERE source_inbox_id=?',(accepted.inbox_id,))[0]

    def read(self,row):
        with self.tx() as cursor:return self.replies.source(cursor,self.token,row)

    def query(self,sql,args=()):
        try:
            with self.tx() as cursor:
                cursor.execute(sql,args)
                return [dict(row) for row in cursor.fetchall()] if sql.lstrip().upper().startswith('SELECT') else []
        except DatabaseError as error:
            if not isinstance(error.__cause__,psycopg.Error):raise
            raise error.__cause__ from error  # Assert the actual PG constraint, not the sanitized wrapper.

    def insert(self,row):
        self.query('INSERT INTO qq_official_link_replies('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))

    def test_authenticated_outcome_is_frozen_and_never_borrows_a_later_window(self):
        accepted = self.accept(self.link_frame('/link list')); row = self.reply(accepted)
        self.assertEqual((row['status'],row['entries_json'],row['more']),('listed','[]',False))
        self.assertIsNotNone(row['authority_hash']); self.assertEqual(self.read(row)['inbox_id'],accepted.inbox_id)
        self.now += 1; newer = self.accept(self.link_frame('TEST_NEWER_ADMIN_WINDOW'))
        self.assertNotEqual(self.read(row)['inbox_id'],newer.inbox_id)
        self.assertEqual(self.query('SELECT * FROM messages'),[])
        self.assertEqual(self.query('SELECT * FROM delivery_plans'),[])
        self.assertNotIn('/link list',repr(row)+self.persisted_text())
        for field,value in (('status','denied'),('source_hash','b'*64),('expires_at',self.now+2)):
            with self.subTest(field=field),self.assertRaisesRegex(psycopg.Error,'immutable_link_reply'):
                self.query(f'UPDATE qq_official_link_replies SET {field}=? WHERE source_inbox_id=?',(value,accepted.inbox_id))
        with self.assertRaisesRegex(psycopg.Error,'immutable_link_reply'):
            self.query('DELETE FROM qq_official_link_replies WHERE source_inbox_id=?',(accepted.inbox_id,))

    def test_duplicate_and_code_issuance_do_not_create_another_control_reply(self):
        code = self.issue(); self.assertEqual(self.query('SELECT * FROM qq_official_link_replies'),[])
        self.assertIsNotNone(code._link_input.code)
        frame = self.link_frame('/link list'); accepted = self.accept(frame)
        frame['s'] += 1; duplicate = self.accept(frame)
        self.assertIsNone(duplicate._link_input)
        self.assertEqual(len(self.query('SELECT * FROM qq_official_link_replies')),1)
        self.assertEqual(self.read(self.reply(accepted))['inbox_id'],accepted.inbox_id)
        self.assertNotIn('723891',str(self.query('SELECT * FROM qq_official_link_replies')))

    def test_unprivileged_generic_denial_cannot_become_a_privileged_outcome(self):
        accepted = self.accept(self.link_frame('/link list',role='member')); row = self.reply(accepted)
        self.assertEqual((row['status'],row['authority_hash']),('denied',None)); self.read(row)
        forged = dict(row,status='listed',link_ref='a'*32); forged['source_hash'] = self.replies.source_hash(forged)
        with self.assertRaisesRegex(PermissionError,'authority_required'):self.read(forged)
        with self.assertRaisesRegex(psycopg.Error,'link_reply_unprivileged'):self.insert(forged)
        self.store.private_policy = 'public'; self.activate_group(EndpointKey(self.account,'private','TEST_PRIVATE_PEER'))
        private = self.accept(self.frame(private=True,actor='TEST_PRIVATE_PEER',content='/link list',
            timestamp=self.link_frame()['d']['timestamp']))
        private_row = self.reply(private); self.read(private_row)
        self.store.private_policy = 'owner_only'
        with self.assertRaisesRegex(PermissionError,'source_changed'):self.read(private_row)

    def test_original_gateway_and_signed_outcome_are_required(self):
        accepted = self.accept(self.link_frame('/link list')); row = self.reply(accepted)
        with self.assertRaisesRegex(PermissionError,'source_changed'):self.read(dict(row,entries_json='["TEST_OTHER_DATA"]'))
        with self.tx() as cursor:cursor.execute('DELETE FROM qq_official_gateway_receipts WHERE inbox_id=?',(accepted.inbox_id,))
        with self.assertRaisesRegex(PermissionError,'authenticated_receipt_unavailable'):self.read(row)

    def test_final_clock_after_authority_query_and_role_regrant_cannot_revive_reply(self):
        row = self.reply(self.accept(self.link_frame('/link list'))); current = self.links.authority.current
        def crosses(*args):
            allowed = current(*args); self.now = row['expires_at']; return allowed
        with patch.object(self.links.authority,'current',side_effect=crosses):
            with self.assertRaisesRegex(PermissionError,'expired'):self.read(row)
        self.now = row['received_at']+1; self.accept(self.link_frame('TEST_DEMOTION',role='member'))
        self.now += 1; self.accept(self.link_frame('TEST_REGRANT',role='admin'))
        with self.assertRaisesRegex(PermissionError,'authority_changed'):self.read(row)

    def test_original_route_time_and_inbox_composite_binding_are_not_replaceable(self):
        row = self.reply(self.accept(self.link_frame('/link list')))
        other = self.accept(self.frame(content='/link',author={'member_openid':'TEST_OTHER','member_role':'admin'},
            timestamp=self.link_frame()['d']['timestamp']))
        wrong = dict(row,source_inbox_id=other.inbox_id)
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):self.insert(wrong)
        with self.assertRaisesRegex(psycopg.Error,'link_reply_original_input_required'):
            self.insert(dict(row,received_at=row['received_at']-1))
        with self.tx() as cursor:cursor.execute('UPDATE qq_official_endpoints SET route_epoch=route_epoch+1 WHERE endpoint_key=?',(self.group.key,))
        with self.assertRaisesRegex(PermissionError,'source_changed'):self.read(row)

    def test_full_account_isolation_and_restart_never_renew_original_reply(self):
        accepted = self.accept(self.link_frame('/link list')); row = self.reply(accepted)
        other = AccountKey(self.account.platform,self.account.app_id,'sandbox'); self.store.ensure_account(other)
        other_lease = DedicatedAccountLease(self.database.dsn,other,schema=self.database.schema,clock=lambda:self.now)
        self.addCleanup(other_lease.close); other_token = other_lease.acquire(); self.assertIsNotNone(other_token)
        with self.tx() as cursor:
            self.assertIsNone(self.replies.get(cursor,other_token,accepted.inbox_id))
            with self.assertRaisesRegex(PermissionError,'source_unavailable'):self.replies.source(cursor,other_token,row)
        self.lease.close()
        with self.assertRaises(LeaseLost):self.read(row)
        replacement = DedicatedAccountLease(self.database.dsn,self.account,schema=self.database.schema,clock=lambda:self.now)
        self.addCleanup(replacement.close); self.token = replacement.acquire(); self.assertIsNotNone(self.token)
        self.assertGreater(self.token.epoch,row['account_epoch'])
        with self.assertRaisesRegex(PermissionError,'source_unavailable'):self.read(row)
        self.assertEqual(self.reply(accepted),row)

    def test_reply_capture_failure_rolls_back_the_actual_tx_a_checkpoint(self):
        frame = self.link_frame('/link list'); before = self.store.checkpoint(self.token).seq
        with patch.object(self.replies,'capture',side_effect=RuntimeError('TEST_REPLY_CAPTURE_FAILURE')):
            with self.assertRaisesRegex(RuntimeError,'TEST_REPLY_CAPTURE_FAILURE'):self.accept(frame)
        self.assertEqual(self.store.checkpoint(self.token).seq,before)
        self.assertEqual(self.query('SELECT * FROM qq_official_link_replies'),[])
        self.assertEqual(self.query('SELECT * FROM qq_official_link_roles'),[])


for name in dir(fixture.LinkIngressTests):
    if name.startswith('test_') and name not in LinkReplyTests.__dict__:setattr(LinkReplyTests,name,None)
