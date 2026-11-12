"""API reservations and their audit share a real PostgreSQL transaction."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest

from src.plugins.ai_chat.host_api_calls import HostAPICalls
from tests.postgres_fixture import isolated_postgres_for_test


class HostAPICallTests(unittest.TestCase):
    def setUp(self):
        self.now = 360000
        self.database = isolated_postgres_for_test(self)
        self.store = HostAPICalls(self.database,clock=lambda:self.now)

    def begin(self, **changes):
        return self.store.begin(**(dict(scope_key='TEST_PHYSICAL_A',api_name='test',caller_ref='job#1',
            method='POST',path='/repos/TEST',limit=3)|changes))

    def test_concurrent_limit_is_exact_and_does_not_reset_with_a_new_store(self):
        barrier = Barrier(8)
        def call(index):
            barrier.wait(timeout=10)
            return self.begin(caller_ref=f'job#{index}')['status_code']
        with ThreadPoolExecutor(max_workers=8) as executor:
            codes = list(executor.map(call,range(8)))
        self.assertEqual(codes.count(0),3)
        self.assertEqual(codes.count(429),5)
        self.store = HostAPICalls(self.database,clock=lambda:self.now)
        self.assertEqual(self.begin()['status_code'],429)
        self.assertEqual(self.begin(scope_key='TEST_PHYSICAL_B')['status_code'],0)
        self.assertEqual(self.begin(api_name='another')['status_code'],0)
        self.now += 3600
        self.assertEqual(self.begin()['status_code'],0)
        self.assertEqual(len(self.store.snapshot()['items']),12)

    def test_audit_failure_rolls_back_the_quota_reservation(self):
        with self.assertRaises(Exception):
            self.begin(caller_ref=None)  # NOT NULL failure after reservation.
        with self.store.transaction() as cursor:
            self.assertEqual(cursor.execute('SELECT * FROM host_api_quota').fetchall(),[])
            self.assertEqual(cursor.execute('SELECT * FROM host_api_calls').fetchall(),[])

    def test_failed_calls_consume_slots_and_terminal_audit_is_write_once(self):
        row = self.begin(limit=1)
        self.assertTrue(self.store.finish(row['call_id'],status_code=503,outcome='failed',error_code='api_upstream_unavailable'))
        self.assertFalse(self.store.finish(row['call_id'],status_code=200,outcome='completed'))
        self.assertEqual(self.begin(limit=1)['status_code'],429)
        data = self.store.snapshot()
        self.assertEqual(data['counts'],{'failed':1,'denied':1})
        self.assertEqual(data['items'][1]['status_code'],503)

    def test_keyset_pagination_and_exact_filters_do_not_skip_after_new_calls(self):
        for _ in range(3):
            self.begin(limit=0)
        self.begin(scope_key='TEST_PHYSICAL_B')
        self.begin(api_name='another')
        first = self.store.snapshot(limit=2,scope_key='TEST_PHYSICAL_A',api_name='test')
        self.assertEqual([row['call_id'] for row in first['items']],[3,2])
        self.begin(limit=0)
        second = self.store.snapshot(limit=2,scope_key='TEST_PHYSICAL_A',api_name='test',before=first['next_cursor'])
        self.assertEqual([row['call_id'] for row in second['items']],[1])
        self.assertIsNone(second['next_cursor'])
        self.assertEqual(second['counts'],{'pending':4})
        self.assertEqual(self.store.snapshot(scope_key="TEST' OR TRUE --")['items'],[])

    def test_denials_are_audited_without_query_and_without_reserving(self):
        row = self.begin(path='/TEST?key=TEST_PRIVATE',rejection=(403,'api_operation_denied'))
        self.assertEqual(row['status_code'],403)
        self.assertEqual(self.store.snapshot()['items'][0]['path'],'<invalid>')
        self.assertNotIn('TEST_PRIVATE',str(self.store.snapshot()))
        with self.store.transaction() as cursor:
            self.assertEqual(cursor.execute('SELECT * FROM host_api_quota').fetchall(),[])
        self.assertEqual(self.begin(limit=1)['status_code'],0)
