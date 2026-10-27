from __future__ import annotations

from tests.postgres_fixture import postgres_for_test

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import nonebot

nonebot.init()

from src.plugins.ai_chat.quota import UsageStore


class UsageStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = UsageStore(
            postgres_for_test(self),
            daily_call_limit=2,
            daily_input_token_limit=100,
            daily_output_token_limit=50,
        )
        self.addCleanup(self.store.close)
        self.now = int(
            datetime(2026, 8, 9, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        )

    def record(self, scope: str = "onebot-v11:group:1") -> None:
        self.store.record(
            scope_key=scope,
            source="turn",
            provider="test",
            model="model",
            input_tokens=40,
            output_tokens=10,
            occurred_at=self.now,
        )

    def test_daily_quota_is_scope_bound(self) -> None:
        self.record()
        status = self.store.status("onebot-v11:group:1", now=self.now)
        self.assertTrue(status.allowed)
        self.record()
        self.assertFalse(
            self.store.status("onebot-v11:group:1", now=self.now).allowed
        )
        self.assertTrue(
            self.store.status("onebot-v11:group:2", now=self.now).allowed
        )

    def test_override_and_daily_summary(self) -> None:
        self.record()
        self.store.set_override(
            "onebot-v11:group:1",
            call_limit=1,
            input_limit=0,
            output_limit=0,
        )
        self.assertFalse(
            self.store.status("onebot-v11:group:1", now=self.now).allowed
        )
        summary = self.store.daily_summary(days=365)
        self.assertTrue(any(item["source"] == "turn" for item in summary))


if __name__ == "__main__":
    unittest.main()
