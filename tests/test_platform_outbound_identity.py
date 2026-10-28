"""The send requester and the platform-provided bot author are distinct."""

from __future__ import annotations

from dataclasses import replace
import unittest

from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, MentionNode, TextNode
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey, InboundMessage, SendRequest
from tests.postgres_fixture import postgres_for_test


BODY = MessageBody((TextNode(0, "test response"),))


class BotAuthorContractTests(unittest.TestCase):
    def setUp(self):
        self.account = AccountKey("qq-official", "TEST_APP", "test")
        self.bot = ActorKey(self.account, "bot", "", "TEST_PLATFORM_BOT_ID")
        self.private = EndpointKey(self.account, "private", "TEST_C2C_USER")
        self.group = EndpointKey(self.account, "group", "TEST_GROUP")

    def test_bot_key_roundtrips_and_is_account_scoped_without_group_namespace(self):
        self.assertEqual(ActorKey.parse(self.bot.key), self.bot)
        self.assertTrue(self.bot.matches_endpoint(self.private))
        self.assertTrue(self.bot.matches_endpoint(self.group))
        for other in (replace(self.account, app_id="TEST_OTHER_APP"), replace(self.account, environment="sandbox")):
            self.assertFalse(self.bot.matches_endpoint(EndpointKey(other, "private", "TEST_C2C_USER")))
        for namespace, native_id in (("TEST_GROUP", "TEST_BOT"), ("", ""), ("", "   ")):
            with self.assertRaises(ValueError):
                ActorKey(self.account, "bot", namespace, native_id)

    def test_bot_cannot_become_inbound_or_send_authorization_actor(self):
        for endpoint in (self.group, self.private):
            with self.assertRaisesRegex(ValueError, "request actors"):
                InboundMessage(endpoint, self.bot, "TEST_EVENT", "TEST_MSG", "Bot", "owner", BODY,
                               True, None, 100, None)
            with self.assertRaisesRegex(ValueError, "request actors"):
                SendRequest(endpoint, self.bot, BODY, "TEST_DELIVERY", 0)
        requester = ActorKey(self.account, "c2c_user", "", self.private.native_conversation_id)
        request = SendRequest(self.private, requester, BODY, "TEST_DELIVERY", 0)
        self.assertEqual(request.actor, requester)
        self.assertNotEqual(request.actor, self.bot)


