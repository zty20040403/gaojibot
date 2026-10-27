from __future__ import annotations

import unittest

from tools.benchmark_event_loop import validate_test_dsn


class EventLoopBenchmarkTests(unittest.TestCase):
    def test_only_explicit_loopback_test_databases_are_accepted(self):
        for dsn in (
            "postgresql://127.0.0.1:25432/gaoji_test",
            "host=::1 dbname=gaoji_acceptance",
        ):
            with self.subTest(dsn=dsn):
                validate_test_dsn(dsn)

    def test_missing_remote_production_and_alternate_routes_are_rejected(self):
        for dsn in (
            "",
            "dbname=gaoji_test",
            "host=localhost dbname=gaoji_test",
            "host=100.64.0.1 dbname=gaoji_test",
            "host=127.0.0.1 dbname=gaoji",
            "host=127.0.0.1,100.64.0.1 dbname=gaoji_test",
            "host=127.0.0.1 hostaddr=100.64.0.1 dbname=gaoji_test",
            "host=127.0.0.1 service=production dbname=gaoji_test",
        ):
            with self.subTest(dsn=dsn), self.assertRaises(ValueError):
                validate_test_dsn(dsn)
