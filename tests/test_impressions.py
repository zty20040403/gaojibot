"""Tests for per-group person impression cards (/work/.gaoji/self/people/*.md)."""

from __future__ import annotations

from dataclasses import dataclass
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.plugins.ai_chat.application.turn_context import turn_window
from src.plugins.ai_chat.application_maintenance import ApplicationMaintenance
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey, InboundMessage
from src.plugins.ai_chat.self_files import SelfFilesStore
from src.plugins.ai_chat.tools.impressions import impression
from src.plugins.ai_chat.tools.state import ToolContext, TurnState


@dataclass(frozen=True)
class _FakeMsg:
    canonical_message_id: int
    direction: str
    occurred_at: int
    rendered_text: str
    sender_actor_key: str
    sender_display: str
    reply_to_canonical_message_id: int | None = None
    source_endpoint_key: str = ""
    sender_principal_id: int | None = None
    prompt_text: str = ""
    message_kind: str = "chat"
    body: MessageBody = MessageBody((TextNode(0, ""),))


class _FakeLedger:
    def __init__(self, rows):
        self._rows = list(rows)

    def history(self, scope, *, limit=120, newest=True, after_id=0, **kwargs):
        filtered = [r for r in self._rows if r.canonical_message_id > after_id]
        filtered.sort(key=lambda r: r.canonical_message_id, reverse=newest)
        return filtered[:limit]

    def canonical_id_for_native(self, *args, **kwargs):
        return None


class _FakeConversations:
    def __init__(self):
        self.appended = []

    def tail_after(self, key):
        return 0

    def set_tail_after(self, key, val):
        pass

    def memory(self, key):
        return ""

    def write_memory(self, scope_key, text, *, append=False):
        self.appended.append((scope_key, text, append))
        return text


class ImpressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SelfFilesStore(self.tmp.name)
        acc = AccountKey("qq-official", "TEST_APP", "sandbox")
        self.ep1 = EndpointKey(acc, "group", "TEST_G1")
        self.ep2 = EndpointKey(acc, "group", "TEST_G2")
        self.actor1 = ActorKey(acc, "group_member", "TEST_G1", "TEST_U1")
        self.actor2 = ActorKey(acc, "group_member", "TEST_G1", "TEST_U2")

    def tearDown(self):
        self.tmp.cleanup()

    def test_cross_group_isolation_batch_cap_and_corrupt_tolerance(self):
        # Write 8 people in G1 and 1 in G2
        for idx in range(8):
            acc = self.ep1.account
            actor = ActorKey(acc, "group_member", "TEST_G1", f"U{idx}")
            self.store.write_person(self.ep1.scope.key, actor_key=actor.key, display=f"User{idx}", body=f"擅长 {idx}")
        self.store.write_person(self.ep2.scope.key, actor_key=self.actor1.key, display="Alice", body="G2秘密")

        # Corrupt file is ignored cleanly
        bad_file = self.store.scope_dir(self.ep1.scope.key) / "people" / "broken.md"
        bad_file.write_bytes(b"\xff\xfe\x00\x00")
        self.assertEqual(self.store.read_person(self.ep1.scope.key, actor_key=self.actor1.key), "")

        # Only speakers in batch are selected, capped at 6
        speakers = [(ActorKey(self.ep1.account, "group_member", "TEST_G1", f"U{i}").key, f"User{i}") for i in range(8)]
        cards = self.store.select_impressions_for_batch(self.ep1.scope.key, speakers)
        self.assertEqual(len(cards), 6)
        self.assertTrue(all("G2秘密" not in c for c in cards))

    async def test_impression_tool_note_read_forget_and_turn_window(self):
        app = SimpleNamespace(
            self_files=self.store,
            sandbox_manager=None,
            message_ledger=_FakeLedger([]),
            conversations=_FakeConversations(),
        )
        inbound = InboundMessage(
            endpoint=self.ep1,
            actor=self.actor1,
            event_id="e1",
            native_message_id="m1",
            sender_display="Alice",
            platform_role="",
            body=MessageBody((TextNode(0, "你记了我啥"),)),
            mentions_bot=True,
            reply_to_native_message_id=None,
            occurred_at=1000,
            reply_credential=None,
        )
        ctx = ToolContext(services=SimpleNamespace(context=app), inbound=inbound, platform=SimpleNamespace(account=self.ep1.account), capabilities=frozenset({"memory"}))
        turn = TurnState(context=ctx, model_profile=SimpleNamespace(name="test"), user_text="hi")

        res_note = await impression(turn, {"action": "note", "note": "叫老A，懂Rust，爱玩梗，很熟"})
        self.assertIn('"ok": true', res_note)
        res_read = await impression(turn, {"action": "read"})
        self.assertIn("懂Rust", res_read)

        # turn_window includes Alice's card in G1, not in G2
        _, _, extra1, _ = turn_window(app, inbound, SimpleNamespace(max_input_tokens=128_000))
        self.assertIn("[对说话人的印象]", extra1)
        self.assertIn("懂Rust", extra1)

        inbound_g2 = InboundMessage(
            endpoint=self.ep2,
            actor=ActorKey(self.ep2.account, "group_member", "TEST_G2", "TEST_U1"),
            event_id="e2",
            native_message_id="m2",
            sender_display="Alice",
            platform_role="",
            body=MessageBody((TextNode(0, "hi"),)),
            mentions_bot=True,
            reply_to_native_message_id=None,
            occurred_at=1000,
            reply_credential=None,
        )
        _, _, extra2, _ = turn_window(app, inbound_g2, SimpleNamespace(max_input_tokens=128_000))
        self.assertNotIn("懂Rust", extra2)

        # Forget deletes the card in G1
        res_forget = await impression(turn, {"action": "forget"})
        self.assertIn('"forgotten": true', res_forget)
        self.assertEqual(self.store.read_person(self.ep1.scope.key, actor_key=self.actor1.key), "")

    async def test_consolidation_updates_cards_and_events(self):
        rows = [
            _FakeMsg(i, "inbound", 1000 + i, f"聊点技术 #{i}", self.actor1.key, "Alice", prompt_text=f"聊点技术 #{i}")
            for i in range(1, 8)
        ]
        conv = _FakeConversations()
        app = SimpleNamespace(
            settings=SimpleNamespace(dream_profile="cheap"),
            model_catalog=SimpleNamespace(resolve_runtime=lambda **kw: SimpleNamespace(name="cheap")),
            self_files=self.store,
            message_ledger=_FakeLedger(rows),
            conversations=conv,
            usage_store=None,
            sandbox_manager=None,
            db_call=lambda fn, *args, **kwargs: _async_call(fn, *args, **kwargs),
        )
        maint = ApplicationMaintenance()
        maint.bind(app)
        llm_reply = '{"people": [{"name": "Alice", "card": "熟人，爱聊NixOS，下周要面试"}], "events": ["Alice：下周面试（11-18 #7）"]}'
        with patch("src.plugins.ai_chat.application_maintenance.ask_deepseek", return_value=llm_reply):
            ok = await maint.consolidate_scope(self.ep1.scope, now=20000)
        self.assertTrue(ok)
        self.assertIn("下周要面试", self.store.read_person(self.ep1.scope.key, actor_key=self.actor1.key))
        self.assertEqual(len(conv.appended), 1)
        self.assertIn("Alice：下周面试", conv.appended[0][1])


async def _async_call(fn, *args, **kwargs):
    return fn(*args, **kwargs)


if __name__ == "__main__":
    unittest.main()
