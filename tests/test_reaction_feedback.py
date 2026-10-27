from __future__ import annotations

from tests.postgres_fixture import postgres_for_test

import unittest

import nonebot
nonebot.init()

from nonebot.adapters.onebot.v11 import NoticeEvent
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.matchers import _is_reaction_notice
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.reaction_feedback import record_reaction_feedback
from src.plugins.ai_chat.turn_journal import TurnJournal


class ReactionFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.ledger = MessageLedger(postgres_for_test(self))
        self.journal = TurnJournal(postgres_for_test(self))
        self.addCleanup(self.ledger.close)
        self.addCleanup(self.journal.close)
        self.scope = ConversationScope("onebot-v11", "group", "100")
        self.question = self.message("q", "10", "what do you think?")
        self.turn = self.journal.start_turn(self.scope,
            trigger_canonical_message_id=self.question.canonical_message_id,
            objective="what do you think?", provider="fixture", model="fixture")
        self.answer = self.message("-99", "900", "an answer", direction="outbound")
        self.journal.link_send(self.turn.turn_id, self.answer.canonical_message_id)
        self.journal.record_context_plan(self.turn.turn_id, {"scope_key": self.scope.key,
            "current_message_id": self.question.canonical_message_id})

    def message(self, native_id, user, text, **kwargs):
        return self.ledger.record_message(self.scope, native_message_id=native_id,
            sender_native_user_id=user, sender_display="Member", body=MessageBody((TextNode(0, text),)),
            occurred_at=1000, **kwargs)

    def notice(self, **changes):
        return {"post_type": "notice", "notice_type": "group_msg_emoji_like", "self_id": 900,
            "group_id": 100, "user_id": 10, "message_id": -99, "time": 1010,
            "is_add": True, "likes": [{"emoji_id": "76", "count": 999}], **changes}

    def ingest(self, payload, **options):
        return record_reaction_feedback(payload, bot_id=900, ledger=self.ledger, journal=self.journal,
            group_enabled=lambda group_id: group_id == 100,
            **{"positive_ids": ("76",), "negative_ids": ("77",), "now": 2000, **options})

    def votes(self):
        return self.journal.recent_context_plans()[0]["votes"]

    def test_notice_preserves_extension_fields_and_tracks_asker_not_count(self):
        event = NoticeEvent(**self.notice())
        self.assertTrue(_is_reaction_notice(event))
        self.assertEqual(self.ingest(event.model_dump()), 1)
        self.assertEqual(self.ingest(self.notice()), 0)
        self.assertEqual(self.votes(), {"asker": 1, "others_up": 0, "others_down": 0})
        self.assertEqual(self.ingest(self.notice(user_id=11)), 1)
        self.assertEqual(self.votes(), {"asker": 1, "others_up": 1, "others_down": 0})

    def test_change_retract_duplicate_and_old_event_order(self):
        self.ingest(self.notice())
        self.ingest(self.notice(time=1020, likes=[{"emoji_id": "77", "count": 1}]))
        self.assertEqual(self.votes()["asker"], -1)
        self.assertEqual(self.ingest(self.notice(time=1030, is_add=False)), 0)
        self.assertEqual(self.votes()["asker"], -1)
        self.assertEqual(self.ingest(self.notice(time=1040, is_add=False, likes=[{"emoji_id": "77"}])), 1)
        self.assertIsNone(self.votes()["asker"])
        self.assertEqual(self.ingest(self.notice(time=1020, likes=[{"emoji_id": "77"}])), 0)
        self.assertEqual(self.ingest(self.notice(time=1040, likes=[{"emoji_id": "77"}])), 0)
        self.assertEqual(self.ingest(self.notice(time=1050)), 1)

    def test_filtering_does_not_create_votes_or_identities(self):
        for changed in ({"user_id": 900}, {"user_id": None}, {"group_id": 200},
                        {"self_id": 901}, {"message_id": 9999}, {"is_add": "false"},
                        {"time": 999999}, {"likes": [{"emoji_id": "13"}]},
                        {"likes": [{"emoji_id": "76"}, {"emoji_id": "77"}]}):
            with self.subTest(changed=changed):
                self.assertEqual(self.ingest(self.notice(**changed)), 0)
        self.assertEqual(self.ingest(self.notice(), positive_ids=()), 0)
        self.assertEqual(self.ingest(self.notice(), negative_ids=("76",)), 0)
        self.assertEqual(self.votes(), {"asker": None, "others_up": 0, "others_down": 0})

    def test_only_linked_bot_messages_in_current_scope(self):
        forged = self.message("88", "12", "not the bot")
        self.journal.link_send(self.turn.turn_id, forged.canonical_message_id)
        self.assertEqual(self.ingest(self.notice(message_id=88)), 0)
        other = ConversationScope("onebot-v11", "group", "200")
        self.assertFalse(self.journal.set_reaction_vote(other, self.turn.turn_id,
            reactor_principal_id=self.question.sender_principal_id,
            message_id=self.answer.canonical_message_id, emoji_id="76", vote=1,
            is_add=True, source_time=1020))
        self.ledger.hide_history(self.scope)
        self.assertEqual(self.ingest(self.notice()), 0)

    def test_removing_reaction_on_old_chunk_does_not_remove_new_chunk_vote(self):
        second = self.message("-100", "900", "second chunk", direction="outbound")
        self.journal.link_send(self.turn.turn_id, second.canonical_message_id, chunk_index=1)
        self.ingest(self.notice())
        self.ingest(self.notice(message_id=-100, time=1020))
        self.assertEqual(self.ingest(self.notice(is_add=False, time=1030)), 0)
        self.assertEqual(self.votes()["asker"], 1)

    def test_admin_label_is_independent_and_negative_filter_runs_in_store(self):
        self.journal.set_context_feedback(self.turn.turn_id, verdict="correct", note="reviewed",
            actor="admin", resource_version=1)
        self.ingest(self.notice(user_id=12, likes=[{"emoji_id": "77"}]))
        plan = self.journal.recent_context_plans(negative_only=True)[0]
        self.assertEqual(plan["feedback"]["verdict"], "correct")
        self.assertEqual(plan["votes"]["others_down"], 1)
        self.assertEqual(self.journal.recent_context_plans(turn_id=999), [])
        self.ingest(self.notice(user_id=12, time=1020, is_add=False, likes=[{"emoji_id": "77"}]))
        self.assertEqual(self.journal.recent_context_plans(negative_only=True), [])
