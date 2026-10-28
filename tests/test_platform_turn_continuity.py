"""Reply continuation is tied to actual endpoint evidence, even in shared history."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.postgres_fixture import postgres_for_test
from src.plugins.ai_chat.application.platform_turn_ports import (
    current_turn_context, platform_turn_ports, reply_target_turn,
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

    def current(self, inbound=None):
        inbound = inbound or self.own
        trigger = self.message(inbound, inbound.native_message_id, "CURRENT_TRIGGER_MUST_BE_EXCLUDED", 130)
        turn = self.journal.start_turn(self.scope, trigger_canonical_message_id=trigger.canonical_message_id,
            objective="continuation", provider="test", model="TEST_MODEL", started_at=130)
        return turn, trigger

    def edges(self):
        return self.journal._connection.execute(
            "SELECT from_turn_id, to_turn_id, created_by_principal_id FROM turn_edges ORDER BY edge_id"
        ).fetchall()

    def test_actual_reply_target_creates_fork_with_requesting_actor_principal(self):
        previous = self.previous(self.own)
        current, trigger = self.current()
        other_principal = self.ledger.ensure_actor_identity(self.other.actor, "Other")
        self.assertEqual(reply_target_turn(self.app, self.own).turn_id, previous.turn_id)
        context = current_turn_context(self.app, self.own, current.turn_id)
        self.assertIn(previous.handle, context)
        edges = self.edges()
        self.assertEqual(len(edges), 1)
        self.assertEqual((edges[0]["from_turn_id"], edges[0]["to_turn_id"]), (current.turn_id, previous.turn_id))
        self.assertEqual(edges[0]["created_by_principal_id"], trigger.sender_principal_id)
        self.assertNotEqual(edges[0]["created_by_principal_id"], other_principal)
        self.assertEqual(self.journal.fork_parent(self.scope, current.turn_id).turn_id, previous.turn_id)

    def test_same_native_reply_id_in_bound_other_endpoint_cannot_select_its_turn(self):
        foreign = self.previous(self.other)
        current, _ = self.current()
        self.assertIsNone(reply_target_turn(self.app, self.own))
        self.assertEqual(current_turn_context(self.app, self.own, current.turn_id), "")
        self.assertEqual(self.edges(), [])
        previous = self.previous(self.own)
        self.assertEqual(reply_target_turn(self.app, self.own).turn_id, previous.turn_id)
        current, _ = self.current()
        current_turn_context(self.app, self.own, current.turn_id)
        self.assertNotEqual(self.edges()[0]["to_turn_id"], foreign.turn_id)
        self.assertEqual(self.edges()[0]["to_turn_id"], previous.turn_id)

    def test_refresh_never_creates_or_recreates_fork_edge(self):
        previous = self.previous(self.own)
        current, _ = self.current()
        ports = platform_turn_ports(self.app, self.own, reference_resolver=None)
        with patch.object(self.journal, "add_fork_edge", side_effect=AssertionError("refresh created edge")):
            refreshed = ports.refresh_turn_context(current.turn_id)
        self.assertIn(previous.handle, refreshed)
        self.assertEqual(self.edges(), [])
        current_turn_context(self.app, self.own, current.turn_id)
        original = [dict(row) for row in self.edges()]
        with patch.object(self.journal, "add_fork_edge", side_effect=AssertionError("refresh recreated edge")):
            ports.refresh_turn_context(current.turn_id)
            ports.refresh_turn_context(current.turn_id)
        self.assertEqual([dict(row) for row in self.edges()], original)

    def test_ambient_delta_keeps_other_endpoint_same_id_but_excludes_current_source(self):
        self.previous(self.own)
        self.message(self.other, self.own.native_message_id, "OTHER_ENDPOINT_SAME_ID_STAYS_VISIBLE", 120)
        current, _ = self.current()
        context = current_turn_context(self.app, self.own, current.turn_id)
        self.assertIn("此前回合之后有 1 条其他消息", context)
        self.assertIn("OTHER_ENDPOINT_SAME_ID_STAYS_VISIBLE", context)
        self.assertNotIn("CURRENT_TRIGGER_MUST_BE_EXCLUDED", context)

    def test_absent_unlinked_or_hidden_reply_does_not_create_a_fork(self):
        current, _ = self.current()
        self.assertIsNone(reply_target_turn(self.app, replace(self.own, reply_to_native_message_id=None)))
        self.message(self.own, "TEST_REPLY_SAME_ID", "unlinked answer", 100, outbound=True)
        self.assertIsNone(reply_target_turn(self.app, self.own))
        self.previous(self.own)
        self.ledger.hide_history(self.scope)
        self.assertIsNone(reply_target_turn(self.app, self.own))
        self.assertEqual(current_turn_context(self.app, self.own, current.turn_id), "")
        self.assertEqual(self.edges(), [])
