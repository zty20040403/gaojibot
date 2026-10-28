"""Structured history and personal memory never authorize through legacy IDs."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.context_store import ContextStore
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.long_term_memory import LongTermMemoryError, LongTermMemoryStore
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey, InboundMessage, PlatformFeatures
from src.plugins.ai_chat.tools import neutral_registry_for
from src.plugins.ai_chat.tools.memory import memory_scope_keys, official_memory_provenance
from src.plugins.ai_chat.tools.state import ToolContext, TurnState
from src.plugins.ai_chat.turn_journal import TurnJournal
from tests.postgres_fixture import postgres_for_test


def inbound(*, app="TEST_APP", environment="production", group="TEST_GROUP", user="TEST_USER", message="TEST_MSG"):
    account = AccountKey("qq-official", app, environment)
    endpoint = EndpointKey(account, "group" if group else "private", group or user)
    actor = ActorKey(account, "group_member" if group else "c2c_user", group or "", user)
    return InboundMessage(
        endpoint=endpoint, actor=actor, event_id="TEST_EVENT", native_message_id=message,
        sender_display="Test user", platform_role="member", body=MessageBody((TextNode(0, "needle actual message"),)),
        mentions_bot=True, reply_to_native_message_id=None, occurred_at=1000, reply_credential=None,
    )


class NoLegacyCommands:
    def __getattribute__(self, name):
        raise AssertionError("official tool used a legacy helper: " + name)


class PlatformContextToolsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        database = postgres_for_test(self)
        self.ledger = MessageLedger(database)
        self.memory = LongTermMemoryStore(database)
        self.journal = TurnJournal(database)
        self.chapters = ContextStore(database, input_budget_tokens=1000,
                                     high_watermark_tokens=500, low_watermark_tokens=250,
                                     compartment_target_tokens=250, raw_tail_min_messages=3)
        for store in (self.ledger, self.journal, self.chapters):
            self.addCleanup(store.close)
        self.app = SimpleNamespace(
            db_call=DatabaseCalls(), message_ledger=self.ledger, long_term_memory=self.memory,
            turn_journal=self.journal, context_store=self.chapters, pin_store=None,
            semantic_recall=None, settings=SimpleNamespace(turn_expand_max_chars=3000), logger=Mock(),
        )
        self.addAsyncCleanup(self.app.db_call.close)
        self.message = inbound()
        self.turn = self.make_turn(self.message)

    def make_turn(self, message):
        platform = SimpleNamespace(account=message.endpoint.account, features=PlatformFeatures())
        services = SimpleNamespace(context=self.app, commands=NoLegacyCommands())
        return TurnState(ToolContext(services, event=None, inbound=message, platform=platform,
                                    capabilities=frozenset({"context", "memory", "pins", "members"})))

    def record(self, message):
        return self.ledger.record_message(
            message.scope, native_message_id=message.native_message_id,
            sender_native_user_id=message.actor.native_user_id, sender_display=message.sender_display,
            body=message.body, occurred_at=message.occurred_at, actor=message.actor, source_endpoint=message.endpoint,
        )

    async def call(self, name, arguments, *, turn=None):
        turn = turn or self.turn
        return json.loads(await neutral_registry_for(turn.context).execute(turn, name, arguments))

    def variants(self):
        return [self.message, inbound(app="TEST_APP_B"), inbound(environment="sandbox"),
                inbound(group="TEST_GROUP_B"), inbound(group=None),
                inbound(user="TEST_OTHER_USER", message="TEST_OTHER_MESSAGE")]

    async def test_declared_capabilities_allow_only_migrated_read_and_memory_tools(self):
        registry = neutral_registry_for(self.turn.context)
        self.assertEqual({item["function"]["name"] for item in registry.definitions(self.turn.context)},
                         {"context_expand", "context_search", "memory_add", "memory_list", "memory_remove"})
        for tool in ("group_members", "pin_message", "unpin_message"):
            self.assertEqual((await self.call(tool, {}))["code"], "tool_unavailable")

    async def test_same_openid_across_app_environment_group_and_c2c_has_separate_memory(self):
        entries = []
        for index, message in enumerate(self.variants()):
            turn = self.make_turn(message)
            self.record(message)
            result = await self.call("memory_add", {"content": f"needle private preference {index}"}, turn=turn)
            self.assertTrue(result["ok"])
            entries.append((turn, result["memory"]["id"]))
        for turn, own_id in entries:
            listed = await self.call("memory_list", {}, turn=turn)
            self.assertEqual([entry["id"] for entry in listed["memories"]], [own_id])
            for other_turn, other_id in entries:
                if other_id != own_id:
                    self.assertFalse((await self.call("memory_remove", {"memory_id": other_id}, turn=turn))["ok"])
            entry = self.memory.list_entries([turn.context.inbound.actor.key])[0]
            self.assertIsNone(entry.creator_user_id)
            self.assertEqual(entry.creator_actor_key, turn.context.inbound.actor.key)
            self.assertGreater(entry.creator_principal_id, 0)
        self.assertEqual(len(self.memory.all_entries()), len(entries))

    async def test_add_update_remove_audit_uses_actual_actor_and_endpoint_qualified_source(self):
        shared_history = ConversationScope("onebot-v11", "group", "101")
        message = replace(self.message, canonical_scope=shared_history)
        other = replace(inbound(app="TEST_APP_B"), canonical_scope=shared_history)
        source = self.record(message)
        collision = self.record(other)
        self.assertNotEqual(source.canonical_message_id, collision.canonical_message_id)
        turn = self.make_turn(message)
        result = await self.call("memory_add", {"content": "preferred editor"}, turn=turn)
        memory_id = result["memory"]["id"]
        provenance = await self.app.db_call(official_memory_provenance, turn.context)
        self.assertEqual(provenance["source_message_id"], source.canonical_message_id)
        self.assertEqual(provenance["actor_key"], message.actor.key)
        self.assertIsNone(provenance["actor_user_id"])
        # The store update API is audited; no new model-facing update tool exists.
        updated = self.memory.update(memory_id, "preferred terminal", memory_scope_keys(turn.context, "user"),
                                     expected_version=1, **provenance)
        self.assertEqual(updated.version, 2)
        other_context = self.make_turn(other).context
        with self.assertRaises(LongTermMemoryError):
            self.memory.update(memory_id, "foreign mutation", memory_scope_keys(other_context, "user"),
                               expected_version=2, **official_memory_provenance(other_context))
        self.assertTrue((await self.call("memory_remove", {"memory_id": memory_id}, turn=turn))["ok"])
        audit = self.memory.audit([message.actor.key])
        self.assertEqual([item.action for item in audit], ["remove", "update", "create"])
        for mutation in audit:
            self.assertIsNone(mutation.actor_user_id)
            self.assertEqual(mutation.actor_key, message.actor.key)
            self.assertEqual(mutation.actor_principal_id, source.sender_principal_id)
            self.assertEqual(mutation.source_message_id, source.canonical_message_id)

    async def test_history_binding_shares_group_memory_but_never_old_or_other_personal_memory(self):
        history = ConversationScope("onebot-v11", "group", "101")
        message = replace(self.message, canonical_scope=history)
        turn = self.make_turn(message)
        shared, _ = self.memory.add("group:101", "group", "needle shared project", creator_user_id=7)
        self.memory.add("group:101:user:7", "user", "needle old QQ private secret", creator_user_id=7)
        other = replace(inbound(app="TEST_APP_B"), canonical_scope=history)
        await self.call("memory_add", {"content": "needle other actor private secret"}, turn=self.make_turn(other))
        own = await self.call("memory_add", {"content": "needle own private"}, turn=turn)
        listed = await self.call("memory_list", {}, turn=turn)
        self.assertEqual({item["id"] for item in listed["memories"]}, {shared.id, own["memory"]["id"]})
        result = await self.call("context_search", {"query": "needle"}, turn=turn)
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual({item["id"] for item in result["memories"]}, {shared.id, own["memory"]["id"]})
        for role in ("member", "owner", "admin"):
            privileged_platform_role = self.make_turn(replace(message, platform_role=role))
            self.assertEqual((await self.call("memory_add", {"scope": "group", "content": "overwrite"},
                                             turn=privileged_platform_role))["code"], "memory_permission_denied")
            self.assertFalse((await self.call("memory_remove", {"memory_id": shared.id},
                                              turn=privileged_platform_role))["ok"])
        self.assertEqual(self.memory.list_entries(["group:101"]), [shared])

    async def test_forged_domain_arguments_and_secrets_do_not_write(self):
        for args in ({"scope": inbound(app="TEST_OTHER").actor.key, "content": "bad"},
                     {"scope": "user", "actor_key": inbound(app="TEST_OTHER").actor.key, "content": "bad"},
                     {"content": "password: TEST_PASSWORD"}, {"content": "sk-TEST_KEY_MUST_NOT_PERSIST"}):
            self.assertFalse((await self.call("memory_add", args))["ok"])
        self.assertFalse((await self.call("memory_list", {"scope_key": "group:101"}))["ok"])
        self.assertFalse((await self.call("memory_remove", {"memory_id": True}))["ok"])
        self.assertFalse((await self.call("context_search", {"query": "needle", "scope_key": "group:101"}))["ok"])
        self.assertFalse((await self.call("context_expand", {"target": "t#1", "scope": "group:101"}))["ok"])
        self.assertEqual(self.memory.all_entries(), [])

    async def test_context_search_uses_actual_history_scope_and_filters_false_semantic_claims(self):
        current = self.record(self.message)
        foreign = self.record(inbound(app="TEST_APP_B"))
        semantic_message = self.record(replace(self.message, native_message_id="TEST_SEMANTIC",
                                               body=MessageBody((TextNode(0, "Stored semantic evidence"),))))
        own_memory = await self.call("memory_add", {"content": "Stored preference"})
        foreign_memory, _ = self.memory.add(inbound(app="TEST_APP_B").actor.key, "user", "foreign secret",
                                             creator_actor_key=inbound(app="TEST_APP_B").actor.key)
        def hit(handle, scope, kind="message"):
            return SimpleNamespace(source_handle=handle, scope_key=scope, source_type=kind,
                                   content="UNVERIFIED SECRET", score=0.8, metadata={"raw": "UNVERIFIED SECRET"})
        self.app.semantic_recall = SimpleNamespace(search=AsyncMock(return_value=[
            hit(f"msg#{foreign.canonical_message_id}", self.message.scope.key),
            hit(f"memory#{foreign_memory.id}", self.message.actor.key, "memory"),
            hit(f"msg#{semantic_message.canonical_message_id}", self.message.scope.key),
            hit(f"memory#{own_memory['memory']['id']}", self.message.actor.key, "memory"),
        ]))
        result = await self.call("context_search", {"query": "needle"})
        self.assertEqual([item["handle"] for item in result["messages"]], [f"msg#{current.canonical_message_id}"])
        self.assertEqual({item["text"] for item in result["semantic"]}, {"Stored semantic evidence", "Stored preference"})
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertNotIn("foreign", json.dumps(result))
        self.app.semantic_recall.search.assert_awaited_once_with(
            sorted({self.message.scope.key, *memory_scope_keys(self.turn.context)}), "needle", limit=10)

    async def test_expand_real_turns_and_episodes_cannot_cross_scope(self):
        self.record(self.message)
        current = self.journal.start_turn(self.message.scope, trigger_canonical_message_id=None,
                                          objective="current visible turn", provider="test", model="test")
        other = inbound(app="TEST_APP_B")
        for ordinal in range(1, 3):
            self.journal.start_turn(other.scope, trigger_canonical_message_id=None,
                                    objective=f"foreign turn secret {ordinal}", provider="test", model="test")
        visible = await self.call("context_expand", {"target": current.handle})
        self.assertIn("current visible turn", visible["record"])
        self.assertFalse((await self.call("context_expand", {"target": "t#2"}))["ok"])
        for index in range(18):
            self.record(replace(other, native_message_id=f"TEST_EPISODE_{index}",
                                body=MessageBody((TextNode(0, "foreign chapter secret " + "x" * 200),))))
        projection = self.chapters.build_projection(self.ledger, other.scope)
        self.assertTrue(projection.compartment_handles)
        target = "episode#" + projection.compartment_handles[0]
        self.assertFalse((await self.call("context_expand", {"target": target}))["ok"])
        own = await self.call("context_expand", {"target": target}, turn=self.make_turn(other))
        self.assertTrue(own["ok"])
        self.assertIn("foreign chapter secret", own["record"])


class NeutralToolImportTests(unittest.TestCase):
    def test_real_neutral_tools_execute_when_every_nonebot_import_is_blocked(self):
        script = r'''
import importlib.abc, sys
class BlockNative(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "nonebot" or fullname.startswith("nonebot."):
            raise AssertionError("unexpected native SDK: " + fullname)
sys.meta_path.insert(0, BlockNative())
import asyncio, json, tempfile
from pathlib import Path
from types import SimpleNamespace
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey, InboundMessage, PlatformFeatures
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.long_term_memory import LongTermMemoryStore
from src.plugins.ai_chat.skills import SkillRegistry
from src.plugins.ai_chat.tools import neutral_registry_for
from src.plugins.ai_chat.tools.state import ToolContext, TurnState
async def db_call(fn, *args, **kwargs): return fn(*args, **kwargs)
async def run():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "test.md").write_text("# Test\nStatic skill instructions")
        account = AccountKey("qq-official", "TEST_APP", "test")
        endpoint = EndpointKey(account, "private", "TEST_USER")
        actor = ActorKey(account, "c2c_user", "", "TEST_USER")
        inbound = InboundMessage(endpoint, actor, "TEST_EVENT", "TEST_MSG", "Test", "member",
            MessageBody((TextNode(0, "test"),)), False, None, 1000, None)
        app = SimpleNamespace(db_call=db_call, message_ledger=None,
            long_term_memory=LongTermMemoryStore(root / "memory.json"), skill_registry=SkillRegistry(root))
        context = ToolContext(SimpleNamespace(context=app), inbound=inbound,
            platform=SimpleNamespace(account=account, features=PlatformFeatures()),
            capabilities=frozenset({"self", "memory", "context"}))
        turn = TurnState(context)
        registry = neutral_registry_for(context)
        skill = json.loads(await registry.execute(turn, "use_skill", {"name": "test"}))
        assert skill["ok"] and "Static skill" in skill["instructions"]
        added = json.loads(await registry.execute(turn, "memory_add", {"content": "preferred editor"}))
        assert added["ok"]
        listed = json.loads(await registry.execute(turn, "memory_list", {}))
        assert len(listed["memories"]) == 1
        removed = json.loads(await registry.execute(turn, "memory_remove", {"memory_id": added["memory"]["id"]}))
        assert removed["ok"]
        app.message_ledger = SimpleNamespace(search_in_scope=lambda scope, query, limit: [])
        app.context_store = app.pin_store = app.semantic_recall = None
        history = json.loads(await registry.execute(turn, "context_search", {"query": "test"}))
        assert history["ok"] and history["messages"] == []
        app.settings = SimpleNamespace(turn_expand_max_chars=2000)
        app.turn_journal = SimpleNamespace(render_turn=lambda scope, ordinal, max_chars: "structured history")
        expanded = json.loads(await registry.execute(turn, "context_expand", {"target": "t#1"}))
        assert expanded["ok"] and expanded["record"] == "structured history"
        assert not any(name == "nonebot" or name.startswith("nonebot.") for name in sys.modules)
asyncio.run(run())
'''
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                                env={**os.environ, "PYTHON_DOTENV_DISABLED": "1"},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
