from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

import nonebot

nonebot.init()

from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.agent_loop import AgentTrace
from src.plugins.ai_chat.turn_journal import TurnJournal


class TurnJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.journal = TurnJournal(":memory:")
        self.addCleanup(self.journal.close)
        self.group_a = ConversationScope("onebot-v11", "group", "100")
        self.group_b = ConversationScope("onebot-v11", "group", "200")

    def start(self, scope: ConversationScope, objective: str = "build project"):
        return self.journal.start_turn(
            scope,
            trigger_canonical_message_id=1,
            objective=objective,
            provider="deepseek-openai-compatible",
            model="deepseek-chat",
            prompt_version="v1",
            tool_catalog_version="tools-v1",
        )

    def test_records_normalized_tool_events_and_renders_digest(self) -> None:
        turn = self.start(self.group_a)
        self.journal.record_model_note(turn.turn_id, 1, "先检查项目")
        self.journal.record_tool_started(
            turn.turn_id,
            2,
            "sandbox_exec",
            {
                "command": "pytest",
                "password": "do-not-store",
                "authorization": "Bearer hidden-credential",
            },
            ("write:sandbox",),
            {
                "fingerprint": "abc123",
                "risk": "high",
                "idempotency": "non-idempotent",
            },
        )
        self.journal.record_tool_finished(
            turn.turn_id,
            2,
            "sandbox_exec",
            "succeeded",
            '{"ok":true,"stdout":"2 passed"}',
            ("write:sandbox",),
        )
        finished = self.journal.finish_turn(
            turn.turn_id,
            status="succeeded",
            final_text="项目测试完成",
        )

        self.assertEqual(finished.tool_call_count, 1)  # type: ignore[union-attr]
        rendered = self.journal.render_turn(self.group_a, turn.turn_ordinal)
        self.assertIn("sandbox_exec", rendered)
        self.assertIn("2 passed", rendered)
        self.assertIn("[REDACTED]", rendered)
        self.assertNotIn("do-not-store", rendered)
        self.assertNotIn("hidden-credential", rendered)
        self.assertIn("t#1", self.journal.render_recent_turns(self.group_a))
        replay = self.journal.replay_steps(self.group_a, turn.turn_ordinal)
        self.assertEqual(replay[1]["tool_name"], "sandbox_exec")
        self.assertEqual(replay[1]["metadata"]["fingerprint"], "abc123")
        self.assertEqual(replay[1]["arguments"]["password"], "[REDACTED]")
        self.assertEqual(self.journal.replay_steps(self.group_b, 1), ())

    def test_chat_only_turn_is_not_in_recent_worked_lines(self) -> None:
        turn = self.start(self.group_a, "hello")
        self.journal.finish_turn(
            turn.turn_id,
            status="succeeded",
            final_text="hi",
        )

        self.assertEqual(self.journal.render_recent_turns(self.group_a), "")
        self.assertIsNotNone(
            self.journal.render_turn(self.group_a, turn.turn_ordinal)
        )

    def test_recent_trace_summaries_exclude_conversation_content(self) -> None:
        turn = self.start(self.group_a, "private conversation text")
        self.journal.record_tool_started(
            turn.turn_id,
            1,
            "web_search",
            {"query": "private query"},
            (),
        )
        self.journal.record_tool_finished(
            turn.turn_id,
            1,
            "web_search",
            "succeeded",
            '{"ok":true}',
            (),
        )
        trace = AgentTrace(
            provider="provider-b",
            model="model-b",
            profile="fallback",
            trace_id="b" * 32,
            model_routes=[
                {"provider": "provider-a", "profile": "main", "model": "model-a"},
                {"provider": "provider-b", "profile": "fallback", "model": "model-b"},
            ],
        )
        self.journal.finish_turn(
            turn.turn_id,
            status="succeeded",
            final_text="private answer",
            trace_payload=trace.to_payload(),
            input_tokens=20,
            output_tokens=10,
            total_tokens=30,
        )

        summaries = self.journal.recent_trace_summaries()

        self.assertEqual(summaries[0]["trace_id"], "b" * 32)
        self.assertEqual(summaries[0]["tools"], ["web_search"])
        self.assertTrue(summaries[0]["fallback"])
        self.assertEqual(summaries[0]["total_tokens"], 30)
        self.assertNotIn("scope_key", summaries[0])
        self.assertNotIn("objective", summaries[0])
        self.assertNotIn("final_text", summaries[0])

    def test_routing_decisions_survive_archive_and_distinguish_requested_actual(self) -> None:
        turn = self.start(self.group_a, "hello")
        trace = AgentTrace(model_routing=[{
            "requested_profile": "qwen-local", "requested_model": "qwen3.8-27b",
            "actual_profile": "deepseek", "actual_model": "deepseek-test",
            "reason_code": "service_stopped", "reason": "Qwen 服务未启动",
            "fallback": True,
            "outcomes": [{"profile": "qwen-local", "status": "skipped", "reason_code": "service_stopped", "reason": "Qwen 服务未启动", "unexpected": "not public"}],
        }])
        self.journal.finish_turn(turn.turn_id, status="succeeded", trace_payload=trace.to_payload())
        summary = self.journal.recent_trace_summaries()[0]
        self.assertEqual(summary["requested_profile"], "qwen-local")
        self.assertEqual(summary["actual_profile"], "deepseek")
        self.assertEqual(summary["routing_reason"], "Qwen 服务未启动")
        self.assertTrue(summary["fallback"])
        self.assertNotIn("unexpected", summary["model_routing"][0]["outcomes"][0])

    def test_context_plan_is_scoped_and_visible_to_admin(self) -> None:
        turn = self.start(self.group_a, "你觉得呢")
        self.journal.record_context_plan(
            turn.turn_id,
            {
                "scope_key": self.group_a.key,
                "current_message_id": 12,
                "current_principal_id": 3,
                "focus_message_id": 10,
                "confidence": 0.87,
                "reason_codes": ["recent_question", "same_scope"],
                "related_message_ids": [11],
                "candidates": [
                    {
                        "message_id": 10,
                        "score": 89.0,
                        "reason_codes": ["recent_question"],
                    }
                ],
                "recall_route": {"mode": "follow_up", "confidence": 0.95},
                "adaptive_budget": {"focus": 330, "timeline": 275},
                "evidence_guard": {"sufficient": True, "confidence": 0.87},
                "topic_id": 10,
                "topic_message_ids": [10, 11],
                "topic_query": "这个部署方案",
                "recall_candidates": [
                    {
                        "handle": "msg#10",
                        "source": "relation_graph",
                        "selected": True,
                        "raw_score": 0.91,
                        "adjusted_score": 0.91,
                        "decision_codes": ["selected"],
                        "evidence_ids": [10],
                    },
                    {
                        "handle": "msg#9",
                        "source": "group_timeline",
                        "selected": False,
                        "raw_score": 0.31,
                        "adjusted_score": 0.31,
                        "decision_codes": ["below_source_threshold"],
                        "evidence_ids": [9],
                    },
                ],
                "resolver_version": "reference-rules-v1",
                "context_hash": "abc123",
            },
            created_at=123,
        )

        item = self.journal.recent_context_plans()[0]
        self.assertEqual(item["scope_key"], self.group_a.key)
        self.assertEqual(item["focus_message_id"], 10)
        self.assertEqual(item["reason_codes"], ["recent_question", "same_scope"])
        self.assertEqual(item["recall_route"]["mode"], "follow_up")
        self.assertEqual(item["adaptive_budget"]["focus"], 330)
        self.assertTrue(item["evidence_guard"]["sufficient"])
        self.assertEqual(item["topic_query"], "这个部署方案")
        self.assertEqual(item["topic_message_ids"], [10, 11])
        self.assertTrue(item["recall_candidates"][0]["selected"])
        feedback = self.journal.set_context_feedback(
            turn.turn_id,
            verdict="off_topic",
            note="应该关联 msg#11",
            actor="Kenneth",
            resource_version=3,
            updated_at=130,
        )
        self.assertEqual(feedback["verdict"], "off_topic")
        refreshed = self.journal.recent_context_plans()[0]
        self.assertEqual(refreshed["feedback"]["note"], "应该关联 msg#11")
        self.assertEqual(refreshed["feedback"]["resource_version"], 3)
        with self.assertRaises(ValueError):
            self.journal.record_context_plan(
                turn.turn_id,
                {
                    "scope_key": self.group_b.key,
                    "current_message_id": 12,
                    "resolver_version": "v1",
                    "context_hash": "bad",
                },
            )

    def test_recent_work_labels_the_member_who_started_it(self) -> None:
        turn = self.start(self.group_a, "部署项目")
        self.journal.record_context_plan(
            turn.turn_id,
            {
                "scope_key": self.group_a.key,
                "current_message_id": 12,
                "current_principal_id": 7,
                "focus_message_id": None,
                "resolver_version": "reference-rules-v1",
                "context_hash": "abc123",
            },
        )
        self.journal.record_tool_started(
            turn.turn_id,
            1,
            "sandbox_exec",
            {"command": "pytest"},
            ("write:sandbox",),
        )
        self.journal.record_tool_finished(
            turn.turn_id,
            1,
            "sandbox_exec",
            "succeeded",
            '{"ok":true}',
            ("write:sandbox",),
        )
        self.journal.finish_turn(turn.turn_id, status="succeeded")

        rendered = self.journal.render_recent_turns(self.group_a)
        self.assertIn("发起人 mention#7", rendered)
        self.assertIn("只能归属给标注的发起人", rendered)

    def test_usage_summary_is_scoped_and_respects_clear(self) -> None:
        first = self.start(self.group_a, "one")
        self.journal.finish_turn(
            first.turn_id,
            status="succeeded",
            input_tokens=12,
            output_tokens=8,
            total_tokens=20,
        )
        other = self.start(self.group_b, "other")
        self.journal.finish_turn(
            other.turn_id,
            status="succeeded",
            input_tokens=99,
            output_tokens=99,
            total_tokens=198,
        )
        self.assertEqual(
            self.journal.usage_summary(self.group_a),
            {
                "turns": 1,
                "input_tokens": 12,
                "output_tokens": 8,
                "total_tokens": 20,
            },
        )
        self.journal.hide_history(self.group_a)
        self.assertEqual(self.journal.usage_summary(self.group_a)["turns"], 0)

    def test_rejected_tool_request_is_counted_as_work(self) -> None:
        turn = self.start(self.group_a, "run too many tools")
        self.journal.record_tool_rejected(
            turn.turn_id,
            1,
            "web_search",
            {"query": "example"},
            '{"ok":false,"error":"too many calls"}',
            ("read",),
        )
        finished = self.journal.finish_turn(
            turn.turn_id,
            status="succeeded",
        )

        self.assertEqual(finished.tool_call_count, 1)  # type: ignore[union-attr]
        self.assertIn("REJECTED web_search", self.journal.render_turn(self.group_a, 1))
        self.assertIn("t#1", self.journal.render_recent_turns(self.group_a))

    def test_turn_handles_are_scoped_and_cannot_be_guessed_cross_group(self) -> None:
        turn = self.start(self.group_a)
        self.journal.finish_turn(turn.turn_id, status="succeeded")

        self.assertIsNotNone(
            self.journal.get_visible_turn(self.group_a, turn.turn_ordinal)
        )
        self.assertIsNone(
            self.journal.get_visible_turn(self.group_b, turn.turn_ordinal)
        )

    def test_send_link_resolves_only_inside_own_scope(self) -> None:
        turn = self.start(self.group_a)
        self.journal.finish_turn(turn.turn_id, status="succeeded")
        self.journal.link_send(turn.turn_id, 55)

        resolved = self.journal.find_turn_for_reply(self.group_a, 55)
        self.assertEqual(resolved.turn_id, turn.turn_id)  # type: ignore[union-attr]
        self.assertIsNone(self.journal.find_turn_for_reply(self.group_b, 55))

    def test_clear_hides_turns_and_reply_links(self) -> None:
        old = self.start(self.group_a)
        self.journal.finish_turn(old.turn_id, status="succeeded")
        self.journal.link_send(old.turn_id, 55)

        self.assertEqual(self.journal.hide_history(self.group_a), 1)
        self.assertIsNone(
            self.journal.get_visible_turn(self.group_a, old.turn_ordinal)
        )
        self.assertIsNone(self.journal.find_turn_for_reply(self.group_a, 55))

        new = self.start(self.group_a)
        self.assertGreater(new.turn_ordinal, old.turn_ordinal)
        self.assertIsNotNone(
            self.journal.get_visible_turn(self.group_a, new.turn_ordinal)
        )

    def test_archive_is_compressed_and_secret_turn_is_not_archived(self) -> None:
        safe_turn = self.start(self.group_a)
        trace = AgentTrace(model="deepseek-chat")
        trace.input_tokens = 12
        trace.output_tokens = 8
        trace.total_tokens = 20
        trace.messages.append({"role": "tool", "content": "ok"})
        self.journal.finish_turn(
            safe_turn.turn_id,
            status="succeeded",
            trace_payload=trace.to_payload(),
        )
        self.assertEqual(
            self.journal.archive_payload(safe_turn.turn_id)["messages"][0]["content"],  # type: ignore[index]
            "ok",
        )

        secret_turn = self.start(self.group_a, "secret")
        self.journal.finish_turn(
            secret_turn.turn_id,
            status="succeeded",
            trace_payload={
                "messages": [{"content": "ok"}],
                "access_token": "top-secret",
            },
        )
        self.assertIsNone(self.journal.archive_payload(secret_turn.turn_id))

    def test_replay_requires_matching_environment_and_strips_final_reasoning(self) -> None:
        turn = self.start(self.group_a, "build project")
        self.journal.record_tool_started(
            turn.turn_id,
            1,
            "sandbox_list",
            {},
            ("read",),
        )
        self.journal.record_tool_finished(
            turn.turn_id,
            1,
            "sandbox_list",
            "succeeded",
            '{"ok":true}',
            ("read",),
        )
        trace_payload = {
            "provider": "deepseek-openai-compatible",
            "model": "deepseek-chat",
            "messages": [
                {"role": "user", "content": "build project"},
                {
                    "role": "assistant",
                    "content": "checking",
                    "reasoning_content": "tool reasoning",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "sandbox_list",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
                {
                    "role": "assistant",
                    "content": "done",
                    "reasoning_content": "final private reasoning",
                },
            ],
        }
        self.journal.finish_turn(
            turn.turn_id,
            status="succeeded",
            final_text="done",
            trace_payload=trace_payload,
        )
        self.journal.link_send(turn.turn_id, 99)

        replay = self.journal.build_replay(
            self.group_a,
            turn.turn_ordinal,
            current_model="deepseek-chat",
            prompt_version="v1",
            tool_catalog_version="tools-v1",
        )

        self.assertEqual(replay.mode, "verbatim")
        self.assertEqual(replay.reason, "valid")
        self.assertIn(1, replay.covered_canonical_message_ids)
        self.assertIn(99, replay.covered_canonical_message_ids)
        assistants = [
            message for message in replay.messages if message["role"] == "assistant"
        ]
        self.assertEqual(assistants[0]["reasoning_content"], "tool reasoning")
        self.assertNotIn("reasoning_content", assistants[-1])
        self.assertIn("sandbox_list", self.journal.digest_for_turn(turn.turn_id))

        mismatch = self.journal.build_replay(
            self.group_a,
            turn.turn_ordinal,
            current_model="another-model",
            prompt_version="v1",
            tool_catalog_version="tools-v1",
        )
        self.assertEqual(mismatch.mode, "digest")
        self.assertEqual(mismatch.reason, "model-changed")

        provider_mismatch = self.journal.build_replay(
            self.group_a,
            turn.turn_ordinal,
            current_model="deepseek-chat",
            current_provider="anthropic:anthropic-messages",
            current_profile="default",
            prompt_version="v1",
            tool_catalog_version="tools-v1",
        )
        self.assertEqual(provider_mismatch.reason, "provider-changed")

        profile_mismatch = self.journal.build_replay(
            self.group_a,
            turn.turn_ordinal,
            current_model="deepseek-chat",
            current_provider="deepseek-openai-compatible",
            current_profile="another-profile",
            prompt_version="v1",
            tool_catalog_version="tools-v1",
        )
        self.assertEqual(profile_mismatch.reason, "profile-changed")

    def test_chat_only_turn_degrades_to_digest(self) -> None:
        turn = self.start(self.group_a, "just chat")
        self.journal.finish_turn(
            turn.turn_id,
            status="succeeded",
            final_text="hello",
            trace_payload={
                "messages": [
                    {"role": "user", "content": "just chat"},
                    {"role": "assistant", "content": "hello"},
                ]
            },
        )

        replay = self.journal.build_replay(
            self.group_a,
            turn.turn_ordinal,
            current_model="deepseek-chat",
            prompt_version="v1",
            tool_catalog_version="tools-v1",
        )

        self.assertEqual(replay.mode, "digest")
        self.assertEqual(replay.reason, "chat-only")
        self.assertIn("just chat", replay.digest_prefix)

    def test_digest_is_lazily_backfilled_for_existing_finished_turn(self) -> None:
        turn = self.start(self.group_a, "legacy work")
        self.journal.record_tool_started(
            turn.turn_id,
            1,
            "sandbox_list",
            {},
            ("read",),
        )
        with self.journal._transaction() as cursor:
            cursor.execute(
                """
                UPDATE agent_turns
                SET status = 'succeeded', finished_at = 100
                WHERE turn_id = ?
                """,
                (turn.turn_id,),
            )

        digest = self.journal.digest_for_turn(turn.turn_id)

        self.assertIn("legacy work", digest)
        self.assertIn("sandbox_list", digest)

    def test_explicit_startup_recovery_marks_inflight_turn_as_crashed(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "turns.sqlite3"
        first = TurnJournal(path)
        turn = first.start_turn(
            self.group_a,
            trigger_canonical_message_id=1,
            objective="unfinished",
            provider="deepseek-openai-compatible",
            model="deepseek-chat",
        )
        first.close()

        reopened = TurnJournal(path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get_turn_by_id(turn.turn_id).status, "running")
        reopened.recover_interrupted()
        recovered = reopened.get_turn_by_id(turn.turn_id)

        self.assertEqual(reopened.recovered_crashed_turns, 1)
        self.assertEqual(recovered.status, "crashed")  # type: ignore[union-attr]

    def test_explicit_recovery_marks_unfinished_tool_effect_outcome_unknown(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "turns.sqlite3"
        first = TurnJournal(path)
        turn = first.start_turn(
            self.group_a,
            trigger_canonical_message_id=1,
            objective="unfinished effect",
            provider="deepseek-openai-compatible",
            model="deepseek-chat",
        )
        first.record_tool_started(
            turn.turn_id,
            1,
            "sandbox_exec",
            {"command": "deploy"},
            ("write:sandbox",),
        )
        first.close()

        reopened = TurnJournal(path)
        self.addCleanup(reopened.close)
        self.assertEqual([event.state for event in reopened.events_for_turn(turn.turn_id)], ["started"])
        reopened.recover_interrupted()
        events = reopened.events_for_turn(turn.turn_id)

        self.assertEqual(reopened.recovered_unknown_effects, 1)
        self.assertEqual([event.state for event in events], ["started", "outcome-unknown"])

    def test_reopening_marks_dangling_send_on_finished_turn_unknown(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "turns.sqlite3"
        first = TurnJournal(path)
        turn = first.start_turn(
            self.group_a,
            trigger_canonical_message_id=1,
            objective="send result",
            provider="deepseek-openai-compatible",
            model="deepseek-chat",
        )
        first.finish_turn(turn.turn_id, status="succeeded", final_text="done")
        first.record_send_started(turn.turn_id, 1)
        first.close()

        reopened = TurnJournal(path)
        self.addCleanup(reopened.close)
        reopened.recover_interrupted()
        send_events = [
            event
            for event in reopened.events_for_turn(turn.turn_id)
            if event.tool_name == "reply_send"
        ]

        self.assertEqual(reopened.recovered_unknown_effects, 1)
        self.assertEqual(reopened.recovered_crashed_turns, 0)
        self.assertEqual(
            [event.state for event in send_events],
            ["started", "outcome-unknown"],
        )
        rendered = reopened.render_turn(self.group_a, turn.turn_ordinal)
        self.assertIn("OUTCOME-UNKNOWN", rendered)
        self.assertIn("服务在完成事件写入前中断", rendered)

    def test_recent_window_excludes_old_work(self) -> None:
        turn = self.journal.start_turn(
            self.group_a,
            trigger_canonical_message_id=1,
            objective="old",
            provider="deepseek-openai-compatible",
            model="deepseek-chat",
            started_at=int(time.time()) - 48 * 3600,
        )
        self.journal.record_tool_started(
            turn.turn_id,
            1,
            "web_search",
            {"query": "old"},
            ("read",),
        )
        self.journal.finish_turn(turn.turn_id, status="succeeded")

        self.assertEqual(
            self.journal.render_recent_turns(self.group_a, hours=24),
            "",
        )


if __name__ == "__main__":
    unittest.main()
