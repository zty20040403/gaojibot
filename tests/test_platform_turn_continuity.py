"""Reply continuation is tied to actual endpoint evidence, even in shared history."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import unittest

from tests.postgres_fixture import postgres_for_test
from src.plugins.ai_chat.application.platform_turn_ports import (
    current_turn_context, reply_target_turn,
)
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey, InboundMessage
from src.plugins.ai_chat.turn_journal import TurnJournal


class PlatformTurnContinuityTests(unittest.TestCase):
    def setUp(self):
        database = postgres_for_test(self)
        self.ledger, self.journal = MessageLedger(database), TurnJournal(database)
        self.addCleanup(self.ledger.close)
        self.addCleanup(self.journal.close)
        self.scope = ConversationScope("onebot-v11", "group", "123")
        self.app = SimpleNamespace(message_ledger=self.ledger, turn_journal=self.journal,
            settings=SimpleNamespace(turn_expand_max_chars=3000))
        self.own, self.other = self.inbound("APP_A"), self.inbound("APP_B")

    def inbound(self, app):
        account = AccountKey("qq-official", app, "test")
        endpoint = EndpointKey(account, "group", "TEST_SAME_GROUP_OPENID")
        actor = ActorKey(account, "group_member", endpoint.native_conversation_id, "TEST_SAME_MEMBER_OPENID")
        return InboundMessage(endpoint, actor, "TEST_EVENT", "TEST_CURRENT_SAME_ID", "Member", "owner",
            MessageBody(), True, "TEST_REPLY_SAME_ID", 130, None, canonical_scope=self.scope)

    def message(self, inbound, native, text, occurred_at, *, outbound=False):
        actor = ActorKey(inbound.endpoint.account, "bot", "", "TEST_PLATFORM_BOT_ID") if outbound else inbound.actor
        return self.ledger.record_message(self.scope, native_message_id=native,
            sender_native_user_id=actor.native_user_id, sender_display="Bot" if outbound else "Member",
            body=MessageBody((TextNode(0, text),)), occurred_at=occurred_at,
            direction="outbound" if outbound else "inbound", actor=actor, source_endpoint=inbound.endpoint)

    def previous(self, inbound):
        trigger = self.message(inbound, "TEST_OLD_TRIGGER", "old question", 90)
        previous = self.journal.start_turn(self.scope, trigger_canonical_message_id=trigger.canonical_message_id,
            objective="old objective", provider="test", model="TEST_MODEL", started_at=91)
        self.journal.record_tool_started(previous.turn_id, 1, "fixture_read", {}, ("read",))
        self.journal.record_tool_finished(previous.turn_id, 1, "fixture_read", "succeeded", '{"ok":true}', ("read",))
        self.journal.finish_turn(previous.turn_id, status="succeeded", final_text="completed", finished_at=100)
        sent = self.message(inbound, "TEST_REPLY_SAME_ID", "old bot answer", 100, outbound=True)
        self.journal.link_send(previous.turn_id, sent.canonical_message_id)
        return previous

    def current(self):
        return self.message(self.own, self.own.native_message_id, "CURRENT_TRIGGER_MUST_BE_EXCLUDED", 130)

    def test_reference_shows_evidence_without_changing_requesting_actor(self):
        previous = self.previous(self.own)
        trigger = self.current()
        other_principal = self.ledger.ensure_actor_identity(self.other.actor, "Other")
        self.assertEqual(reply_target_turn(self.app, self.own).turn_id, previous.turn_id)
        context = current_turn_context(self.app, self.own)
        self.assertIn(previous.handle, context)
        self.assertEqual(trigger.sender_principal_id, self.ledger.principal_id_for_actor(self.own.actor))
        self.assertNotEqual(trigger.sender_principal_id, other_principal)

    def test_same_native_reply_id_in_bound_other_endpoint_cannot_select_its_turn(self):
        foreign = self.previous(self.other)
        self.assertIsNone(reply_target_turn(self.app, self.own))
        self.assertEqual(current_turn_context(self.app, self.own), "")
        previous = self.previous(self.own)
        self.assertEqual(reply_target_turn(self.app, self.own).turn_id, previous.turn_id)
        self.assertNotEqual(foreign.turn_id, previous.turn_id)

    def test_ambient_delta_keeps_other_endpoint_same_id_but_excludes_current_source(self):
        self.previous(self.own)
        self.message(self.other, self.own.native_message_id, "OTHER_ENDPOINT_SAME_ID_STAYS_VISIBLE", 120)
        self.current()
        context = current_turn_context(self.app, self.own)
        self.assertIn("此前回合之后有 1 条其他消息", context)
        self.assertIn("OTHER_ENDPOINT_SAME_ID_STAYS_VISIBLE", context)
        self.assertNotIn("CURRENT_TRIGGER_MUST_BE_EXCLUDED", context)

    def test_absent_unlinked_or_hidden_reply_gives_no_context(self):
        self.current()
        self.assertIsNone(reply_target_turn(self.app, replace(self.own, reply_to_native_message_id=None)))
        self.message(self.own, "TEST_REPLY_SAME_ID", "unlinked answer", 100, outbound=True)
        self.assertIsNone(reply_target_turn(self.app, self.own))
        self.previous(self.own)
        self.ledger.hide_history(self.scope)
        self.assertIsNone(reply_target_turn(self.app, self.own))
        self.assertEqual(current_turn_context(self.app, self.own), "")
