from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import nonebot

nonebot.init()

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.onebot_codec import record_onebot_event, scope_from_event
from src.plugins.ai_chat.pins import PinStore
from src.plugins.ai_chat.tools.context import HANDLERS
from src.plugins.ai_chat.tools.registry import ToolRegistry
from src.plugins.ai_chat.tools.state import ToolContext, TurnState


def event(
    group: int = 100, message_id: int = 1, text: str = "database current"
) -> GroupMessageEvent:
    return GroupMessageEvent(
        time=message_id,
        self_id=999,
        post_type="message",
        sub_type="normal",
        user_id=7,
        message_type="group",
        message_id=message_id,
        message=Message(text),
        original_message=Message(text),
        raw_message=text,
        font=0,
        sender={"user_id": 7, "nickname": "Alice", "role": "member"},
        group_id=group,
    )


class ContextToolHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.ledger = MessageLedger(":memory:")
        self.pins = PinStore(":memory:")
        self.addCleanup(self.ledger.close)
        self.addCleanup(self.pins.close)
        self.event = event()
        self.scope = scope_from_event(self.event)
        self.current = record_onebot_event(self.ledger, self.event)
        self.foreign = record_onebot_event(
            self.ledger, event(200, 2, "database foreign secret")
        )
        self.memory_scopes = [self.scope.key + ":user:7", self.scope.key + ":shared"]
        self.commands = Mock()
        self.commands._canonical_message_id.side_effect = lambda value: int(
            value.removeprefix("msg#")
        )
        self.commands._memory_scope_keys.return_value = self.memory_scopes
        self.app = SimpleNamespace(
            message_ledger=self.ledger,
            pin_store=self.pins,
            context_store=None,
            turn_journal=None,
            semantic_recall=None,
            long_term_memory=Mock(),
            logger=Mock(),
            settings=SimpleNamespace(turn_expand_max_chars=2000),
        )
        self.app.long_term_memory.list_entries.return_value = []
        self.bot = AsyncMock()
        self.turn = TurnState(
            ToolContext(
                SimpleNamespace(context=self.app, commands=self.commands),
                self.event,
                frozenset({"context", "pins", "members"}),
            ),
            bot=self.bot,
        )
        self.registry = ToolRegistry(HANDLERS)

    async def run_tool(self, name: str, arguments: dict) -> dict:
        return json.loads(await self.registry.execute(self.turn, name, arguments))

    async def test_search_keeps_lexical_and_semantic_results_in_current_scope(
        self,
    ) -> None:
        def hit(handle, scope, text):
            return SimpleNamespace(
                source_handle=handle,
                scope_key=scope,
                source_type="message",
                content=text,
                score=0.9,
                metadata={},
            )

        self.app.semantic_recall = SimpleNamespace(
            search=AsyncMock(
                return_value=[
                    hit(
                        f"msg#{self.current.canonical_message_id}",
                        self.scope.key,
                        "duplicate",
                    ),
                    hit(
                        "msg#other-group",
                        scope_from_event(event(200)).key,
                        "foreign secret",
                    ),
                    hit(
                        "memory#other-person",
                        self.scope.key + ":user:8",
                        "other person's memory",
                    ),
                    hit(
                        "memory#current",
                        self.memory_scopes[0],
                        "current person's memory",
                    ),
                ]
            )
        )
        result = await self.run_tool(
            "context_search", {"query": "database", "limit": 5}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(
            [item["handle"] for item in result["messages"]],
            [f"msg#{self.current.canonical_message_id}"],
        )
        self.assertEqual(
            [item["handle"] for item in result["semantic"]], ["memory#current"]
        )
        self.assertNotIn("secret", json.dumps(result))
        self.app.semantic_recall.search.assert_awaited_once_with(
            sorted({self.scope.key, *self.memory_scopes}), "database", limit=10
        )

    async def test_semantic_failure_preserves_lexical_evidence(self) -> None:
        self.app.semantic_recall = SimpleNamespace(
            search=AsyncMock(side_effect=RuntimeError("offline"))
        )
        result = await self.run_tool("context_search", {"query": "database"})
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["messages"]), 1)
        self.assertEqual(result["semantic"], [])

    async def test_forged_message_handle_cannot_pin_or_unpin_other_group(self) -> None:
        other_scope = scope_from_event(event(200))
        foreign_id = self.foreign.canonical_message_id
        self.pins.pin(self.ledger, other_scope, foreign_id, pinned_by_principal_id=None)
        for name in ("pin_message", "unpin_message"):
            result = await self.run_tool(name, {"message_handle": f"msg#{foreign_id}"})
            self.assertFalse(result["ok"])
        self.assertEqual(len(self.pins.search(self.ledger, other_scope, "database")), 1)
        current_handle = f"msg#{self.current.canonical_message_id}"
        self.assertTrue(
            (await self.run_tool("pin_message", {"message_handle": current_handle}))[
                "ok"
            ]
        )
        self.assertTrue(
            (await self.run_tool("unpin_message", {"message_handle": current_handle}))[
                "ok"
            ]
        )

    async def test_expand_passes_scope_and_reports_missing_evidence(self) -> None:
        self.app.context_store = SimpleNamespace(expand=Mock(return_value=None))
        self.app.turn_journal = SimpleNamespace(render_turn=Mock(return_value=None))
        self.assertFalse(
            (await self.run_tool("context_expand", {"target": "episode#other"}))["ok"]
        )
        self.app.context_store.expand.assert_called_once_with(
            self.ledger, self.scope, "other", max_chars=2000
        )
        self.assertFalse(
            (await self.run_tool("context_expand", {"target": "t#17"}))["ok"]
        )
        self.app.turn_journal.render_turn.assert_called_once_with(
            self.scope, 17, max_chars=2000
        )

    async def test_group_members_are_current_group_scoped_and_use_canonical_mentions(
        self,
    ) -> None:
        self.bot.get_group_member_list.return_value = [
            {"user_id": 7, "nickname": "Alice", "role": "member"},
            {"user_id": 8, "nickname": "Bob", "role": "admin"},
            {"user_id": "not-an-id", "nickname": "invalid"},
        ]
        result = await self.run_tool("group_members", {"query": "alice"})
        self.bot.get_group_member_list.assert_awaited_once_with(group_id=100)
        self.assertEqual(result["count"], 1)
        self.assertEqual(
            result["members"][0]["principal"],
            f"[mention#{self.current.sender_principal_id}]",
        )
        self.assertNotIn("user_id", result["members"][0])
