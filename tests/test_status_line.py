"""Tests for ephemeral 10-minute status/mood line computation."""

from __future__ import annotations

from dataclasses import dataclass
import unittest

from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.platform_types import AccountKey
from src.plugins.ai_chat.status_line import compute_status_line


@dataclass(frozen=True)
class _FakeMessage:
    canonical_message_id: int
    direction: str
    occurred_at: int
    rendered_text: str
    reply_to_canonical_message_id: int | None = None
    prompt_text: str = ""
    source_endpoint_key: str = ""
    sender_principal_id: int | None = None
    sender_display: str = "Alice"
    message_kind: str = "chat"
    body: MessageBody = MessageBody((TextNode(0, ""),))


class _FakeLedger:
    def __init__(self, rows):
        self._rows = list(rows)

    def history(self, scope, *, limit=120, newest=True, **kwargs):
        rows = sorted(self._rows, key=lambda r: r.canonical_message_id, reverse=newest)
        return rows[:limit]


class StatusLineTests(unittest.TestCase):
    scope = ConversationScope(
        "qq-official",
        "group",
        "TEST_GROUP",
        account=AccountKey("qq-official", "TEST_APP", "sandbox"),
    )

    def test_empty_window_returns_empty_string(self):
        ledger = _FakeLedger([
            _FakeMessage(1, "inbound", 1000, "old praise 谢谢", prompt_text="old praise 谢谢"),
        ])
        self.assertEqual(compute_status_line(ledger, self.scope, now=2000), "")

    def test_praise_and_pushback_affect_tone_and_expire(self):
        ledger = _FakeLedger([
            _FakeMessage(10, "outbound", 1900, "查好了", prompt_text="查好了"),
            _FakeMessage(11, "inbound", 1920, "谢谢 gaoji 太强了", prompt_text="谢谢 gaoji 太强了"),
        ])
        praised = compute_status_line(ledger, self.scope, now=1950)
        self.assertIn("被夸了 1 次", praised)
        self.assertEqual(compute_status_line(ledger, self.scope, now=2700), "")

        ledger._rows.extend([
            _FakeMessage(12, "inbound", 1960, "闭嘴吧你", reply_to_canonical_message_id=10, prompt_text="闭嘴吧你"),
            _FakeMessage(13, "inbound", 1970, "别吵了", prompt_text="别吵了"),
        ])
        pushed = compute_status_line(ledger, self.scope, now=1980)
        self.assertIn("被怼/叫闭嘴 2 次", pushed)
        self.assertIn("被 @/点名 2 次", pushed)

    def test_busy_mentions_and_broken_ledger_safe(self):
        rows = [
            _FakeMessage(i, "inbound", 1000 + i, f"gaoji #{i}", prompt_text=f"gaoji #{i}")
            for i in range(1, 7)
        ]
        busy = compute_status_line(_FakeLedger(rows), self.scope, now=1020)
        self.assertIn("被 @/点名 6 次", busy)
        self.assertIn("少废话", busy)

        class Broken:
            def history(self, *args, **kwargs):
                raise RuntimeError("boom")

        self.assertEqual(compute_status_line(Broken(), self.scope, now=1020), "")

    def test_human_quote_does_not_make_nearby_pushback_about_the_bot(self):
        ledger = _FakeLedger([
            _FakeMessage(1, "outbound", 1900, "查好了"),
            _FakeMessage(2, "inbound", 1920, "我觉得还行"),
            _FakeMessage(3, "inbound", 1940, "闭嘴吧你", reply_to_canonical_message_id=2),
        ])
        status = compute_status_line(ledger, self.scope, now=1950)
        self.assertIn("被 @/点名 0 次", status)
        self.assertNotIn("被怼", status)

    def test_turn_window_appends_status_line_without_changing_prefix(self):
        from types import SimpleNamespace
        from src.plugins.ai_chat.application.turn_context import turn_window
        from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey, InboundMessage

        endpoint = EndpointKey(self.scope.account, "group", "TEST_GROUP")
        actor = ActorKey(self.scope.account, "group_member", "TEST_GROUP", "TEST_USER")
        inbound = InboundMessage(
            endpoint=endpoint,
            actor=actor,
            event_id="evt-1",
            native_message_id="msg-1",
            sender_display="Alice",
            platform_role="",
            body=MessageBody((TextNode(0, "谢谢 gaoji"),)),
            mentions_bot=True,
            reply_to_native_message_id=None,
            occurred_at=1950,
            reply_credential=None,
        )

        class _Ledger(_FakeLedger):
            def canonical_id_for_native(self, *args, **kwargs):
                return 2

        class _Store:
            def tail_after(self, key):
                return 0

            def set_tail_after(self, key, val):
                pass

            def memory(self, key):
                return ""

        app = SimpleNamespace(
            message_ledger=_Ledger([
                _FakeMessage(1, "outbound", 1900, "查好了", prompt_text="查好了"),
                _FakeMessage(2, "inbound", 1950, "谢谢 gaoji", reply_to_canonical_message_id=1, prompt_text="谢谢 gaoji"),
            ]),
            conversations=_Store(),
        )
        _, memory, extra, _ = turn_window(app, inbound, SimpleNamespace(max_input_tokens=128_000))
        self.assertEqual(memory, "")
        self.assertIn("[状态]", extra)
        self.assertIn("被夸了 1 次", extra)


if __name__ == "__main__":
    unittest.main()
