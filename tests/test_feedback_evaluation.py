from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from src.context_feedback_evaluation import (
    compare_cases, digest, final_label, validate_export, write_private_json,
)


def fixture(label="negative"):
    scope = "onebot-v11:group:100"
    case = {"case_id": "turn#1", "label": label, "scope_key": scope,
            "question": {"id": 2, "scope_key": scope, "text": "you think?"},
            "messages": [{"id": 1, "scope_key": scope, "text": "context"}],
            "original_answer": "original", "unsupported_reason": ""}
    case["sha256"] = digest(case)
    return case


class FeedbackEvaluationTests(unittest.IsolatedAsyncioTestCase):
    def test_admin_overrides_asker_and_community_labels(self):
        votes = {"asker": -1, "others_up": 20, "others_down": 0}
        self.assertEqual(final_label({"verdict": "correct"}, votes), ("positive", "administrator"))
        self.assertEqual(final_label(None, votes), ("negative", "asker"))
        self.assertEqual(final_label(None, {**votes, "asker": None}), ("positive", "community"))
        self.assertIsNone(final_label(None, {"others_up": 2, "others_down": 2})[0])

    def test_private_output_refuses_overwrite_and_symlinks(self):
        with TemporaryDirectory() as folder:
            target = Path(folder) / "cases.json"
            write_private_json(target, {"private": True})
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                write_private_json(target, {})
            link = Path(folder) / "link.json"
            link.symlink_to(target)
            with self.assertRaises(FileExistsError):
                write_private_json(link, {})
            self.assertTrue(json.loads(target.read_text())["private"])

    def test_export_rejects_tampering_future_evidence_and_cross_scope(self):
        case = fixture()
        self.assertEqual(validate_export({"format": "gaoji-feedback-v1", "cases": [case]}), [case])
        for changed in ({"id": 3}, {"scope_key": "onebot-v11:group:200"}):
            mutated = copy.deepcopy(case)
            mutated["messages"][0].update(changed)
            mutated["sha256"] = digest({key: value for key, value in mutated.items() if key != "sha256"})
            with self.assertRaises(ValueError):
                validate_export({"format": "gaoji-feedback-v1", "cases": [mutated]})
        case["original_answer"] = "changed"
        with self.assertRaisesRegex(ValueError, "hash"):
            validate_export({"format": "gaoji-feedback-v1", "cases": [case]})

    async def test_paired_results_and_no_answer_or_label_leakage(self):
        async def generate(case):
            self.assertEqual(set(case), {"scope_key", "question", "messages"})
            return {"answer": "better"}

        async def judge(pair):
            self.assertNotIn("label", pair)
            return {"winner": "A" if pair["A"] == "better" else "B"}

        report = await compare_cases([fixture(), fixture("positive")], generate=generate, judge=judge)
        self.assertEqual(report["groups"]["negative"]["win"], 1)
        self.assertEqual(report["groups"]["positive"]["win"], 1)
        self.assertEqual(report["coverage"], 1)
        self.assertFalse(report["release_ready"])

    async def test_errors_and_unsupported_cases_are_not_counted_as_wins(self):
        async def fail(_):
            raise RuntimeError("secret provider url")
        unsupported = {**fixture(), "unsupported_reason": "tool_turn_not_replayed"}
        report = await compare_cases([fixture(), unsupported], generate=fail, judge=fail)
        self.assertEqual(report["groups"]["negative"]["error"], 1)
        self.assertEqual(report["groups"]["negative"]["skipped"], 1)
        self.assertEqual(report["coverage"], 0)
        self.assertNotIn("secret provider", json.dumps(report))

    async def test_positive_regression_and_tie(self):
        async def generate(_):
            return {"answer": "worse"}
        async def judge(pair):
            return {"winner": "A" if pair["A"] == "original" else "B"}
        report = await compare_cases([fixture("positive")], generate=generate, judge=judge)
        self.assertEqual(report["positive_regressions"], 1)
        async def tie(_):
            return {"winner": "tie"}
        report = await compare_cases([fixture()], generate=generate, judge=tie)
        self.assertEqual(report["groups"]["negative"]["tie"], 1)

    async def test_timeout_cancels_provider(self):
        cancelled = asyncio.Event()
        async def slow(_):
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()
        report = await compare_cases([fixture()], generate=slow, judge=slow, timeout=0.01)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(report["groups"]["negative"]["error"], 1)

    async def test_replay_uses_current_projection_and_only_selected_profiles(self):
        import nonebot
        nonebot.init()
        from tools.context_eval import replay_feedback
        from src.plugins.ai_chat.llm_gateway import _profile_scope

        case = fixture()
        case["question"].update(principal_id=10, at=1010)
        case["messages"][0].update(principal_id=10, direction="inbound", at=1000, reply_to=None)
        catalog = SimpleNamespace(resolve_preference=lambda name: SimpleNamespace(name=name, configured=True))
        gateway = SimpleNamespace(close=AsyncMock())

        async def candidate(question, history, **kwargs):
            self.assertEqual(_profile_scope.get(), frozenset({"candidate"}))
            self.assertIn("context", kwargs["group_context"])
            self.assertNotIn("original", kwargs["group_context"])
            kwargs["trace"].profile = "candidate"
            return "new answer"

        async def reviewer(system, body, **kwargs):
            self.assertEqual(_profile_scope.get(), frozenset({"judge"}))
            self.assertIn("untrusted", body)
            self.assertIn("original", body)
            kwargs["trace"].profile = "judge"
            return {"winner": "tie", "reason": "fixture"}

        with patch("src.plugins.ai_chat.deepseek._runtime", return_value=(catalog, gateway)), \
             patch("src.plugins.ai_chat.deepseek.ask_deepseek", side_effect=candidate), \
             patch("src.plugins.ai_chat.deepseek.ask_deepseek_json", side_effect=reviewer):
            report = await replay_feedback([case], "candidate", "judge")
        self.assertEqual(report["groups"]["negative"]["tie"], 1)
        gateway.close.assert_awaited_once()
