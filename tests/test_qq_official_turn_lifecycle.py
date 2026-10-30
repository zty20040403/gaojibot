import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official import turn_source as execution
from tests import qq_official_turn_fixture as fixture
from src.plugins.ai_chat.adapters.qq_official.turns import OfficialTurnLifecycle
from src.plugins.ai_chat.adapters.qq_official.turn_source import load_turn_input
from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.agent_loop import AgentTrace
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.quota import UsageStore

from src.plugins.ai_chat import turn_journal as journal_module


class LifecycleTests(fixture.ExecutionFixture):
    def setUp(self):
        super().setUp()
        self.journal = journal_module.TurnJournal(self.database)
        self.deliveries = DeliveryStore(self.database, recover_interrupted=False)
        self.usage = UsageStore(self.database)
        for store in (self.journal, self.deliveries, self.usage):
            self.addCleanup(store.close)
        self.calls = DatabaseCalls()
        self.addCleanup(lambda: asyncio.run(self.calls.close()))
        self.handle = self.claim().handle
        self.life = OfficialTurnLifecycle(api=execution, token=self.token, handle=self.handle,
            job=self.job, admission=self.admission, transactions=self.deliveries, journal=self.journal,
            database_calls=self.calls, usage=self.usage, clock=lambda: self.now)
        with self.tx() as cursor:
            self.input = load_turn_input(cursor, execution_api=execution, **self.args(self.handle))
        self.profile = SimpleNamespace(provider_identity="TEST", model="TEST_MODEL", name="TEST_PROFILE")
        self.trace = AgentTrace(provider="TEST", model="TEST_MODEL", profile="TEST_PROFILE",
                                input_tokens=3, output_tokens=4, total_tokens=7)

    async def begin_lifecycle(self):
        return await self.life.begin(inbound=self.input, selected_profile=self.profile,
                                     objective="TEST question", prompt_version="TEST")

    async def finish_lifecycle(self, turn_id):
        await self.life.finish(turn_id=turn_id, status="succeeded", trace=self.trace,
                               final_text="TEST reply", error_code="", scope_key=self.handle.scope_key)

    def test_lifecycle_result_journal_usage_commit_once(self):
        async def run():
            turn = await self.begin_lifecycle()
            self.trace.model, self.trace.profile = 'TEST_FALLBACK_MODEL', 'TEST_FALLBACK_PROFILE'
            await self.finish_lifecycle(turn)
        asyncio.run(run())
        self.assertEqual(self.query("SELECT state FROM qq_official_turn_executions")[0]["state"], "result_ready")
        self.assertEqual(self.query("SELECT status FROM agent_turns")[0]["status"], "succeeded")
        self.assertEqual(len(self.query("SELECT * FROM usage_events")), 1)
        self.assertEqual(self.query("SELECT model,profile FROM agent_turns"),
            [{"model": "TEST_FALLBACK_MODEL", "profile": "TEST_FALLBACK_PROFILE"}])

    def test_lifecycle_journal_failure_rolls_back_frozen_result_and_usage(self):
        async def run():
            turn = await self.begin_lifecycle()
            with patch.object(self.journal, "finish_turn", side_effect=RuntimeError("TEST_JOURNAL")):
                with self.assertRaisesRegex(RuntimeError, "TEST_JOURNAL"):
                    await self.finish_lifecycle(turn)
            self.assertEqual(self.query("SELECT state FROM qq_official_turn_executions")[0]["state"], "executing")
            self.assertEqual(self.query("SELECT * FROM usage_events"), [])
            await self.finish_lifecycle(turn)
        asyncio.run(run())
        self.assertEqual(len(self.query("SELECT * FROM usage_events")), 1)

    def test_lifecycle_legacy_recovery_cannot_touch_account_owned_running_turn(self):
        async def run():
            turn = await self.begin_lifecycle()
            legacy = self.journal.start_turn(self.endpoint.scope, trigger_canonical_message_id=None,
                objective="TEST_LEGACY", provider="TEST", model="TEST")
            self.journal.recover_interrupted()
            self.assertEqual(self.journal.get_turn_by_id(turn).status, "running")
            self.assertEqual(self.journal.get_turn_by_id(legacy.turn_id).status, "crashed")
        asyncio.run(run())


    def test_lifecycle_quota_refusal_freezes_without_fake_model_journal(self):
        async def run():
            await self.life.finish(turn_id=None, status="succeeded", trace=None, final_text="TEST_QUOTA",
                                   error_code="model_quota", scope_key=self.handle.scope_key)
        asyncio.run(run())
        self.assertEqual(self.query("SELECT * FROM agent_turns"), [])
        self.assertEqual(self.query("SELECT state FROM qq_official_turn_executions")[0]["state"], "result_ready")


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(LifecycleTests(name) for name in dir(LifecycleTests) if name.startswith("test_lifecycle_"))


if __name__ == "__main__":
    unittest.main()