class BotAuthorLedgerTests(unittest.TestCase):
    def setUp(self):
        self.ledger = MessageLedger(postgres_for_test(self))
        self.addCleanup(self.ledger.close)
        self.account = AccountKey("qq-official", "TEST_APP", "test")
        self.private = EndpointKey(self.account, "private", "TEST_USER")
        self.user = ActorKey(self.account, "c2c_user", "", "TEST_USER")
        self.bot = ActorKey(self.account, "bot", "", "TEST_PLATFORM_BOT_ID")

    def record(self, endpoint, actor, message, *, direction="inbound", body=BODY, scope=None, reply=None, native_user=None):
        return self.ledger.record_message(scope or endpoint.scope, native_message_id=message,
            sender_native_user_id=actor.native_user_id if native_user is None else native_user,
            sender_display="Bot" if actor.identity_kind == "bot" else "User", body=body,
            direction=direction, occurred_at=100, actor=actor, source_endpoint=endpoint,
            reply_to_native_message_id=reply)

    def test_c2c_outbound_has_bot_author_and_human_mentions_in_the_recipient_domain(self):
        request = self.record(self.private, self.user, "TEST_REQUEST")
        sent = self.record(self.private, self.bot, "TEST_SENT", direction="outbound", reply="TEST_REQUEST",
                           body=MessageBody((MentionNode(0, "TEST_USER", "User"),
                                             MentionNode(1, "TEST_OTHER_USER", "Other"), TextNode(2, "reply"))))
        self.assertEqual(sent.sender_actor_key, self.bot.key)
        self.assertEqual(sent.sender_native_user_id, "TEST_PLATFORM_BOT_ID")
        self.assertNotEqual(sent.sender_principal_id, request.sender_principal_id)
        self.assertFalse(sent.is_from_actor(self.user.key))
        self.assertTrue(sent.is_from_actor(self.bot.key))
        self.assertEqual(sent.reply_to_canonical_message_id, request.canonical_message_id)
        self.assertEqual(sent.body.nodes[0].principal_id, request.sender_principal_id)
        self.assertIsNone(sent.body.nodes[1].principal_id)
        self.assertIsNone(self.ledger.principal_id_for_actor(ActorKey(self.account, "bot", "", "TEST_USER")))
        self.assertIsNone(self.ledger.actor_identity_for_principal(self.private, sent.sender_principal_id))
        self.assertIsNone(self.ledger.principal_id_for_actor(ActorKey(self.account, "c2c_user", "", "TEST_OTHER_USER")))

    def test_bot_group_mentions_use_group_member_namespace_and_bot_identity_is_account_global(self):
        private_sent = self.record(self.private, self.bot, "TEST_PRIVATE_SENT", direction="outbound")
        members = []
        for group in ("TEST_GROUP_A", "TEST_GROUP_B"):
            endpoint = EndpointKey(self.account, "group", group)
            member = ActorKey(self.account, "group_member", group, "TEST_MEMBER")
            request = self.record(endpoint, member, "TEST_REQUEST")
            sent = self.record(endpoint, self.bot, "TEST_SENT", direction="outbound",
                               body=MessageBody((MentionNode(0, "TEST_MEMBER", "Member"),)))
            self.assertEqual(sent.sender_principal_id, private_sent.sender_principal_id)
            self.assertEqual(sent.body.nodes[0].principal_id, request.sender_principal_id)
            self.assertEqual(self.ledger.actor_identity_for_principal(endpoint, request.sender_principal_id)[0], member)
            self.assertIsNone(self.ledger.actor_identity_for_principal(endpoint, sent.sender_principal_id))
            members.append(request.sender_principal_id)
        self.assertNotEqual(*members)

    def test_shared_canonical_history_keeps_outbound_author_reply_and_dedup_account_qualified(self):
        history = ConversationScope("onebot-v11", "group", "123")
        sent = []
        for account in (self.account, replace(self.account, app_id="TEST_APP_B"),
                        replace(self.account, environment="sandbox")):
            endpoint = EndpointKey(account, "private", "TEST_USER")
            bot = ActorKey(account, "bot", "", "TEST_PLATFORM_BOT_ID")
            user = ActorKey(account, "c2c_user", "", "TEST_USER")
            reply = self.record(endpoint, bot, "TEST_SENT", direction="outbound", scope=history, reply="TEST_REQUEST")
            target = self.record(endpoint, user, "TEST_REQUEST", scope=history)
            stored = self.ledger.get_in_scope(history, reply.canonical_message_id)
            self.assertEqual(stored.reply_to_canonical_message_id, target.canonical_message_id)
            self.assertEqual(stored.source_endpoint_key, endpoint.key)
            self.assertEqual(stored.sender_actor_key, bot.key)
            duplicate = self.record(endpoint, bot, "TEST_SENT", direction="outbound", scope=history)
            self.assertEqual(duplicate.canonical_message_id, reply.canonical_message_id)
            sent.append(reply)
        self.assertEqual(len({item.canonical_message_id for item in sent}), 3)
        self.assertEqual(len({item.sender_principal_id for item in sent}), 3)

    def test_sender_direction_account_and_native_identity_mismatch_fail_before_writes(self):
        cases = [
            (self.private, self.bot, "inbound", None),
            (self.private, self.bot, "system", None),
            (self.private, self.user, "outbound", None),
            (self.private, self.bot, "outbound", self.user.native_user_id),
            (EndpointKey(replace(self.account, app_id="TEST_FOREIGN_APP"), "private", "TEST_USER"),
             self.bot, "outbound", None),
        ]
        for endpoint, actor, direction, native in cases:
            with self.subTest(direction=direction, actor=actor.key), self.assertRaises(ValueError):
                self.record(endpoint, actor, "TEST_REJECTED", direction=direction, native_user=native)
        self.assertEqual(self.ledger.list_scopes(), [])
        self.assertIsNone(self.ledger.principal_id_for_actor(self.bot))
