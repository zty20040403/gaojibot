"""Real PostgreSQL reception boundaries using an actual dedicated gateway lease."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import unittest
from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official.authority import OwnerAuthority
from src.plugins.ai_chat.adapters.qq_official.binding import BindingContext, BindingReducer
from src.plugins.ai_chat.adapters.qq_official.inbox import IngressError, OfficialInboxStore, record_pending_inbox
from src.plugins.ai_chat.adapters.qq_official.lease import DedicatedAccountLease, assert_epoch
from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
from src.plugins.ai_chat.adapters.qq_official.turns import claim_execution, load_turn_input
from src.plugins.ai_chat.application.turn_context import speaker
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from tests.postgres_fixture import postgres_for_test


class OfficialInboxTests(unittest.TestCase):
    def setUp(self):
        self.database = postgres_for_test(self)
        self.now = 1_800_000_000
        self.account = AccountKey('qq-official', 'TEST_INBOX_APP', 'test')
        self.session = 'TEST_INBOX_SESSION'
        self.seq = 0
        self.owner = 'TEST_OWNER_C2C'
        self.other_owner = 'SECOND_OWNER'
        self.group = EndpointKey(self.account, 'group', 'TEST_GROUP')
        self.authority = OwnerAuthority(account=self.account,
                                        owner_c2c_ids=(self.owner, self.other_owner), clock=lambda: self.now)
        self.binding = BindingReducer(b'FAKE-INBOX-PEPPER-LOCAL-TEST-ONLY-12345', epoch_guard=assert_epoch,
                                      authority=self.authority, clock=lambda: self.now)
        self.jobs = DurableJobStore(self.database)
        self.addCleanup(self.jobs.close)
        self.store = self.new_store()
        self.store.ensure_account(self.account)
        self.lease = DedicatedAccountLease(self.database.dsn, self.account, schema=self.database.schema,
                                          clock=lambda: self.now)
        self.addCleanup(self.lease.close)
        self.token = self.lease.acquire()
        self.assertIsNotNone(self.token)
        self.store.apply_configuration(self.token)
        self.assertEqual(self.store.accept({'op': 0, 's': 0, 't': 'READY',
            'd': {'session_id': self.session}}, token=self.token, session_id=self.session).disposition, 'control')

    def new_store(self, **kwargs):
        store = OfficialInboxStore(self.database, jobs=self.jobs, binding=self.binding, authority=self.authority,
                                   clock=lambda: self.now, **kwargs)
        self.addCleanup(store.close)
        return store

    @contextmanager
    def tx(self):
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            yield cursor
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def query(self, query, values=()):
        with self.tx() as cursor:
            return [dict(row) for row in cursor.execute(query, values).fetchall()]

    def count(self, table):
        self.assertIn(table, self.persisted_tables())
        return self.query(f'SELECT COUNT(*) AS n FROM {table}')[0]['n']

    @staticmethod
    def persisted_tables():
        return ('qq_official_inbound_events', 'qq_official_inbound_dedup', 'qq_official_gateway_receipts',
                'qq_official_accounts', 'qq_official_endpoints', 'qq_official_owner_actor_links',
                'qq_official_messages', 'qq_official_message_indexes', 'admin_audit_log', 'durable_jobs', 'messages')

    def persisted_text(self):
        return repr({table: self.query(f'SELECT * FROM {table}') for table in self.persisted_tables()})

    def frame(self, *, seq=None, private=False, actor=None, group='TEST_GROUP', native=None,
              event_id='default', content='ordinary message', event_type=None, **body):
        sequence = self.seq + 1 if seq is None else seq
        identifier = f'TEST_MESSAGE_{sequence}' if native is None else native
        author = {'user_openid': actor or self.owner} if private else {'member_openid': actor or 'TEST_MEMBER'}
        data = {'id': identifier, 'author': author, 'content': content,
                'timestamp': '2027-01-15T08:00:00Z', **body}
        if not private:
            data['group_openid'] = group
        frame = {'op': 0, 's': sequence,
                 't': event_type or ('C2C_MESSAGE_CREATE' if private else 'GROUP_MESSAGE_CREATE'), 'd': data}
        if event_id is not None:
            frame['id'] = f'TEST_EVENT_{sequence}' if event_id == 'default' else event_id
        return frame

    def accept(self, frame=None, *, store=None, **kwargs):
        frame = frame or self.frame(**kwargs)
        result = (store or self.store).accept(frame, token=self.token, session_id=self.session)
        self.seq = max(self.seq, frame['s'])
        return result

    def activate_group(self, endpoint=None):
        endpoint = endpoint or self.group
        with self.tx() as cursor:
            cursor.execute('''INSERT INTO qq_official_endpoints
                (endpoint_key,account_key,kind,native_conversation_id,canonical_scope_key,binding_state,
                 membership_state,created_at,updated_at) VALUES (?,?,?,?,?,'active','present',?,?)
                ON CONFLICT(endpoint_key) DO UPDATE SET binding_state='active',membership_state='present' ''',
                (endpoint.key, endpoint.account.key, endpoint.kind, endpoint.native_conversation_id,
                 endpoint.scope.key, self.now, self.now))

    def snapshot(self):
        checkpoint = self.store.checkpoint(self.token)
        return (checkpoint.session_id, checkpoint.seq,
                tuple((table, self.count(table)) for table in self.persisted_tables()))

    def issue(self, owner=None):
        owner_id = owner or self.owner
        endpoint = EndpointKey(self.account, 'private', owner_id)
        actor = ActorKey(self.account, 'c2c_user', '', owner_id)
        with self.store.transaction() as cursor:
            self.store._endpoint(cursor, endpoint, self.now)
            return self.binding.issue_owner_code(cursor, context=BindingContext(
                endpoint, actor, f'TEST_ISSUE_{self.seq}', self.now), lease_token=self.token)

    def submit(self, issued, *, actor='TEST_MEMBER'):
        member = ActorKey(self.account, 'group_member', self.group.native_conversation_id, actor)
        with self.store.transaction() as cursor:
            self.store._endpoint(cursor, self.group, self.now)
            decision = self.binding.consume_owner_code(cursor, context=BindingContext(
                self.group, member, f'TEST_SUBMIT_{self.seq}', self.now),
                code=issued.effect.code, lease_token=self.token)
        self.assertEqual(decision.status, 'confirmed')
        return decision

    def gap_evidence(self):
        self.submit(self.issue())
        return self.query('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?',(self.group.key,))[0]

    def test_new_gateway_session_preserves_binding_owner_and_deduplication(self):
        before=self.gap_evidence()
        old=self.accept(native='TEST_BEFORE_RECONNECT')
        self.store.checkpoint_callback(self.token,None)
        self.new_identify_session()
        self.assertEqual(self.accept(native='TEST_BEFORE_RECONNECT').disposition,'duplicate')
        after=self.query('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?',(self.group.key,))[0]
        self.assertEqual((after['binding_state'],after['generation'],after['route_epoch']),
            (before['binding_state'],before['generation'],before['route_epoch']))
        with self.tx() as cursor:
            member=ActorKey(self.account,'group_member',self.group.native_conversation_id,'TEST_MEMBER')
            self.assertIsNotNone(self.authority.owner_for_actor(cursor,self.group,member))
        self.assertIsNotNone(old.inbox_id)

    def new_identify_session(self):
        self.session = 'TEST_INBOX_SESSION_NEW_IDENTIFY'
        self.seq = 0
        self.assertEqual(self.store.accept({'op': 0, 's': 0, 't': 'READY',
            'd': {'session_id': self.session}}, token=self.token, session_id=self.session).disposition, 'control')

    def test_accept_commits_inbox_receipt_checkpoint_and_existing_job_together(self):
        self.activate_group()
        result = self.accept()
        self.assertEqual(result.disposition, 'inbox')
        row = self.query('SELECT * FROM qq_official_inbound_events WHERE inbox_id=?', (result.inbox_id,))[0]
        self.assertEqual(row['status'], 'pending')
        self.assertEqual(row['endpoint_key'], self.group.key)
        self.assertIn('ordinary message', row['payload_json'])
        self.assertIsNone(row['ingest_job_id'])
        self.assertEqual(self.store.checkpoint(self.token).seq, 1)
        receipt = self.query('SELECT * FROM qq_official_gateway_receipts WHERE seq=1')[0]
        self.assertEqual((receipt['disposition'], receipt['inbox_id']), ('inbox', result.inbox_id))
        self.assertEqual(self.count('qq_official_inbound_dedup'), 2)
        self.assertEqual(self.count('messages'), 0)


    def test_checkpoint_update_failure_rolls_back_receipt_dedup_inbox_and_job(self):
        self.activate_group()
        before = self.snapshot()
        with self.tx() as cursor:
            cursor_type = type(cursor)
        original = cursor_type.execute

        def fail_checkpoint(cursor, query, values=()):
            if ' '.join(query.split()).startswith('UPDATE qq_official_accounts SET session_id='):
                raise RuntimeError('injected checkpoint failure')
            return original(cursor, query, values)

        with patch.object(cursor_type, 'execute', new=fail_checkpoint), self.assertRaisesRegex(RuntimeError, 'checkpoint'):
            self.accept()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.accept().disposition, 'inbox')

    def test_sequence_gap_session_mismatch_and_replay_cannot_advance_or_duplicate(self):
        self.activate_group()
        before = self.snapshot()
        with self.assertRaisesRegex(IngressError, 'contiguous'):
            self.accept(self.frame(seq=2))
        self.assertEqual(self.snapshot(), before)
        with self.assertRaisesRegex(IngressError, 'session'):
            self.store.accept(self.frame(), token=self.token, session_id='DIFFERENT_SESSION')
        self.assertEqual(self.snapshot(), before)
        frame = self.frame()
        first = self.accept(frame)
        snapshot = self.snapshot()
        duplicate = self.accept(deepcopy(frame))
        self.assertEqual((duplicate.disposition, duplicate.inbox_id), ('duplicate', first.inbox_id))
        self.assertEqual(self.snapshot(), snapshot)
        conflicting = deepcopy(frame)
        conflicting['t'] = 'GROUP_AT_MESSAGE_CREATE'
        with self.assertRaisesRegex(IngressError, 'identity conflict'):
            self.accept(conflicting)
        self.assertEqual(self.snapshot(), snapshot)

    def test_unbound_ordinary_message_persists_no_body_nickname_url_or_content_hash(self):
        sentinels = ('SECRET_BODY_UNBOUND_97131', 'SECRET_NICK_UNBOUND_33173',
                     'https://private.invalid/SECRET_URL_UNBOUND_44931')
        frame = self.frame(content=sentinels[0], author={'member_openid': 'TEST_MEMBER', 'nickname': sentinels[1]},
                           attachments=[{'url': sentinels[2], 'content_type': 'image/png', 'filename': 'SECRET_FILENAME_9943'}])
        result = self.accept(frame)
        self.assertEqual((result.disposition, result.reason), ('skipped', 'not_admitted'))
        row = self.query('SELECT * FROM qq_official_inbound_events WHERE inbox_id=?', (result.inbox_id,))[0]
        self.assertIsNone(row['payload_json'])
        self.assertIsNone(row['payload_expires_at'])
        self.assertIsNone(row['ingest_job_id'])
        output = self.persisted_text()
        for value in (*sentinels, 'SECRET_FILENAME_9943', json.dumps(frame)):
            self.assertNotIn(value, output)
            self.assertNotIn(hashlib.sha256(value.encode()).hexdigest(), output)
        self.assertEqual(self.count('durable_jobs'), 0)
        self.assertEqual(self.count('messages'), 0)
        self.assertEqual(self.store.checkpoint(self.token).seq, 1)

    def test_owner_private_allowlist_public_and_group_admission_are_distinct(self):
        self.assertEqual(self.accept(private=True).disposition, 'inbox')
        self.assertEqual(self.accept(private=True, actor='TEST_OTHER').disposition, 'skipped')
        self.assertEqual(self.accept(actor=self.owner, author={'member_openid': self.owner, 'member_role': 'owner'}).disposition, 'skipped')
        allowed = self.new_store(private_policy='allowlist', private_allowed_openids=('TEST_ALLOWED',))
        self.assertEqual(self.accept(store=allowed, private=True, actor='TEST_ALLOWED').disposition, 'inbox')
        self.assertEqual(self.accept(store=allowed, private=True, actor='TEST_NOT_ALLOWED').disposition, 'skipped')
        public = self.new_store(private_policy='public')
        self.assertEqual(self.accept(store=public, private=True, actor='TEST_PUBLIC').disposition, 'inbox')
        with self.tx() as cursor:
            actor = ActorKey(self.account, 'c2c_user', '', 'TEST_PUBLIC')
            self.assertIsNone(self.authority.owner_for_actor(cursor, EndpointKey(self.account, 'private', 'TEST_PUBLIC'), actor))
        self.activate_group()
        self.assertEqual(self.accept().disposition, 'inbox')
        with self.tx() as cursor:
            cursor.execute("UPDATE qq_official_endpoints SET membership_state='unknown' WHERE endpoint_key=?", (self.group.key,))
            self.assertFalse(self.store.admitted(cursor, self.group,
                ActorKey(self.account, 'group_member', self.group.native_conversation_id, 'TEST_MEMBER')))
        # A fresh authenticated human event proves presence; an explicit revoke
        # remains distinct from unknown lifecycle continuity after a gap.
        self.assertEqual(self.accept().disposition, 'inbox')
        for binding, membership in (('revoked', 'present'), ('revoked', 'absent')):
            with self.tx() as cursor:
                cursor.execute('UPDATE qq_official_endpoints SET binding_state=?,membership_state=? WHERE endpoint_key=?',
                               (binding, membership, self.group.key))
            self.assertEqual(self.accept().disposition, 'skipped')

    def test_retired_console_identity_text_creates_no_binding_or_chat(self):
        text = '/admin-bind ' + 'a' * 32 + '.' + 'b' * 32
        for private in (True, False):
            received = self.accept(content=text, private=private)
            row, = self.query('SELECT payload_json,ingest_job_id FROM qq_official_inbound_events WHERE inbox_id=?', (received.inbox_id,))
            self.assertEqual(row, {'payload_json': None, 'ingest_job_id': None})
            self.assertEqual(received.disposition, 'quarantined')
        self.assertEqual(self.count('messages'), 0)
        self.assertNotIn('b' * 32, self.persisted_text())

    def test_owner_binding_entries_are_body_free_and_unbound_group_gets_no_code(self):
        hint = self.accept(content='/绑定群')
        self.assertEqual((hint.disposition, hint.binding.status), ('control', 'guide'))
        self.assertNotIn('000042', hint.binding.message)
        self.assertEqual(self.accept(content='000042').disposition, 'quarantined')
        with patch('src.plugins.ai_chat.adapters.qq_official.binding.secrets.randbelow', return_value=42):
            private = self.frame(private=True, content=' ' * 64 + '/绑定群')
            created = self.accept(private)
        self.assertEqual((created.disposition, created.binding.status), ('control', 'owner_code_issued'))
        self.assertEqual(created.binding.effect.code, '000042')
        self.assertIsNone(self.accept(private).binding)
        submitted = self.accept(content='000042')
        self.assertEqual((submitted.disposition, submitted.binding.status), ('control', 'confirmed'))
        self.assertIsNone(self.accept(content='/绑定群 000042').binding.effect)
        self.assertEqual(self.accept(content='000042').disposition, 'quarantined')
        self.assertNotIn('000042', self.persisted_text())
        self.assertEqual(self.count('durable_jobs'), 0)
        ordinary = self.accept(content='一共有 123456 件')
        self.assertEqual(ordinary.disposition, 'inbox')
        self.assertIsNone(ordinary.binding)
        rows = self.query('SELECT payload_json,ingest_job_id,reason_code FROM qq_official_inbound_events')
        self.assertTrue(all(row['payload_json'] is None and row['ingest_job_id'] is None for row in rows[:-1]))

    def test_invalid_payload_and_missing_ids_are_terminal_without_public_empty_dedup(self):
        oversized = self.frame(content='SECRET_TOO_LARGE_' * 20_000)
        self.assertEqual(self.accept(oversized).disposition, 'quarantined')
        for _ in range(2):
            missing = self.frame(event_id=None, content='SECRET_MISSING_IDS')
            missing['d'].pop('id')
            self.assertEqual(self.accept(missing).disposition, 'quarantined')
        self.assertEqual(self.count('qq_official_inbound_dedup'), 0)
        self.assertEqual(self.count('durable_jobs'), 0)
        self.assertEqual(self.store.checkpoint(self.token).seq, 3)
        self.assertNotIn('SECRET_TOO_LARGE_', self.persisted_text())
        self.assertNotIn('SECRET_MISSING_IDS', self.persisted_text())
        self.assertEqual(self.accept(private=True).disposition, 'inbox')
        self.assertEqual(self.store.checkpoint(self.token).seq, 4)

    def test_message_and_event_dedup_are_separate_and_missing_event_id_has_no_empty_alias(self):
        self.activate_group()
        first = self.accept(native='M1', event_id='E1')
        second = self.accept(native='M1', event_id='E2')
        self.assertEqual((second.disposition, second.inbox_id), ('duplicate', first.inbox_id))
        no_event = self.accept(native='M2', event_id=None)
        no_event_replay = self.accept(native='M2', event_id=None)
        self.assertEqual((no_event_replay.disposition, no_event_replay.inbox_id), ('duplicate', no_event.inbox_id))
        self.assertEqual(self.count('durable_jobs'), 0)
        rows = self.query('SELECT key_kind,native_id FROM qq_official_inbound_dedup')
        self.assertEqual({(row['key_kind'], row['native_id']) for row in rows},
                         {('event', 'E1'), ('event', 'E2'), ('message', 'M1'), ('message', 'M2')})
        self.assertTrue(all(row['native_id'] for row in rows))
        self.assertEqual(self.store.checkpoint(self.token).seq, 4)

    def test_conflicting_event_and_message_aliases_do_not_merge_or_attach_new_message(self):
        self.activate_group()
        first = self.accept(native='M1', event_id='E1')
        second = self.accept(native='M2', event_id='E2')
        conflict = self.accept(native='M2', event_id='E1')
        self.assertEqual(conflict.disposition, 'quarantined')
        another = self.accept(native='M3', event_id='E1')
        self.assertEqual(another.disposition, 'quarantined')
        rows = self.query('SELECT key_kind,native_id,inbox_id FROM qq_official_inbound_dedup')
        aliases = {(r['key_kind'], r['native_id']): r['inbox_id'] for r in rows}
        self.assertNotIn(('message', 'M3'), aliases)
        self.assertEqual(aliases[('message', 'M1')], first.inbox_id)
        self.assertEqual(aliases[('message', 'M2')], second.inbox_id)
        self.assertEqual(self.count('durable_jobs'), 0)

    def test_same_message_id_in_two_groups_is_distinct_but_event_alias_cannot_cross_endpoint(self):
        self.activate_group()
        other = EndpointKey(self.account, 'group', 'OTHER_GROUP')
        self.activate_group(other)
        first = self.accept(native='SHARED_MESSAGE', event_id='E1')
        second = self.accept(group=other.native_conversation_id, native='SHARED_MESSAGE', event_id='E2')
        self.assertEqual(second.disposition, 'inbox')
        self.assertNotEqual(first.inbox_id, second.inbox_id)
        conflict = self.accept(group=other.native_conversation_id, native='OTHER_MESSAGE', event_id='E1')
        self.assertEqual(conflict.disposition, 'inbox')
        self.assertEqual(self.count('durable_jobs'), 0)

    def test_lifecycle_removal_atomically_revokes_owner_link(self):
        self.submit(self.issue())
        endpoint = self.query('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?', (self.group.key,))[0]
        member = ActorKey(self.account, 'group_member', self.group.native_conversation_id, 'TEST_MEMBER')
        with self.tx() as cursor:
            self.assertIsNotNone(self.authority.owner_for_actor(cursor, self.group, member))
        jobs_before = self.count('durable_jobs')
        removed = self.accept({'op': 0, 's': self.seq + 1, 't': 'GROUP_DEL_ROBOT', 'id': 'DELETE_EVENT',
                               'd': {'group_openid': self.group.native_conversation_id}})
        self.assertEqual(removed.disposition, 'control')
        endpoint_after = self.query('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?', (self.group.key,))[0]
        self.assertEqual((endpoint_after['membership_state'], endpoint_after['binding_state']), ('absent', 'revoked'))
        self.assertEqual(endpoint_after['generation'], endpoint['generation'] + 1)
        self.assertEqual(endpoint_after['route_epoch'], endpoint['route_epoch'] + 1)
        self.assertEqual(self.query('SELECT state FROM qq_official_owner_actor_links')[0]['state'], 'revoked')
        with self.tx() as cursor:
            self.assertIsNone(self.authority.owner_for_actor(cursor, self.group, member))
            self.assertFalse(self.store.admitted(cursor, self.group, member))
        self.assertEqual(self.count('durable_jobs'), jobs_before)
        self.assertEqual(self.count('messages'), 0)
        self.assertEqual(self.accept().disposition, 'skipped')

    def test_repeated_same_membership_state_is_idempotent_and_rejoin_does_not_rebind(self):
        self.activate_group()
        self.accept({'op': 0, 's': self.seq + 1, 't': 'GROUP_DEL_ROBOT', 'id': 'DELETE_1',
                     'd': {'group_openid': self.group.native_conversation_id}})
        deleted = self.query('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?', (self.group.key,))[0]
        self.accept({'op': 0, 's': self.seq + 1, 't': 'GROUP_DEL_ROBOT', 'id': 'DELETE_2',
                     'd': {'group_openid': self.group.native_conversation_id}})
        self.assertEqual(self.query('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?', (self.group.key,))[0], deleted)
        self.accept({'op': 0, 's': self.seq + 1, 't': 'GROUP_ADD_ROBOT', 'id': 'ADD_1',
                     'd': {'group_openid': self.group.native_conversation_id}})
        joined = self.query('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?', (self.group.key,))[0]
        self.assertEqual((joined['membership_state'], joined['binding_state']), ('present', 'revoked'))
        self.accept({'op': 0, 's': self.seq + 1, 't': 'GROUP_ADD_ROBOT', 'id': 'ADD_2',
                     'd': {'group_openid': self.group.native_conversation_id}})
        self.assertEqual(self.query('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?', (self.group.key,))[0], joined)
        self.assertEqual(self.accept().disposition, 'skipped')
        self.assertEqual(self.count('durable_jobs'), 0)

    def test_proactive_rejection_does_not_remove_membership_or_binding(self):
        self.activate_group()
        original = self.query('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?', (self.group.key,))[0]
        result = self.accept({'op': 0, 's': self.seq + 1, 't': 'GROUP_MSG_REJECT', 'id': 'REJECT_EVENT',
                              'd': {'group_openid': self.group.native_conversation_id}})
        self.assertEqual(result.disposition, 'control')
        after = self.query('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?', (self.group.key,))[0]
        self.assertEqual((after['binding_state'], after['membership_state'], after['proactive_state']),
                         ('active', 'present', 'denied'))
        self.assertEqual((after['generation'], after['route_epoch']), (original['generation'], original['route_epoch']))
        self.assertEqual(self.count('durable_jobs'), 0)



    def test_explicit_del_binding_stays_revoked_after_gap_and_fresh_human_message(self):
        self.activate_group()
        self.accept({'op': 0, 's': 1, 't': 'GROUP_DEL_ROBOT', 'id': 'PRE_GAP_DEL',
                     'd': {'group_openid': self.group.native_conversation_id}})
        self.store.checkpoint_callback(self.token, None)
        unknown = self.query('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?', (self.group.key,))[0]
        self.assertEqual((unknown['membership_state'], unknown['binding_state']), ('absent', 'revoked'))
        self.new_identify_session()
        fresh = self.accept(native='NEW_AFTER_EXPLICIT_DEL', event_id='NEW_EVENT_AFTER_EXPLICIT_DEL')
        self.assertEqual((fresh.disposition, fresh.reason), ('skipped', 'not_admitted'))
        after = self.query('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?', (self.group.key,))[0]
        self.assertEqual((after['membership_state'], after['binding_state']), ('present', 'revoked'))
        self.assertEqual(after['route_epoch'], unknown['route_epoch']+1)
        self.assertEqual(self.count('durable_jobs'), 0)
        self.assertIsNone(self.query('SELECT payload_json FROM qq_official_inbound_events WHERE inbox_id=?',
                                     (fresh.inbox_id,))[0]['payload_json'])

    def test_repeated_none_checkpoint_callback_does_not_bump_generations_or_grant_revisions(self):
        self.gap_evidence()
        self.store.checkpoint_callback(self.token, None)
        tables = ('qq_official_accounts', 'qq_official_endpoints', 'qq_official_owner_actor_links')
        before = {table: self.query(f'SELECT * FROM {table} ORDER BY 1') for table in tables}
        self.now += 5
        self.store.checkpoint_callback(self.token, None)
        self.assertEqual({table: self.query(f'SELECT * FROM {table} ORDER BY 1') for table in tables}, before)
        self.assertIsNone(self.store.checkpoint(self.token))
        self.assertEqual(before['qq_official_accounts'][0]['fencing_epoch'], self.token.epoch)
        self.assertEqual(len(self.query('SELECT job_id FROM durable_jobs WHERE kind IN (?,?)',
            (account_job_kind('ingest', self.account.key), account_job_kind('turn', self.account.key)))), 0)

    def ingest(self, inbox_id):
        ledger = MessageLedger(self.database)
        try:
            with self.store.transaction() as cursor:
                return record_pending_inbox(cursor, inbox_id=inbox_id, lease_token=self.token,
                    ledger=ledger, epoch_guard=assert_epoch,
                    admission=self.store.admitted, now=self.now)
        finally:
            ledger.close()

    def test_single_pass_accept_with_ledger_records_canonical_message_immediately(self):
        self.activate_group()
        ledger = MessageLedger(self.database)
        self.addCleanup(ledger.close)
        frame = self.frame(native='TEST_SINGLE_PASS', content='hello direct ledger', event_type='GROUP_AT_MESSAGE_CREATE',
                           mentions=[{'id': 'TEST_BOT', 'is_you': True}])
        accepted = self.store.accept(frame, token=self.token, session_id=self.session, ledger=ledger)
        self.seq = max(self.seq, frame['s'])
        self.assertEqual(accepted.disposition, 'inbox')
        self.assertIsNotNone(accepted.ingested)
        self.assertEqual(accepted.ingested.status, 'processed')
        self.assertTrue(accepted.ingested.reply_requested)
        self.assertEqual(self.count('messages'), 1)
        self.assertEqual(self.count('qq_official_messages'), 1)
        self.assertEqual(self.count('durable_jobs'), 0)

    def test_pending_member_quote_is_not_a_bot_address_even_if_dispatched(self):
        self.activate_group()
        target = self.ingest(self.accept(actor='TEST_BOB', native='TEST_QUOTED',
            message_scene={'ext': {'msg_idx': 'TEST_QUOTED_IDX'}}).inbox_id)
        accepted = self.accept(actor='TEST_ALICE', message_scene={'ext': {'ref_msg_idx': 'TEST_QUOTED_IDX'}})
        result = self.ingest(accepted.inbox_id)
        self.assertFalse(result.reply_requested)
        with self.tx() as cursor:
            row = cursor.execute("""SELECT i.trigger_requested,m.reply_to_canonical_message_id
                FROM qq_official_inbound_events i JOIN messages m USING(canonical_message_id)
                WHERE i.inbox_id=?""", (accepted.inbox_id,)).fetchone()
            self.assertFalse(row['trigger_requested'])
            self.assertEqual(row['reply_to_canonical_message_id'], target.canonical_message_id)
            # Even historical dispatch intent isn't evidence of addressing the bot.
            cursor.execute('UPDATE qq_official_inbound_events SET trigger_requested=TRUE WHERE inbox_id=?',
                           (accepted.inbox_id,))
            decision = claim_execution(cursor, token=self.token, inbox_id=accepted.inbox_id,
                now=self.now, deadline_at=self.now+60, admission=self.store.admitted)
            inbound = load_turn_input(cursor, token=self.token, handle=decision.handle,
                                     now=self.now, admission=self.store.admitted)
        self.assertEqual(inbound.reply_to_native_message_id, 'TEST_QUOTED')
        self.assertFalse(inbound.mentions_bot)
        self.assertEqual(speaker(inbound, reply_to_id=target.canonical_message_id),
                         f'{inbound.sender_display} 回复#{target.canonical_message_id}')
        with self.tx() as cursor:
            cursor.execute("UPDATE qq_official_message_indexes SET ambiguous=TRUE WHERE index_value='TEST_QUOTED_IDX'")
            inbound = load_turn_input(cursor, token=self.token, handle=decision.handle,
                                     now=self.now, admission=self.store.admitted)
        self.assertIsNone(inbound.reply_to_native_message_id)

    def test_late_at_adds_one_intent_without_rewriting_body_or_window(self):
        self.activate_group()
        accepted = self.accept(native='TEST_LATE_AT_MESSAGE', content='first accepted body')
        result = self.ingest(accepted.inbox_id)
        self.assertEqual(result.status, 'processed')
        newer = self.ingest(self.accept(native='TEST_NEWER', content='newer topic still pending').inbox_id)
        before_messages = self.query('SELECT * FROM messages')
        for _ in range(2):
            replay = self.accept(native='TEST_LATE_AT_MESSAGE', content='different duplicate body',
                                 event_type='GROUP_AT_MESSAGE_CREATE')
            self.assertEqual(replay.inbox_id, accepted.inbox_id)
        row = self.query('SELECT * FROM qq_official_inbound_events WHERE inbox_id=?', (accepted.inbox_id,))[0]
        self.assertTrue(row['trigger_requested'])
        self.assertTrue(row['explicit_mention'])
        self.assertIsNone(row['dispatch_job_id'])
        self.assertEqual(self.count('durable_jobs'), 0)
        self.assertEqual(self.query('SELECT * FROM messages'), before_messages)
        pending = self.query('SELECT attention_cursor,attention_end FROM qq_official_endpoints WHERE endpoint_key=?', (row['endpoint_key'],))[0]
        self.assertEqual((pending['attention_cursor'], pending['attention_end']), (result.canonical_message_id, newer.canonical_message_id))

    def test_expired_late_at_does_not_dispatch_and_purge_waits_for_queue_settlement(self):
        self.activate_group()
        accepted = self.accept(native='TEST_EXPIRED_MESSAGE')
        self.ingest(accepted.inbox_id)
        first = self.query('SELECT * FROM qq_official_inbound_events WHERE inbox_id=?', (accepted.inbox_id,))[0]
        self.now = first['payload_expires_at']
        replay = self.accept(native='TEST_EXPIRED_MESSAGE', event_type='GROUP_AT_MESSAGE_CREATE')
        self.assertEqual(replay.inbox_id, accepted.inbox_id)
        row = self.query('SELECT * FROM qq_official_inbound_events WHERE inbox_id=?', (accepted.inbox_id,))[0]
        self.assertFalse(row['trigger_requested'])
        self.assertIsNone(row['dispatch_job_id'])
        self.assertIsNotNone(row['payload_json'])
        evidence_before = {table: self.query(f'SELECT * FROM {table} ORDER BY 1') for table in
            ('qq_official_inbound_dedup', 'qq_official_gateway_receipts', 'qq_official_messages',
             'qq_official_message_indexes', 'messages')}
        self.assertEqual(self.store.purge_terminal_payloads(self.token), 1)
        self.assertIsNone(self.query('SELECT payload_json FROM qq_official_inbound_events WHERE inbox_id=?',
                                    (accepted.inbox_id,))[0]['payload_json'])
        self.assertEqual({table: self.query(f'SELECT * FROM {table} ORDER BY 1') for table in evidence_before}, evidence_before)
