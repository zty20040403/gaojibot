from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from tests.postgres_fixture import postgres_for_test
from src.plugins.ai_chat.context_pipeline import ReferenceResolver
from src.plugins.ai_chat.context_pipeline.models import ContextTokenBudget
from src.plugins.ai_chat.context_pipeline.recall import build_hybrid_recall, _message_candidate
from src.plugins.ai_chat.context_store import ContextStore
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.historian import HistorianService, HistorianResult, MemoryProposal
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.long_term_memory import LongTermMemoryStore, LongTermMemoryError
from src.plugins.ai_chat.memory_scopes import group_memory_scope, memory_scopes_for_inbound
from src.plugins.ai_chat.message_ir import MessageBody, TextNode, MentionNode
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey, InboundMessage
from src.plugins.ai_chat.storage.jobs import DurableJobStore


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

    def test_actor_comparison_in_bound_history_does_not_use_bare_native_id(self):
        scope = ConversationScope("onebot-v11", "group", "123")
        a, b = inbound(scope=scope), inbound(app="other", scope=scope, message="current")
        previous = record(self.ledger, a, text="Project database: which one should we use?")
        current = record(self.ledger, b, text="project database")
        self.assertFalse(previous.is_from_actor(b.actor.key, b.actor.native_user_id))
        same = _message_candidate(previous, "raw_history", 110, current_native_user_id=a.actor.native_user_id, current_actor_key=a.actor.key, reason="fixture")
        other = _message_candidate(previous, "raw_history", 110, current_native_user_id=b.actor.native_user_id, current_actor_key=b.actor.key, reason="fixture")
        self.assertGreater(same.actor_score, other.actor_score)
        plan = ReferenceResolver().resolve(self.ledger, scope, current_message_id=current.canonical_message_id,
            current_text="为什么", current_native_user_id=b.actor.native_user_id,
            current_actor_key=b.actor.key, now=110)
        candidate = next(c for c in plan.candidates if c.message_id == previous.canonical_message_id)
        self.assertIn("other_participant", candidate.reason_codes)

    def test_historian_restart_and_recall_use_account_scoped_memory(self):
        context = ContextStore(self.db, input_budget_tokens=1000, high_watermark_tokens=180,
            low_watermark_tokens=90, compartment_target_tokens=70, raw_tail_min_messages=3)
        jobs = DurableJobStore(self.db)
        self.addCleanup(context.close)
        self.addCleanup(jobs.close)
        item, other = inbound(), inbound(app="other")
        for index in range(18):
            record(self.ledger, replace(item, native_message_id=str(index)), text=f"project fact {index} " + "x" * 180)
        memories = LongTermMemoryStore(self.db)
        personal, _ = memories.add(item.actor.key, "user", "project personal preference", creator_actor_key=item.actor.key)
        foreign, _ = memories.add(other.actor.key, "user", "foreign secret", creator_actor_key=other.actor.key)
        async def generate(candidate):
            return HistorianResult("detail", "summary", "short", (MemoryProposal("project shared rule", candidate.messages[0].canonical_message_id),))
        service = HistorianService(self.ledger, context, memories, generate)
        self.assertEqual(service.schedule_due(jobs, now=1000), 1)
        job = jobs.claim_due("worker", now=1000)[0]
        self.assertEqual(job.payload["scope_key"], item.scope.key)
        asyncio.run(HistorianService(self.ledger, context, memories, generate).handle_job(job))
        self.assertEqual(len(memories.list_entries([item.scope.key])), 1)
        self.assertEqual(memories.list_entries([other.scope.key]), [])
        group, user = memory_scopes_for_inbound(item)
        class ReturningForeignHit:
            async def search(self, scopes, _query, *, limit):
                self.scopes = scopes
                return [SimpleNamespace(scope_key=other.actor.key, source_type="memory",
                    source_handle=f"memory#{foreign.id}", content="foreign secret", score=1.0)]
        semantic = ReturningForeignHit()
        recalled = asyncio.run(build_hybrid_recall(ledger=self.ledger, scope=item.scope, plan=None,
            user_text="project", group_memory_scope=group, user_memory_scope=user, memory_store=memories,
            budget=ContextTokenBudget(200, 200, 300, 300, 300), current_actor_key=item.actor.key,
            current_native_user_id=item.actor.native_user_id, semantic_recall=semantic))
        self.assertIn("project shared rule", recalled.memory_context)
        self.assertIn(personal.content, recalled.memory_context)
        self.assertNotIn("foreign secret", recalled.memory_context)
        self.assertEqual(set(semantic.scopes), {item.scope.key, item.actor.key})
        forged = replace(job, payload={**job.payload, "scope_key": other.scope.key})
        with self.assertRaises(ValueError):
            service._candidate_from_job(forged)


class PlatformMemoryTests(unittest.TestCase):
    def test_existing_bridge_group_memory_keys_are_preserved(self):
        for platform in ("matrix", "imessage"):
            scope = ConversationScope(platform, "group", "TEST_LEGACY_GROUP")
            self.assertEqual(group_memory_scope(scope), f"group:{platform}:TEST_LEGACY_GROUP")

    def test_actor_memory_unchanged_by_canonical_binding_and_roundtrips_null_qq(self):
        item = inbound()
        bound = replace(item, canonical_scope=ConversationScope("onebot-v11", "group", "123"))
        self.assertEqual(memory_scopes_for_inbound(item), (item.scope.key, item.actor.key))
        self.assertEqual(memory_scopes_for_inbound(bound), ("group:123", item.actor.key))
        with TemporaryDirectory() as directory:
            path = Path(directory) / "memory.json"
            store = LongTermMemoryStore(path, max_entries_per_scope=1)
            first, _ = store.add(item.actor.key, "user", "first", creator_actor_key=item.actor.key, creator_principal_id=77)
            second, _ = store.add(item.actor.key, "user", "second", creator_actor_key=item.actor.key, creator_principal_id=77)
            store.update(second.id, "updated", [item.actor.key], expected_version=1, actor_key=item.actor.key, actor_user_id=None, actor_principal_id=77)
            reloaded = LongTermMemoryStore(path)
            self.assertEqual(reloaded.list_entries([item.actor.key])[0].creator_actor_key, item.actor.key)
            self.assertIsNone(reloaded.list_entries([item.actor.key])[0].creator_user_id)
            self.assertTrue(all(m.actor_key == item.actor.key and m.actor_user_id is None for m in reloaded.audit([item.actor.key])))
            self.assertIn(first.id, [m.memory_id for m in reloaded.audit([item.actor.key]) if m.action == "evict"])
            reloaded.remove(second.id, [item.actor.key], actor_key=item.actor.key, actor_user_id=None)
            self.assertEqual(reloaded.list_entries([item.actor.key]), [])
            with self.assertRaises(LongTermMemoryError):
                reloaded.add(item.actor.key, "user", "bad", creator_user_id=77, creator_actor_key=item.actor.key)

    def test_same_openid_and_same_text_do_not_merge_personal_memory(self):
        items = [inbound(), inbound(app="other"), inbound(environment="sandbox"), inbound(group="other"), inbound(kind="private")]
        with TemporaryDirectory() as directory:
            store = LongTermMemoryStore(Path(directory) / "memory.json")
            entries = [store.add(i.actor.key, "user", "same text", creator_actor_key=i.actor.key)[0] for i in items]
            self.assertEqual(len({entry.id for entry in entries}), len(items))
            for item, entry in zip(items, entries):
                self.assertEqual(store.list_entries([item.actor.key]), [entry])
