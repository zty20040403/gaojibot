import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import tests.test_platform_chat_orchestrator as fixtures
from src.plugins.ai_chat.execution_control import LeaseLost

from src.plugins.ai_chat.application import chat_orchestrator as draft


class DurableOrchestratorTests(fixtures.PlatformChatOrchestratorTests):
    def setUp(self):
        super().setUp()
        self.orchestrator = draft.ChatOrchestrator(
            ports=self.ports, running_tasks=self.tasks, model_preferences=self.preferences,
            model_catalog=self.catalog, message_ledger=self.ledger, turn_journal=self.journal,
            logger=Mock(), prompt_version="TEST_PROMPT")
        self.lifecycle = SimpleNamespace(assert_current=AsyncMock(), begin=AsyncMock(return_value=23),
                                         finish=AsyncMock(), interrupted=AsyncMock())

    async def strict(self, **kwargs):
        return await self.orchestrator.run(self.message, self.platform, "问题",
                                          durable_lifecycle=self.lifecycle, **kwargs)

    async def test_strict_lifecycle_owns_journal_and_result_transaction(self):
        result = await self.strict(journal_turn_id=999)
        self.assertEqual(result.turn_id, 23)
        self.journal.start_turn.assert_not_called()
        self.journal.finish_turn.assert_not_called()
        self.assertEqual(self.agent.await_args.kwargs["journal_turn_id"], 23)
        self.lifecycle.begin.assert_awaited_once()
        self.lifecycle.finish.assert_awaited_once()
        self.assertEqual(self.lifecycle.finish.await_args.kwargs["final_text"], "测试回复")
        self.lifecycle.interrupted.assert_not_awaited()
        self.assertEqual(self.tasks.list_all(), [])

    async def test_begin_failure_never_falls_back_to_agent(self):
        self.lifecycle.begin.side_effect = RuntimeError("TEST_STORAGE")
        with self.assertRaisesRegex(RuntimeError, "TEST_STORAGE"):
            await self.strict()
        self.agent.assert_not_awaited()
        self.journal.start_turn.assert_not_called()
        self.lifecycle.interrupted.assert_awaited_once()
        self.assertEqual(self.tasks.list_all(), [])

    async def test_result_commit_failure_never_returns_success_or_restarts_model(self):
        self.lifecycle.finish.side_effect = RuntimeError("TEST_COMMIT")
        with self.assertRaisesRegex(RuntimeError, "TEST_COMMIT"):
            await self.strict()
        self.agent.assert_awaited_once()
        self.lifecycle.interrupted.assert_awaited_once()
        self.journal.finish_turn.assert_not_called()

    async def test_cancel_is_not_swallowed_for_durable_caller(self):
        self.agent.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.strict()
        self.lifecycle.interrupted.assert_awaited_once()
        self.lifecycle.finish.assert_not_awaited()
        self.assertEqual(self.tasks.list_all(), [])

    async def test_base_exception_lease_fence_survives_failed_cleanup(self):
        self.agent.side_effect = LeaseLost("TEST_LOST")
        self.lifecycle.interrupted.side_effect = RuntimeError("TEST_CLEANUP")
        with self.assertRaises(LeaseLost):
            await self.strict()
        self.lifecycle.finish.assert_not_awaited()
        self.assertEqual(self.tasks.list_all(), [])

    async def test_quota_rejection_is_durable_without_model_or_journal(self):
        self.orchestrator.usage_store = Mock(status=Mock(return_value=SimpleNamespace(allowed=False)))
        result = await self.strict()
        self.assertIsNone(result.turn_id)
        self.lifecycle.begin.assert_not_awaited()
        self.agent.assert_not_awaited()
        self.assertEqual(self.lifecycle.finish.await_args.kwargs["error_code"], "model_quota")
        self.assertEqual(self.tasks.list_all(), [])

    async def test_safe_model_failure_is_frozen_with_failure_code(self):
        self.agent.side_effect = draft.ChatFailure("安全提示", code="model_config")
        result = await self.strict()
        self.assertEqual(result.error_code, "model_config")
        self.assertEqual(self.lifecycle.finish.await_args.kwargs["final_text"], "安全提示")
        self.assertEqual(self.lifecycle.finish.await_args.kwargs["error_code"], "model_config")

    async def test_legacy_cancellation_still_returns_none(self):
        self.agent.side_effect = asyncio.CancelledError()
        result = await self.orchestrator.run(self.message, self.platform, "问题")
        self.assertIsNone(result)
        self.assertEqual(self.journal.finish_turn.call_args.kwargs["status"], "aborted")

    async def test_selected_override_binds_durable_begin_and_trace(self):
        profile = SimpleNamespace(provider_identity="TEST_OTHER_PROVIDER", model="TEST_OTHER_MODEL", name="TEST_OTHER_PROFILE")
        self.orchestrator.usage_store = Mock(status=Mock(return_value=SimpleNamespace(allowed=True)))
        await self.strict(selected_profile_override=profile)
        self.assertIs(self.lifecycle.begin.await_args.kwargs["selected_profile"], profile)
        trace = self.lifecycle.finish.await_args.kwargs["trace"]
        self.assertEqual((trace.provider, trace.model, trace.profile),
                         (profile.provider_identity, profile.model, profile.name))

    async def test_final_fence_failure_does_not_capture_output(self):
        async def answer(*args, **kwargs):
            self.lifecycle.assert_current.side_effect = LeaseLost("TEST_LOST")
            return "TEST_LATE_REPLY"
        self.agent.side_effect = answer
        with self.assertRaises(LeaseLost):
            await self.strict()
        self.lifecycle.finish.assert_not_awaited()
        self.lifecycle.interrupted.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
