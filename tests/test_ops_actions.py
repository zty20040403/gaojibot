"""Real PostgreSQL owner/job/lease fences; no command or approval UI is executed."""
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier

from src.plugins.ai_chat.adapters.qq_official.ops_actions import OpsActions,Proposal
from src.plugins.ai_chat.ops_plans import OpsPlanError
from tests.ops_action_fixture import OpsFixture


class OpsActionTests(OpsFixture):
    def test_one_code_and_fixed_intent_per_request_with_ten_minute_bound(self):
        first = self.request(); again = self.request()
        self.assertRegex(first.code,r'^\d{6}$'); self.assertNotIn(first.code,repr(first))
        self.assertEqual(first.action['expires_at'],self.now+600)
        self.assertIsNone(again.code); self.assertEqual(again.action,first.action)
        self.assertEqual(first.action['code_hash'],self.ops.code_hash(first.action['action_id'],first.code))
        self.assertNotEqual(first.action['code_hash'],self.ops.code_hash('a'*32,first.code))
        with self.assertRaisesRegex(OpsPlanError,'conflict'):
            self.request(parameters={'unit':'TEST B.service'})
        with self.assertRaisesRegex(OpsPlanError,'pending'):
            self.request(request_id='TEST_OTHER')
        self.assertEqual(json.loads(first.action['parameters_json']),{'unit':'TEST_A.service'})
        self.assertNotIn('TEST_SECRET',json.dumps(first.action))

    def test_expiry_releases_question_but_does_not_reissue_old_code(self):
        first = self.request(); self.now += 600
        self.assertEqual(self.request().action['state'],'expired')
        self.assertEqual(self.request().action['code_hash'],'')
        self.assertIsNotNone(self.request(request_id='TEST_NEW').code)
        with self.tx() as cursor:
            self.assertEqual(self.ops.get(cursor,first.action['action_id'])['state'],'expired')
        self.query('UPDATE background_jobs SET deadline_at=?',(self.now+30,))
        with self.tx() as cursor:
            pending = cursor.execute("SELECT action_id FROM ops_actions WHERE state='sending'").fetchone()
            self.ops.close(cursor,pending['action_id'],state='cancelled',error='test_cancel')
        self.assertEqual(self.request(request_id='TEST_SHORT').action['expires_at'],self.now+30)

    def test_original_owner_route_job_and_stop_generation_remain_current(self):
        row = self.request().action
        changes = ("UPDATE qq_official_owner_grants SET state='revoked',revision=revision+1",
            'UPDATE qq_official_owner_grants SET revision=revision+1',
            "UPDATE qq_official_endpoints SET canonical_scope_key='TEST_SHARED'",
            "UPDATE background_jobs SET scope_key='TEST_SHARED'",'UPDATE qq_official_endpoints SET route_epoch=route_epoch+1',
            "UPDATE background_jobs SET state='cancelled'",'UPDATE durable_jobs SET lease_generation=lease_generation+1',
            'UPDATE durable_jobs SET lease_until=0',
            'UPDATE conversation_settings SET stop_generation=stop_generation+1')
        self.query('INSERT INTO conversation_settings(scope_key) VALUES (?)',(self.endpoint.scope.key,))
        for sql in changes:
            with self.subTest(sql=sql),self.tx() as cursor:
                cursor.execute('SAVEPOINT changed'); cursor.execute(sql)
                with self.assertRaises(PermissionError):
                    self.ops.check(cursor,row)
                cursor.execute('ROLLBACK TO SAVEPOINT changed'); self.ops.check(cursor,row)
        self.allowed = False
        with self.assertRaises(PermissionError):
            self.request()
        self.allowed = True
        with self.tx() as cursor,self.assertRaises(PermissionError):
            self.ops.check(cursor,row,binding='f'*64)

    def test_new_account_epoch_cancels_old_consent_instead_of_reissuing_it(self):
        original = self.request().action
        self.account_lease.close()
        self.token = self.lease().acquire()
        self.assertGreater(self.token.epoch,original['account_epoch'])
        self.ops = OpsActions(self.token,authority=self.authority,admission=lambda *args:True,
            pepper=b'TEST_OPS_PEPPER_'*3,clock=lambda:self.now)
        again = self.request()
        self.assertIsNone(again.code); self.assertEqual(again.action['state'],'cancelled')
        self.assertEqual(again.action['code_hash'],'')
        self.assertEqual(again.action['action_id'],original['action_id'])

    def test_parallel_requests_serialize_the_private_question(self):
        barrier = Barrier(2)
        def run(key):
            barrier.wait(timeout=5)
            try:
                return self.request(request_id=key)
            except OpsPlanError as error:
                return str(error)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run,('TEST_ONE','TEST_TWO')))
        self.assertEqual(sum(isinstance(item,Proposal) for item in results),1)
        self.assertIn('ops_question_already_pending',results)

    def test_cancelled_requests_are_audit_tombstones_and_not_execution_success(self):
        row = self.request().action
        with self.tx() as cursor:
            with self.assertRaises(ValueError):
                self.ops.close(cursor,row['action_id'],state='succeeded')
            closed = self.ops.close(cursor,row['action_id'],state='denied',error='owner_declined')
            self.assertEqual(closed['code_hash'],''); self.assertEqual(closed['finished_at'],self.now)
        self.assertIsNone(self.request().code); self.assertEqual(self.request().action['state'],'denied')
