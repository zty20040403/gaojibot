from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import nonebot

nonebot.init()

from nonebot.adapters.onebot.v11 import ActionFailed, NoticeEvent

from src.bot_storage import DatabaseError
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.matchers import _is_proactive_feedback_notice, proactive_chat
from src.plugins.ai_chat.proactive import ProactiveDecision
from src.plugins.ai_chat.proactive_learning import (
    PRIORS,
    ProactiveLearningStore,
    adaptive_for_group,
    is_rejection,
)
from tests.handler_fixture import HandlerTestCaseMixin
from tests.postgres_fixture import postgres_for_test
from tests.test_proactive import group_event


class ProactiveLearningTests(unittest.TestCase):
    def setUp(self):
        self.database = postgres_for_test(self)
        self.store = ProactiveLearningStore(self.database, feedback_seconds=60)
        self.addCleanup(self.store.close)
        self.scope = ConversationScope("onebot-v11", "group", "100")

    def trial(self, *, trigger="1", native="-90", threshold=94):
        identifier = self.store.begin(
            self.scope, "999", trigger, threshold, 100, now=1000
        )
        if native is not None:
            self.store.sent(identifier, native, now=1010)
        return identifier

    def feedback(self, **kwargs):
        values = dict(
            native_message_id="-90",
            actor_id="7",
            kind="reaction",
            value=1,
            source_id="76",
            occurred_at=1020,
            now=1020,
        )
        values.update(kwargs)
        return self.store.feedback(self.scope, "999", **values)

    def outcome(self, identifier):
        return self.store._connection.execute(
            "SELECT * FROM proactive_trials WHERE trial_id=?", (identifier,)
        ).fetchone()

    def arm(self, threshold=94, *, scope=None, bot="999"):
        return next(
            item
            for item in self.store.arm_stats(scope or self.scope, bot)
            if item["threshold"] == threshold
        )

    def test_defaults_require_flag_and_explicit_group(self):
        settings = SimpleNamespace(
            proactive_adaptive=False, proactive_adaptive_groups={100}
        )
        self.assertFalse(adaptive_for_group(settings, 100))
        settings.proactive_adaptive = True
        self.assertTrue(adaptive_for_group(settings, 100))
        self.assertFalse(adaptive_for_group(settings, 200))
        settings.proactive_adaptive_groups = set()
        self.assertFalse(adaptive_for_group(settings, 100))

    def test_bounded_window_and_valid_group_bot_identity(self):
        for seconds, expected in ((1, 60), (99999, 3600)):
            store = ProactiveLearningStore(self.database, feedback_seconds=seconds)
            try:
                self.assertEqual(store.feedback_seconds, expected)
            finally:
                store.close()
        invalid = [
            ConversationScope("onebot-v11", "private", "100"),
            ConversationScope("matrix", "group", "100"),
            ConversationScope("onebot-v11", "group", "0"),
            replace(self.scope, bot_native_user_id="998"),
        ]
        for scope in invalid:
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                self.store.arm_stats(scope, "999")
        with self.assertRaises(ValueError):
            self.store.arm_stats(self.scope, "0")

    def test_sampling_uses_group_priors_and_conservative_tie_break(self):
        sample = Mock(side_effect=[0.8, 0.6, 0.4])
        self.assertEqual(
            self.store.choose_threshold(self.scope, "999", sample=sample), 90
        )
        self.assertEqual(
            [call.args for call in sample.call_args_list], list(PRIORS.values())
        )
        self.assertEqual(
            self.store.choose_threshold(self.scope, "999", sample=lambda *_: 0.5), 98
        )

    def test_deduplicated_trigger_and_only_above_threshold_trials(self):
        identifier = self.trial()
        self.assertIsNone(self.store.begin(self.scope, "999", "1", 94, 100, now=1020))
        for threshold, score in ((91, 100), (94, 93), (98, 101)):
            with (
                self.subTest(threshold=threshold, score=score),
                self.assertRaises(ValueError),
            ):
                self.store.begin(self.scope, "999", "2", threshold, score)
        self.assertEqual(self.outcome(identifier)["native_message_id"], "-90")

    def test_positive_reward_and_once_only_settlement_survive_reopen(self):
        identifier = self.trial()
        self.assertTrue(self.feedback())
        self.assertFalse(self.feedback())
        self.store.close()
        self.store = ProactiveLearningStore(self.database, feedback_seconds=60)
        self.addCleanup(self.store.close)
        self.assertEqual(self.store.settle_due(now=1071), 1)
        self.assertEqual(self.store.settle_due(now=1072), 0)
        self.assertEqual(
            self.arm(), dict(threshold=94, alpha=3, beta=3, observations=1)
        )
        self.assertEqual(self.outcome(identifier)["outcome"], "positive_reaction")

    def test_no_feedback_is_low_weight_not_disapproval(self):
        identifier = self.trial()
        self.store.settle_due(now=1071)
        row = self.outcome(identifier)
        self.assertEqual(
            (row["reward"], row["weight"], row["outcome"]), (0.5, 0.1, "no_feedback")
        )
        self.assertAlmostEqual(self.arm()["alpha"], 2.05)
        self.assertAlmostEqual(self.arm()["beta"], 3.05)

    def test_explicit_follow_up_is_weaker_than_reaction(self):
        identifier = self.trial()
        self.assertTrue(self.feedback(kind="follow_up", source_id="8"))
        self.store.settle_due(now=1071)
        self.assertEqual(self.outcome(identifier)["weight"], 0.5)
        self.assertEqual(self.arm()["alpha"], 2.5)

    def test_negative_overrides_positive_without_vote_count_amplification(self):
        identifier = self.trial()
        for actor in range(1, 11):
            self.feedback(actor_id=str(actor))
        self.feedback(actor_id="11", kind="rejected", value=-1, source_id="11")
        self.store.settle_due(now=1071)
        self.assertEqual(self.outcome(identifier)["outcome"], "negative")
        self.assertEqual(
            self.arm(), dict(threshold=94, alpha=2, beta=4, observations=1)
        )

    def test_reaction_replacement_retraction_and_out_of_order_events(self):
        identifier = self.trial()
        self.feedback()
        self.assertTrue(
            self.feedback(value=-1, source_id="77", occurred_at=1021, now=1021)
        )
        self.assertFalse(self.feedback(value=0, occurred_at=1022, now=1022))
        self.assertTrue(
            self.feedback(value=0, source_id="77", occurred_at=1023, now=1023)
        )
        self.assertFalse(
            self.feedback(value=-1, source_id="77", occurred_at=1023, now=1023)
        )
        self.assertFalse(
            self.feedback(value=-1, source_id="77", occurred_at=1021, now=1023)
        )
        self.store.settle_due(now=1071)
        self.assertEqual(self.outcome(identifier)["outcome"], "no_feedback")

    def test_retraction_before_add_leaves_tombstone(self):
        identifier = self.trial()
        self.assertTrue(self.feedback(value=0))
        self.assertFalse(self.feedback())
        self.assertTrue(self.feedback(occurred_at=1021, now=1021))
        self.store.settle_due(now=1071)
        self.assertEqual(self.outcome(identifier)["outcome"], "positive_reaction")

    def test_scope_bot_time_and_sender_filters(self):
        self.trial()
        invalid = [
            dict(native_message_id="-91"),
            dict(actor_id="999"),
            dict(actor_id="0"),
            dict(actor_id="not-a-user"),
            dict(occurred_at=1009),
            dict(occurred_at=1071, now=1071),
            dict(now=1071),
            dict(occurred_at=1400),
            dict(native_message_id=None),
            dict(kind="chat"),
            dict(value=2),
        ]
        for changed in invalid:
            with self.subTest(changed=changed):
                self.assertFalse(self.feedback(**changed))
        other = replace(self.scope, native_conversation_id="200")
        args = dict(
            native_message_id="-90",
            actor_id="7",
            kind="reaction",
            value=1,
            source_id="76",
            occurred_at=1020,
            now=1020,
        )
        self.assertFalse(self.store.feedback(other, "999", **args))
        self.assertFalse(self.store.feedback(self.scope, "998", **args))
        self.feedback()
        self.store.settle_due(now=1071)
        self.assertEqual(self.arm(scope=other)["observations"], 0)
        self.assertEqual(self.arm(bot="998")["observations"], 0)

    def test_unacknowledged_failed_and_expired_pending_are_not_training_samples(self):
        ids = []
        for index, status in enumerate(("unknown", "failed", "pending", "missing_id")):
            identifier = self.trial(trigger=str(index), native=None)
            ids.append(identifier)
            if status == "missing_id":
                self.store.sent(identifier, "", now=1010)
            elif status != "pending":
                self.store.unsent(identifier, status)
        self.assertEqual(self.store.settle_due(now=1601), 0)
        self.assertEqual(self.arm()["observations"], 0)
        self.assertEqual(
            [self.outcome(item)["status"] for item in ids],
            ["unknown", "failed", "unknown", "unknown"],
        )

    def test_duplicate_send_receipt_cannot_extend_window_or_overwrite_id(self):
        identifier = self.trial()
        self.store.sent(identifier, "-91", now=1050)
        self.store.unsent(identifier, "failed")
        row = self.outcome(identifier)
        self.assertEqual(
            (row["native_message_id"], row["expires_at"], row["status"]),
            ("-90", 1070, "sent"),
        )

    def test_concurrent_settlement_trains_each_trial_once(self):
        for index in range(8):
            self.trial(trigger=str(index), native=str(index))
        other = ProactiveLearningStore(self.database)
        self.addCleanup(other.close)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(lambda store: store.settle_due(now=1071), [self.store, other])
            )
        self.assertEqual(sum(results), 8)
        self.assertEqual(self.arm()["observations"], 8)

    def test_directed_follow_up_uses_latest_trial_but_reply_id_is_exact(self):
        first = self.trial()
        second = self.trial(trigger="2", native=None)
        self.store.sent(second, "-91", now=1011)
        self.assertTrue(
            self.feedback(native_message_id=None, kind="follow_up", source_id="8")
        )
        self.assertTrue(
            self.feedback(
                native_message_id="-90", kind="rejected", value=-1, source_id="9"
            )
        )
        self.store.settle_due(now=1072)
        self.assertEqual(self.outcome(first)["outcome"], "negative")
        self.assertEqual(self.outcome(second)["outcome"], "addressed_follow_up")

    def notice(self, **changes):
        return dict(
            post_type="notice",
            notice_type="group_msg_emoji_like",
            self_id=999,
            group_id=100,
            user_id=7,
            message_id=-90,
            time=1020,
            is_add=True,
            likes=[dict(emoji_id="76", count=999)],
            **changes,
        )

    def observe(self, payload, **changes):
        options = dict(
            bot_id="999", positive_ids=("76",), negative_ids=("77",), now=1020
        )
        options.update(changes)
        return self.store.observe_notice(payload, **options)

    def test_notice_roundtrip_and_recall_by_other_user(self):
        identifier = self.trial()
        notice = NoticeEvent(**self.notice())
        self.assertTrue(_is_proactive_feedback_notice(notice))
        self.assertTrue(self.observe(notice.model_dump()))
        recall = {
            **self.notice(),
            "notice_type": "group_recall",
            "user_id": 999,
            "operator_id": 8,
        }
        self.assertFalse(self.observe({**recall, "operator_id": 999}))
        self.assertFalse(self.observe({**recall, "user_id": 7}))
        self.assertTrue(self.observe(recall))
        self.store.settle_due(now=1071)
        self.assertEqual(self.outcome(identifier)["outcome"], "negative")

    def test_malformed_or_ambiguous_notice_does_not_train(self):
        self.trial()
        for changes in (
            {"self_id": 998},
            {"group_id": -1},
            {"time": None},
            {"message_id": "bad"},
            {"is_add": "false"},
            {"likes": None},
            {"likes": [{"emoji_id": "76"}] * 51},
            {"likes": [{"emoji_id": "76"}, {"emoji_id": "77"}]},
            {"notice_type": "group_increase"},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(self.observe({**self.notice(), **changes}))
        self.assertFalse(self.observe(self.notice(), positive_ids=()))
        self.assertFalse(self.observe(self.notice(), negative_ids=("76",)))

    def test_rejection_requires_direct_rejection_not_quoted_discussion(self):
        for text in ("闭嘴", "请闭嘴！", "stop talking", "shut up!"):
            self.assertTrue(is_rejection(text))
        for text in ("他说闭嘴是不礼貌的", "这个功能叫闭嘴", "不要说话题无关的内容"):
            self.assertFalse(is_rejection(text))


class AdaptiveProactiveHandlerTests(
    HandlerTestCaseMixin, unittest.IsolatedAsyncioTestCase
):
    def setUp(self):
        super().setUp()
        self.store = ProactiveLearningStore(
            postgres_for_test(self), feedback_seconds=60
        )
        self.app_context.proactive_learning = self.store
        self.app_context.settings = replace(
            self.app_context.settings,
            proactive_enabled=True,
            proactive_adaptive=True,
            proactive_adaptive_groups={100},
            proactive_gate_percent=100,
            proactive_max_checks_per_hour=100,
            voice_enabled=False,
            feedback_positive_emoji_ids=("76",),
            feedback_negative_emoji_ids=("77",),
        )
        self.clock = patch(
            "src.plugins.ai_chat.proactive_learning.time.time", return_value=1000
        )
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.decide = AsyncMock(return_value=ProactiveDecision(95, "这点有道理", False))
        self.choose = patch.object(self.store, "choose_threshold", return_value=94)
        self.choose_mock = self.choose.start()
        self.addCleanup(self.choose.stop)
        decision_patch = patch.object(
            self.handlers.triggers, "_generate_proactive_reply", self.decide
        )
        decision_patch.start()
        self.addCleanup(decision_patch.stop)

    def event(self, message_id=1, text="这个方案值得认真讨论"):
        event = group_event(message_id, text)
        event.time = 1000
        return event

    def trials(self):
        return self.store._connection.execute(
            "SELECT * FROM proactive_trials ORDER BY created_at"
        ).fetchall()

    async def send(self, event=None):
        await self.handlers.triggers.handle_proactive_chat(
            SimpleNamespace(self_id="999"), event or self.event()
        )

    async def test_acknowledged_send_persisted_once_and_recorded_in_ledger(self):
        self.app_context.message_ledger = MessageLedger(postgres_for_test(self))
        with patch.object(
            proactive_chat, "send", AsyncMock(return_value={"message_id": -90})
        ) as sender:
            await self.send()
            await self.send()
        sender.assert_awaited_once()
        self.assertEqual(len(self.trials()), 1)
        self.assertEqual(self.trials()[0]["status"], "sent")
        self.assertEqual(self.trials()[0]["threshold"], 94)
        self.assertEqual(self.decide.await_args.kwargs["threshold"], 94)
        scope = ConversationScope("onebot-v11", "group", "100")
        self.assertIsNotNone(
            self.app_context.message_ledger.canonical_id_for_native(scope, "-90")
        )

    async def test_disabled_global_and_direct_mentions_never_sample_or_send(self):
        with patch.object(proactive_chat, "send", AsyncMock()) as sender:
            self.app_context.settings = replace(
                self.app_context.settings, proactive_enabled=False
            )
            await self.send()
            self.app_context.settings = replace(
                self.app_context.settings, proactive_enabled=True
            )
            await self.send(self.event(2, "[CQ:at,qq=999] 这个方案值得认真讨论"))
        self.choose_mock.assert_not_called()
        self.decide.assert_not_awaited()
        sender.assert_not_awaited()

    async def test_non_opted_in_group_uses_static_threshold(self):
        self.app_context.settings = replace(
            self.app_context.settings,
            proactive_adaptive_groups={200},
            proactive_interest_threshold=98,
        )
        with patch.object(proactive_chat, "send", AsyncMock()) as sender:
            await self.send()
        self.choose_mock.assert_not_called()
        self.assertEqual(self.decide.await_args.kwargs, {})
        sender.assert_not_awaited()

    async def test_stale_trigger_and_below_threshold_do_not_send(self):
        event = self.event()
        event.time = 900
        with patch.object(proactive_chat, "send", AsyncMock()) as sender:
            await self.send(event)
            self.decide.assert_not_awaited()
            self.decide.return_value = ProactiveDecision(93, "不该发", False)
            await self.send(self.event(2))
        sender.assert_not_awaited()
        self.assertEqual(self.trials(), [])

    async def test_group_disabled_during_generation_prevents_send(self):
        with (
            patch.object(self.handlers, "group_enabled", side_effect=[True, False]),
            patch.object(proactive_chat, "send", AsyncMock()) as sender,
        ):
            await self.send()
        sender.assert_not_awaited()
        self.assertEqual(self.trials(), [])

    async def test_unknown_receipt_has_no_learning_and_no_retry(self):
        with patch.object(proactive_chat, "send", AsyncMock(return_value={})) as sender:
            await self.send()
        sender.assert_awaited_once()
        self.assertEqual(self.trials()[0]["status"], "unknown")
        self.assertEqual(self.store.settle_due(now=2000), 0)

    async def test_timeout_receipt_does_not_send_twice(self):
        failure = ActionFailed(retcode=1200, message="timeout")
        with (
            patch.object(
                proactive_chat, "send", AsyncMock(side_effect=failure)
            ) as sender,
            patch.object(
                self.handlers.replies, "_is_napcat_send_timeout", return_value=True
            ),
        ):
            await self.send()
        sender.assert_awaited_once()
        self.assertEqual(self.trials()[0]["status"], "unknown")

    async def test_definite_send_failure_is_recorded_without_training(self):
        with (
            patch.object(
                proactive_chat, "send", AsyncMock(side_effect=RuntimeError("offline"))
            ),
            self.assertRaises(RuntimeError),
        ):
            await self.send()
        self.assertEqual(self.trials()[0]["status"], "failed")
        self.assertEqual(self.store.settle_due(now=2000), 0)

    async def test_store_failure_fails_closed_without_model_or_send(self):
        self.choose_mock.side_effect = DatabaseError("offline")
        with patch.object(proactive_chat, "send", AsyncMock()) as sender:
            await self.send()
        self.decide.assert_not_awaited()
        sender.assert_not_awaited()

    async def test_ordinary_chatter_ignored_but_addressed_reply_is_counted(self):
        scope = ConversationScope("onebot-v11", "group", "100")
        trial = self.store.begin(scope, "999", "0", 94, 100, now=990)
        self.store.sent(trial, "-90", now=995)
        with patch.object(
            self.handlers.ingest_adapter, "ingest", AsyncMock()
        ) as ingest:
            await self.handlers.ingest.handle_canonical_ingest(self.event())
            self.assertEqual(
                self.store._connection.execute(
                    "SELECT * FROM proactive_feedback"
                ).fetchall(),
                [],
            )
            await self.handlers.ingest.handle_canonical_ingest(
                self.event(2, "[CQ:reply,id=-90] 说得有道理")
            )
        self.assertEqual(ingest.await_count, 2)
        rows = self.store._connection.execute(
            "SELECT * FROM proactive_feedback"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["kind"], rows[0]["value"]), ("follow_up", 1))

    async def test_addressed_rejection_and_notice_respect_group_opt_in(self):
        scope = ConversationScope("onebot-v11", "group", "100")
        trial = self.store.begin(scope, "999", "0", 94, 100, now=990)
        self.store.sent(trial, "-90", now=995)
        await self.handlers.ingest._observe_proactive_message(
            self.event(2, "[CQ:at,qq=999] 闭嘴")
        )
        notice = NoticeEvent(
            post_type="notice",
            notice_type="group_msg_emoji_like",
            time=1000,
            self_id=999,
            group_id=100,
            user_id=8,
            message_id=-90,
            is_add=True,
            likes=[dict(emoji_id="76")],
        )
        self.app_context.settings = replace(
            self.app_context.settings, proactive_adaptive_groups={200}
        )
        await self.handlers.ingest.handle_proactive_notice(
            SimpleNamespace(self_id="999"), notice
        )
        rows = self.store._connection.execute(
            "SELECT * FROM proactive_feedback"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "rejected")

    async def test_feedback_failure_does_not_interrupt_canonical_ingest(self):
        with (
            patch.object(self.handlers.ingest_adapter, "ingest", AsyncMock()) as ingest,
            patch.object(
                self.handlers, "group_enabled", side_effect=DatabaseError("offline")
            ),
        ):
            await self.handlers.ingest.handle_canonical_ingest(
                self.event(2, "[CQ:at,qq=999] 你觉得呢")
            )
        ingest.assert_awaited_once()
        self.app_context.logger.warning.assert_called_once()

    async def test_foreign_reply_self_messages_and_commands_are_not_feedback(self):
        scope = ConversationScope("onebot-v11", "group", "100")
        trial = self.store.begin(scope, "999", "0", 94, 100, now=990)
        self.store.sent(trial, "-90", now=995)
        cases = [
            self.event(2, "[CQ:reply,id=-91] 是这样吗"),
            self.event(3, "[CQ:at,qq=999] /ai hello"),
        ]
        own = self.event(4, "[CQ:reply,id=-90] 是这样吗")
        own.user_id = 999
        cases.append(own)
        for event in cases:
            await self.handlers.ingest._observe_proactive_message(event)
        self.assertEqual(
            self.store._connection.execute(
                "SELECT * FROM proactive_feedback"
            ).fetchall(),
            [],
        )


if __name__ == "__main__":
    unittest.main()
