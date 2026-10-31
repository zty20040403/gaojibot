from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import unittest

from tests.postgres_fixture import postgres_for_test
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, TextNode, MentionNode
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey, InboundMessage


def inbound(*, app="app", environment="test", group="group", user="same-openid", kind="group", message="m", scope=None):
    account = AccountKey("qq-official", app, environment)
    endpoint = EndpointKey(account, kind, group if kind == "group" else user)
    actor = ActorKey(account, "group_member" if kind == "group" else "c2c_user", group if kind == "group" else "", user)
    return InboundMessage(endpoint, actor, "event", message, "Member", "", MessageBody((TextNode(0, "project fact"),)), True, None, 100, None, canonical_scope=scope)


def record(ledger, item, *, text="project fact", reply=None, body=None):
    return ledger.record_message(item.scope, native_message_id=item.native_message_id,
        sender_native_user_id=item.actor.native_user_id, sender_display=item.sender_display,
        body=body or MessageBody((TextNode(0, text),)), occurred_at=item.occurred_at,
        reply_to_native_message_id=reply, actor=item.actor, source_endpoint=item.endpoint)


class PlatformIdentityStorageTests(unittest.TestCase):
    def setUp(self):
        self.db = postgres_for_test(self)
        self.ledger = MessageLedger(self.db)
        self.addCleanup(self.ledger.close)

    def test_same_opaque_id_isolated_by_app_environment_kind_and_namespace(self):
        items = [inbound(), inbound(app="other"), inbound(environment="sandbox"),
                 inbound(group="other"), inbound(kind="private")]
        messages = [record(self.ledger, item, body=MessageBody((MentionNode(0, "peer", "Peer"),))) for item in items]
        self.assertEqual(len({m.sender_principal_id for m in messages}), len(items))
        for item, message in zip(items, messages):
            self.assertEqual(message.sender_native_user_id, "same-openid")
            self.assertEqual(message.sender_actor_key, item.actor.key)
            self.assertEqual(message.source_endpoint_key, item.endpoint.key)
            self.assertEqual(self.ledger.principal_id_for_actor(item.actor), message.sender_principal_id)
            self.assertEqual(self.ledger.actor_identity_for_principal(item.endpoint, message.sender_principal_id)[0], item.actor)
            self.assertIsNone(self.ledger.actor_identity_for_principal(inbound(app="never-seen").endpoint, message.sender_principal_id))
        self.assertEqual(len({m.body.nodes[0].principal_id for m in messages[:4]}), 4)
        self.assertIsNone(messages[-1].body.nodes[0].principal_id)  # C2C cannot establish an unrelated actor.
        self.assertEqual({s.key for s in self.ledger.list_scopes()}, {i.scope.key for i in items})
        with self.assertRaises(ValueError):
            self.ledger.principal_id_for_native("qq-official", "same-openid")
        with self.assertRaises(ValueError):
            self.ledger.principal_label_for_native("qq-official", "same-openid")
        with self.assertRaises(ValueError):
            self.ledger.ensure_principal_identity("qq-official", "same-openid", "Member")
        with self.assertRaises(ValueError):
            self.ledger.native_identity_for_principal("qq-official", messages[0].sender_principal_id)

    def test_parallel_mapping_allocates_one_principal_without_orphans(self):
        actor = inbound().actor
        ledgers = [MessageLedger(self.db) for _ in range(6)]
        for ledger in ledgers:
            self.addCleanup(ledger.close)
        with ThreadPoolExecutor(max_workers=6) as pool:
            principals = list(pool.map(lambda n: ledgers[n % 6].ensure_actor_identity(actor, "Member", seen_at=100), range(30)))
        self.assertEqual(len(set(principals)), 1)
        with self.ledger._lock:
            self.assertEqual(self.ledger._connection.execute("SELECT COUNT(*) FROM principals").fetchone()[0], 1)
            self.assertEqual(self.ledger._connection.execute("SELECT COUNT(*) FROM principal_identities").fetchone()[0], 1)

    def test_bound_history_keeps_source_dedup_reply_and_lookup_separate(self):
        scope = ConversationScope("onebot-v11", "group", "123")
        a, b = inbound(scope=scope), inbound(app="other", scope=scope)
        reply_a = record(self.ledger, replace(a, native_message_id="reply"), reply="same")
        reply_b = record(self.ledger, replace(b, native_message_id="reply"), reply="same")
        first = record(self.ledger, replace(a, native_message_id="same"), text="A target")
        second = record(self.ledger, replace(b, native_message_id="same"), text="B target")
        self.assertNotEqual(first.canonical_message_id, second.canonical_message_id)
        self.assertEqual(record(self.ledger, replace(a, native_message_id="same"), text="duplicate").canonical_message_id, first.canonical_message_id)
        for item, target, reply in ((a, first, reply_a), (b, second, reply_b)):
            self.assertEqual(self.ledger.get_in_scope(scope, reply.canonical_message_id).reply_to_canonical_message_id, target.canonical_message_id)
            self.assertEqual(self.ledger.canonical_id_for_native(scope, "same", endpoint=item.endpoint), target.canonical_message_id)
        self.assertIsNone(self.ledger.canonical_id_for_native(scope, "same"))
        visible = self.ledger.render_recent(scope, exclude_native_message_id="same", exclude_endpoint=a.endpoint)
        self.assertNotIn("A target", visible)
        self.assertIn("B target", visible)
        fresh = inbound()
        with self.assertRaises(ValueError):
            self.ledger.canonical_id_for_native(fresh.scope, "m")
        with self.assertRaises(ValueError):
            self.ledger.record_message(fresh.scope, native_message_id="m", sender_native_user_id="x", sender_display="x", body=fresh.body)

    def test_onebot_actor_and_endpoint_explicitly_resolve_legacy_rows(self):
        scope = ConversationScope("onebot-v11", "group", "123")
        old = self.ledger.record_message(scope, native_message_id="old", sender_native_user_id="7", sender_display="Alice", body=MessageBody((TextNode(0, "legacy"),)))
        account = AccountKey("onebot-v11", "900", "production")
        endpoint = EndpointKey(account, "group", "123")
        actor = ActorKey(account, "user", "", "7")
        self.assertEqual(self.ledger.ensure_actor_identity(actor, "Alice"), old.sender_principal_id)
        self.assertEqual(self.ledger.canonical_id_for_native(scope, "old", endpoint=endpoint), old.canonical_message_id)
        self.assertTrue(old.is_from_actor(actor.key))
        matrix_scope = ConversationScope("matrix", "group", "TEST_MATRIX_ROOM")
        matrix_message = self.ledger.record_message(
            matrix_scope, native_message_id="TEST_MATRIX_MSG", sender_native_user_id="7",
            sender_display="Same numeric label", body=old.body,
        )
        self.assertFalse(matrix_message.is_from_actor(actor.key))
        self.assertEqual(self.ledger.actor_identity_for_principal(endpoint, old.sender_principal_id)[0], actor)
        routed = EndpointKey(account, "group", "456")
        distinct = self.ledger.record_message(scope, native_message_id="old", sender_native_user_id="7",
            sender_display="Alice", body=old.body, actor=actor, source_endpoint=routed)
        self.assertNotEqual(distinct.canonical_message_id, old.canonical_message_id)
        self.assertEqual(self.ledger.canonical_id_for_native(scope, "old", endpoint=routed), distinct.canonical_message_id)

    def test_native_identity_is_not_trimmed_or_interpreted_as_number(self):
        item = inbound(user=" 0007 ", message=" message ")
        message = record(self.ledger, item)
        self.assertEqual(message.sender_native_user_id, " 0007 ")
        self.assertEqual(message.native_message_id, " message ")
        self.assertIsNone(self.ledger.principal_id_for_actor(inbound(user="0007").actor))
        self.assertEqual(self.ledger.canonical_id_for_native(item.scope, " message ", endpoint=item.endpoint), message.canonical_message_id)




