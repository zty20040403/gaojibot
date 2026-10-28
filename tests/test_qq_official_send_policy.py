"""Real PostgreSQL send-policy transactions with synthetic clocks and leases."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import threading
import unittest
import uuid

from src.plugins.ai_chat.adapters.qq_official.lease import DedicatedAccountLease, LeaseLost
from src.plugins.ai_chat.adapters.qq_official.rate_policy import LocalRatePermit, VerifiedDayBoundary, daily_quota_spec
from src.plugins.ai_chat.adapters.qq_official.send_policy import (
    PolicyWait, SendPolicyChoice, SendPolicyError, apply_policy_rejection, check_send_policy,
    extend_server_cooldown, read_server_cooldown, reserve_send_policy, settle_send_policy,
)
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey
from tests.postgres_fixture import postgres_for_test


def opaque():
    return 'TEST_' + uuid.uuid4().hex


def insert(cursor, table, values, returning=None):
    query = f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})"
    if returning:
        query += f' RETURNING {returning}'
    cursor.execute(query, tuple(values.values()))
    return cursor.fetchone()[0] if returning else None


class OfficialSendPolicyTests(unittest.TestCase):
    def setUp(self):
        self.database = postgres_for_test(self)
        self.endpoint = EndpointKey(AccountKey('qq-official', 'TEST_POLICY_APP', 'test'), 'private', 'TEST_PERSON')
        with self.tx() as cursor:
            self.provision(cursor, self.endpoint)
        self.lease = DedicatedAccountLease(self.database.dsn, self.endpoint.account, schema=self.database.schema)
        self.addCleanup(self.lease.close)
        self.token = self.lease.acquire()

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

    def provision(self, cursor, endpoint):
        cursor.execute('''INSERT INTO qq_official_accounts
            (account_key,enabled,gateway_state,created_at,updated_at) VALUES (?,TRUE,'idle',100,100)
            ON CONFLICT(account_key) DO NOTHING''', (endpoint.account.key,))
        cursor.execute('''INSERT INTO qq_official_endpoints
            (endpoint_key,account_key,kind,native_conversation_id,canonical_scope_key,binding_state,membership_state,created_at,updated_at)
            VALUES (?,?,?,?,?,'active','present',100,100) ON CONFLICT(endpoint_key) DO NOTHING''',
            (endpoint.key,endpoint.account.key,endpoint.kind,endpoint.native_conversation_id,endpoint.scope.key))

    def permit(self, endpoint=None, mode='passive'):
        endpoint = endpoint or self.endpoint
        return LocalRatePermit(endpoint.account.key, endpoint.key, 'group' if endpoint.kind=='group' else 'c2c', mode, 'unverified', 0.0)

    def check(self, cursor, *, endpoint=None, token=None, source='TEST_SOURCE', now=100, purpose='final', **kwargs):
        endpoint = endpoint or self.endpoint
        row = cursor.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?', (endpoint.key,)).fetchone()
        options = {'deadline': 200000, 'rate_permit': self.permit(endpoint), **kwargs}
        return check_send_policy(cursor, token=token or self.token, endpoint=endpoint, endpoint_row=row,
                                 source_native_id=source, purpose=purpose, now=now, **options)

    def window(self, cursor, *, endpoint=None, native='TEST_SOURCE', **changes):
        endpoint = endpoint or self.endpoint
        values = dict(window_id=opaque(), account_key=endpoint.account.key, endpoint_key=endpoint.key,
                      endpoint_generation=1, route_epoch=1, credential_kind='msg_id', credential_value=native,
                      issued_at=90, received_at=90, expires_at=400, max_replies=4, state='active',
                      created_at=90, updated_at=90)
        values.update(changes)
        insert(cursor, 'qq_official_reply_windows', values)
        return values['window_id']

    def allow_proactive(self, cursor, endpoint=None):
        endpoint = endpoint or self.endpoint
        cursor.execute("UPDATE qq_official_endpoints SET proactive_state='allowed' WHERE endpoint_key=?", (endpoint.key,))

    def part(self, cursor, choice, now):
        endpoint = choice.endpoint
        actor = ActorKey(endpoint.account, 'c2c_user', '', endpoint.native_conversation_id) if endpoint.kind=='private' else ActorKey(endpoint.account, 'group_member', endpoint.native_conversation_id, 'TEST_REQUESTER')
        plan_id = opaque()
        digest = hashlib.sha256(plan_id.encode()).hexdigest()
        insert(cursor,'delivery_plans',dict(plan_id=plan_id,idempotency_key='qqp1:'+digest,account_key=endpoint.account.key,
            endpoint_key=endpoint.key,requester_actor_key=actor.key,source_scope_key=endpoint.scope.key,
            endpoint_generation=choice.endpoint_generation,route_epoch=choice.route_epoch,capability_revision=choice.capability_revision,
            purpose=choice.purpose,payload_mode='persistent',manifest_json='{}',manifest_digest=digest,digest_scheme='sha256',
            part_count=1,expires_at=200000,priority=0,created_at=now))
        return insert(cursor,'deliveries',dict(idempotency_key='qqd1:'+digest,source_scope_key=endpoint.scope.key,
            target_platform='qq-official',target_kind=endpoint.kind,target_native_conversation_id=endpoint.native_conversation_id,
            body_json='{"v":1,"nodes":[]}',content_fingerprint='TEST_SAFE',status='pending',next_attempt_at=now,created_at=now,updated_at=now,
            plan_id=plan_id,part_index=0,account_key=endpoint.account.key,endpoint_key=endpoint.key,requester_actor_key=actor.key,
            endpoint_generation=choice.endpoint_generation,route_epoch=choice.route_epoch,capability_revision=choice.capability_revision,
            purpose=choice.purpose,payload_mode='persistent',variant='primary',payload_digest=digest,digest_scheme='sha256',priority=0,expires_at=200000), 'delivery_id')

    def attempt(self, cursor, choice, *, now=100, token=None):
        token = token or self.token
        part = self.part(cursor,choice,now)
        attempt = insert(cursor,'delivery_attempts',dict(delivery_id=part,attempt=1,state='reserved',started_at=now,
            account_key=choice.endpoint.account.key,endpoint_key=choice.endpoint.key,fencing_epoch=token.epoch,
            endpoint_generation=choice.endpoint_generation,route_epoch=choice.route_epoch,capability_revision=choice.capability_revision,
            send_mode=choice.send_mode,window_id=choice.window_id,msg_seq=choice.msg_seq,
            window_charge_state='reserved' if choice.window_id else 'none',payload_mode='persistent',payload_digest='b'*64,
            digest_scheme='sha256',wire_payload_json='{"content":"synthetic"}',send_by_at=choice.send_by_at,acceptance='unknown'), 'attempt_id')
        cursor.execute("UPDATE deliveries SET current_attempt_id=?,claim_epoch=?,status='sending',attempts=1 WHERE delivery_id=?", (attempt,token.epoch,part))
        return attempt

    def reserve(self, cursor, choice, *, now=100, token=None):
        attempt = self.attempt(cursor,choice,now=now,token=token)
        reserve_send_policy(cursor,token=token or self.token,choice=choice,attempt_id=attempt,now=now)
        return attempt

    def quota(self, cursor, *, now=100, limit=1000, used=0, reserved=0, boundary=None):
        spec = daily_quota_spec(self.endpoint,'proactive',now=now,unverified_period_start=now,verified_boundary=boundary)
        insert(cursor,'qq_official_quota_periods',dict(quota_key=spec.quota_key,account_key=spec.account_key,endpoint_key=spec.endpoint_key,
            scene=spec.scene,send_mode=spec.send_mode,metric=spec.metric,period_start_at=spec.period_start_at,
            period_end_at=spec.period_end_at,reset_verified=spec.reset_verified,limit_count=limit,used_count=used,reserved_count=reserved,
            policy_revision=spec.policy_revision,evidence_code=spec.evidence_code,created_at=now,updated_at=now))
        return spec

    def test_candidate_probe_is_read_only_and_reservation_requires_rate_permit(self):
        with self.tx() as cursor:
            self.window(cursor)
            choice = self.check(cursor,rate_permit=None)
            self.assertIsInstance(choice,SendPolicyChoice)
            self.assertEqual(cursor.execute('SELECT next_seq,reserved_count FROM qq_official_reply_windows').fetchone()[0],1)
            self.assertEqual(cursor.execute('SELECT COUNT(*) FROM delivery_attempts').fetchone()[0],0)
        with self.assertRaisesRegex(SendPolicyError,'rate_permit_required'), self.tx() as cursor:
            self.reserve(cursor,choice)
        with self.tx() as cursor:
            self.assertEqual(cursor.execute('SELECT COUNT(*) FROM delivery_attempts').fetchone()[0],0)

    def test_concurrent_window_claims_do_not_overspend_or_duplicate_sequences(self):
        with self.tx() as cursor:
            self.window(cursor,max_replies=5)
        barrier = threading.Barrier(6)

        def work(_):
            barrier.wait(timeout=5)
            with self.tx() as cursor:
                choice = self.check(cursor)
                if isinstance(choice,PolicyWait):
                    return choice.reason
                self.reserve(cursor,choice)
                return choice.msg_seq

        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(work,range(6)))
        self.assertEqual(sorted(value for value in results if isinstance(value,int)),[1,2,3,4,5])
        self.assertEqual(results.count('waiting_reply_window'),1)
        with self.tx() as cursor:
            row=cursor.execute('SELECT next_seq,reserved_count,used_count FROM qq_official_reply_windows').fetchone()
            self.assertEqual(tuple(row.values()),(6,5,0))

    def test_background_keeps_final_slot_and_progress_is_suppressed(self):
        with self.tx() as cursor:
            self.window(cursor,used_count=3,next_seq=4)
            for purpose in ('background_result','unsolicited'):
                self.assertEqual(self.check(cursor,purpose=purpose).reason,'waiting_reply_window')
            self.assertEqual(self.check(cursor,purpose='progress').reason,'progress_suppressed')
            choice=self.check(cursor)
            self.assertEqual(choice.msg_seq,4)
            self.reserve(cursor,choice)

    def test_seq_65535_burns_even_when_unsent_and_rollback_allocates_nothing(self):
        with self.tx() as cursor:
            self.window(cursor,next_seq=65535)
        with self.assertRaisesRegex(RuntimeError,'test rollback'), self.tx() as cursor:
            self.reserve(cursor,self.check(cursor))
            raise RuntimeError('test rollback')
        with self.tx() as cursor:
            self.assertEqual(cursor.execute('SELECT next_seq FROM qq_official_reply_windows').fetchone()[0],65535)
            attempt=self.reserve(cursor,self.check(cursor))
            settle_send_policy(cursor,token=self.token,attempt_id=attempt,result='proven_unsent',now=101)
            settle_send_policy(cursor,token=self.token,attempt_id=attempt,result='proven_unsent',now=101)
            row=cursor.execute('SELECT next_seq,reserved_count,used_count FROM qq_official_reply_windows').fetchone()
            self.assertEqual(tuple(row.values()),(65536,0,0))
            self.assertEqual(self.check(cursor).reason,'waiting_reply_window')

    def test_new_window_reuse_is_explicit_and_mode_change_requires_new_gate(self):
        with self.tx() as cursor:
            self.window(cursor,expires_at=100)
            newer=self.window(cursor,native='TEST_NEWER',issued_at=99)
            self.assertEqual(self.check(cursor).reason,'waiting_reply_window')
            self.assertEqual(self.check(cursor,allow_newer_windows=True).window_id,newer)
            self.allow_proactive(cursor)
            self.assertEqual(self.check(cursor).reason,'mode_changed')
            choice=self.check(cursor,rate_permit=self.permit(mode='proactive'))
            self.assertEqual(choice.send_mode,'proactive')

    def test_scope_and_epoch_isolation_cannot_borrow_same_native_id_windows(self):
        with self.tx() as cursor:
            self.window(cursor)
            group=EndpointKey(self.endpoint.account,'group',self.endpoint.native_conversation_id)
            self.provision(cursor,group)
            self.assertEqual(self.check(cursor,endpoint=group).reason,'waiting_reply_window')
            foreign=EndpointKey(AccountKey('qq-official','TEST_OTHER_APP','test'),'private','TEST_PERSON')
            self.provision(cursor,foreign)
            with self.assertRaisesRegex(SendPolicyError,'account_mismatch'):
                self.check(cursor,endpoint=foreign)
        self.lease.close()
        with self.assertRaises(LeaseLost),self.tx() as cursor:
            self.check(cursor)

    def test_concurrent_daily_quota_claims_share_one_durable_budget(self):
        with self.tx() as cursor:
            self.allow_proactive(cursor)
            self.quota(cursor,limit=1)
        barrier=threading.Barrier(2)

        def work(_):
            barrier.wait(timeout=5)
            with self.tx() as cursor:
                choice=self.check(cursor,rate_permit=self.permit(mode='proactive'))
                if isinstance(choice,PolicyWait):
                    return choice.reason
                return self.reserve(cursor,choice)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results=list(executor.map(work,range(2)))
        self.assertEqual(results.count('quota_reset_unverified'),1)
        with self.tx() as cursor:
            self.assertEqual(cursor.execute('SELECT reserved_count FROM qq_official_quota_periods').fetchone()[0],1)
            self.assertEqual(cursor.execute('SELECT COUNT(*) FROM delivery_attempt_quotas').fetchone()[0],1)

    def test_unverified_daily_limit_never_refills_on_restart_or_elapsed_time(self):
        with self.tx() as cursor:
            self.allow_proactive(cursor)
            spec=self.quota(cursor,used=1000)
        self.lease.close()
        replacement=DedicatedAccountLease(self.database.dsn,self.endpoint.account,schema=self.database.schema)
        self.addCleanup(replacement.close)
        token=replacement.acquire()
        with self.tx() as cursor:
            wait=self.check(cursor,token=token,now=86501,rate_permit=self.permit(mode='proactive'))
            self.assertEqual(wait,PolicyWait('quota_reset_unverified'))
            row=cursor.execute('SELECT quota_key,period_start_at,used_count FROM qq_official_quota_periods').fetchone()
            self.assertEqual(tuple(row.values()),(spec.quota_key,100,1000))

    def test_verified_day_changes_preserve_old_charges_and_late_acceptance(self):
        boundary=VerifiedDayBoundary(self.endpoint.account,0,'test_verified_day',period_seconds=100)
        with self.tx() as cursor:
            self.allow_proactive(cursor)
            choice=self.check(cursor,now=150,rate_permit=self.permit(mode='proactive'),verified_boundary=boundary)
            first=self.reserve(cursor,choice,now=150)
            settle_send_policy(cursor,token=self.token,attempt_id=first,result='accepted',now=160)
            choice=self.check(cursor,now=201,rate_permit=self.permit(mode='proactive'),verified_boundary=boundary)
            self.assertEqual(choice.quota_specs[0].period_start_at,200)
            second=self.reserve(cursor,choice,now=201)
            self.assertEqual(self.check(cursor,now=301,rate_permit=self.permit(mode='proactive'),verified_boundary=boundary).reason,'quota_unsettled_period')
            settle_send_policy(cursor,token=self.token,attempt_id=second,result='accepted',now=310)
            wait=self.check(cursor,now=311,rate_permit=self.permit(mode='proactive'),verified_boundary=boundary)
            self.assertEqual(wait,PolicyWait('quota_period_requires_review',400))
            self.assertIsInstance(self.check(cursor,now=401,rate_permit=self.permit(mode='proactive'),verified_boundary=boundary),SendPolicyChoice)
            self.assertEqual(cursor.execute('SELECT SUM(used_count),SUM(reserved_count) FROM qq_official_quota_periods').fetchone()[0],2)

    def test_confirmed_rejection_keeps_unproven_charges_while_unsent_refunds_once(self):
        with self.tx() as cursor:
            self.allow_proactive(cursor)
            attempt=self.reserve(cursor,self.check(cursor,rate_permit=self.permit(mode='proactive')))
            settle_send_policy(cursor,token=self.token,attempt_id=attempt,result='confirmed_unaccepted',now=101)
            self.assertEqual(cursor.execute('SELECT reserved_count FROM qq_official_quota_periods').fetchone()[0],1)
            settle_send_policy(cursor,token=self.token,attempt_id=attempt,result='proven_unsent',now=102)
            settle_send_policy(cursor,token=self.token,attempt_id=attempt,result='proven_unsent',now=102)
            self.assertEqual(cursor.execute('SELECT reserved_count FROM qq_official_quota_periods').fetchone()[0],0)

    def test_policy_evidence_can_release_confirmed_rejection_but_never_seq(self):
        with self.tx() as cursor:
            self.window(cursor)
            attempt=self.reserve(cursor,self.check(cursor))
            settle_send_policy(cursor,token=self.token,attempt_id=attempt,result='confirmed_unaccepted',now=101,release_confirmed_window=True)
            row=cursor.execute('SELECT next_seq,reserved_count FROM qq_official_reply_windows').fetchone()
            self.assertEqual(tuple(row.values()),(2,0))

    def test_verified_quota_refund_is_once_and_policy_lowering_preserves_history(self):
        with self.tx() as cursor:
            self.allow_proactive(cursor)
            self.quota(cursor,used=1)
            cursor.execute("UPDATE qq_official_quota_periods SET release_policy='confirmed_unaccepted'")
            attempt=self.reserve(cursor,self.check(cursor,rate_permit=self.permit(mode='proactive')))
            settle_send_policy(cursor,token=self.token,attempt_id=attempt,result='confirmed_unaccepted',now=101)
            settle_send_policy(cursor,token=self.token,attempt_id=attempt,result='confirmed_unaccepted',now=101)
            cursor.execute('UPDATE qq_official_quota_periods SET limit_count=1')
            row=cursor.execute('SELECT used_count,reserved_count FROM qq_official_quota_periods').fetchone()
            self.assertEqual(tuple(row.values()),(1,0))
            self.assertEqual(self.check(cursor,rate_permit=self.permit(mode='proactive')).reason,'quota_reset_unverified')

    def test_unknown_proactive_probe_is_single_use_and_never_for_otp_or_unsolicited(self):
        with self.tx() as cursor:
            for purpose in ('binding_otp','unsolicited'):
                self.assertEqual(self.check(cursor,purpose=purpose,allow_proactive_probe=True,rate_permit=self.permit(mode='proactive')).reason,'waiting_reply_window')
            self.assertEqual(self.check(cursor,rate_permit=self.permit(mode='proactive')).reason,'waiting_reply_window')
            choice=self.check(cursor,allow_proactive_probe=True,rate_permit=self.permit(mode='proactive'))
            self.assertTrue(choice.requires_probe)
            attempt=self.reserve(cursor,choice)
            settle_send_policy(cursor,token=self.token,attempt_id=attempt,result='proven_unsent',now=101)
            self.assertEqual(self.check(cursor,allow_proactive_probe=True,rate_permit=self.permit(mode='proactive')).reason,'waiting_reply_window')
            self.assertEqual(cursor.execute('SELECT proactive_probe_attempt_id FROM qq_official_endpoints').fetchone()[0],attempt)

    def test_server_cooldown_is_monotonic_per_mode_and_persistent(self):
        with self.tx() as cursor:
            self.window(cursor)
            extend_server_cooldown(cursor,token=self.token,endpoint=self.endpoint,send_mode='passive',blocked_until=150,now=100)
            extend_server_cooldown(cursor,token=self.token,endpoint=self.endpoint,send_mode='passive',blocked_until=120,now=101)
            self.assertEqual(read_server_cooldown(cursor,self.endpoint,'passive'),150)
            self.assertEqual(read_server_cooldown(cursor,self.endpoint,'proactive'),0)
            self.assertEqual(self.check(cursor),PolicyWait('server_cooldown',150))
        self.lease.close()
        replacement=DedicatedAccountLease(self.database.dsn,self.endpoint.account,schema=self.database.schema)
        self.addCleanup(replacement.close)
        token=replacement.acquire()
        with self.tx() as cursor:
            self.assertEqual(self.check(cursor,token=token,now=149),PolicyWait('server_cooldown',150))
            self.assertIsInstance(self.check(cursor,token=token,now=150),SendPolicyChoice)

    def test_late_receipt_can_settle_old_epoch_but_cannot_claim_old_unsent_proof(self):
        with self.tx() as cursor:
            self.window(cursor)
            attempt=self.reserve(cursor,self.check(cursor))
        self.lease.close()
        replacement=DedicatedAccountLease(self.database.dsn,self.endpoint.account,schema=self.database.schema)
        self.addCleanup(replacement.close)
        token=replacement.acquire()
        with self.assertRaisesRegex(SendPolicyError,'stale_unsent_proof'),self.tx() as cursor:
            settle_send_policy(cursor,token=token,attempt_id=attempt,result='proven_unsent',now=101)
        with self.tx() as cursor:
            settle_send_policy(cursor,token=token,attempt_id=attempt,result='accepted',now=101)
            settle_send_policy(cursor,token=token,attempt_id=attempt,result='accepted',now=102)
            row=cursor.execute('SELECT used_count,reserved_count FROM qq_official_reply_windows').fetchone()
            self.assertEqual(tuple(row.values()),(1,0))

    def test_permission_rejection_does_not_revoke_membership_or_newer_grants(self):
        with self.tx() as cursor:
            self.allow_proactive(cursor)
            attempt=self.reserve(cursor,self.check(cursor,rate_permit=self.permit(mode='proactive')))
            apply_policy_rejection(cursor,token=self.token,attempt_id=attempt,category='proactive_denied',now=101)
            apply_policy_rejection(cursor,token=self.token,attempt_id=attempt,category='proactive_denied',now=102)
            row=cursor.execute('SELECT proactive_state,capability_revision,membership_state FROM qq_official_endpoints').fetchone()
            self.assertEqual(tuple(row.values()),('denied',2,'present'))
            cursor.execute("UPDATE qq_official_endpoints SET proactive_state='allowed',capability_revision=3")
            apply_policy_rejection(cursor,token=self.token,attempt_id=attempt,category='proactive_denied',now=103)
            self.assertEqual(cursor.execute('SELECT proactive_state FROM qq_official_endpoints').fetchone()[0],'allowed')

    def test_duplicate_reservation_rolls_back_instead_of_double_charging(self):
        with self.tx() as cursor:
            self.window(cursor)
        with self.assertRaisesRegex(SendPolicyError,'window_reservation_changed'),self.tx() as cursor:
            choice=self.check(cursor)
            attempt=self.reserve(cursor,choice)
            reserve_send_policy(cursor,token=self.token,choice=choice,attempt_id=attempt,now=100)
        with self.tx() as cursor:
            self.assertEqual(cursor.execute('SELECT reserved_count FROM qq_official_reply_windows').fetchone()[0],0)
            self.assertEqual(cursor.execute('SELECT COUNT(*) FROM delivery_attempts').fetchone()[0],0)

    def test_ambiguous_keeps_charge_and_changed_capability_aborts_all_reservations(self):
        with self.tx() as cursor:
            self.window(cursor)
            attempt=self.reserve(cursor,self.check(cursor))
            settle_send_policy(cursor,token=self.token,attempt_id=attempt,result='ambiguous',now=101)
            self.assertEqual(cursor.execute('SELECT reserved_count FROM qq_official_reply_windows').fetchone()[0],1)
        with self.assertRaisesRegex(SendPolicyError,'endpoint_snapshot_changed'),self.tx() as cursor:
            choice=self.check(cursor)
            attempt=self.attempt(cursor,choice)
            cursor.execute('UPDATE qq_official_endpoints SET capability_revision=capability_revision+1')
            reserve_send_policy(cursor,token=self.token,choice=choice,attempt_id=attempt,now=100)

    def test_policy_representations_do_not_expose_credentials_or_target_ids(self):
        with self.tx() as cursor:
            self.window(cursor,native='TEST_SECRET_NATIVE_MESSAGE')
            choice=self.check(cursor,source='TEST_SECRET_NATIVE_MESSAGE')
            self.assertNotIn('TEST_SECRET_NATIVE_MESSAGE',repr(choice))
            self.assertNotIn('TEST_PERSON',repr(choice))
            self.assertNotIn('TEST_POLICY_APP',repr(choice))
            with self.assertRaisesRegex(SendPolicyError,'attempt_policy_mismatch'):
                reserve_send_policy(cursor,token=self.token,choice=replace(choice,msg_seq=2),
                                    attempt_id=self.attempt(cursor,choice),now=100)


if __name__ == '__main__':
    unittest.main()
