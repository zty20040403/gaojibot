from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import nonebot

nonebot.init()

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, PrivateMessageEvent
from src.plugins.ai_chat.command_handlers import CommandHandlers
from src.plugins.ai_chat.onebot_codec import scope_from_event
from src.plugins.ai_chat.tools.registry import ToolRegistry
from src.plugins.ai_chat.tools.reminders import HANDLERS
from src.plugins.ai_chat.tools.state import ToolContext, TurnState


def event(private=False):
    fields = dict(
        time=1,
        self_id=999,
        post_type="message",
        user_id=7,
        message_id=10,
        message=Message("remind"),
        original_message=Message("remind"),
        raw_message="remind",
        font=0,
        sender={"user_id": 7, "nickname": "Alice"},
    )
    if private:
        return PrivateMessageEvent(**fields, message_type="private", sub_type="friend")
    return GroupMessageEvent(
        **fields, message_type="group", sub_type="normal", group_id=100
    )


class ReminderToolHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = Mock()
        self.app = SimpleNamespace(reminder_store=self.store, message_ledger=Mock())
        self.app.message_ledger.principal_id_for_native.return_value = 17
        services = SimpleNamespace(context=self.app)
        services.commands = CommandHandlers(services)
        self.turn = TurnState(ToolContext(services, event(), frozenset({"reminders"})))
        self.registry = ToolRegistry(HANDLERS)
        self.due = datetime.now(timezone.utc) + timedelta(days=1)
        self.reminder = SimpleNamespace(
            handle="reminder#5",
            message="submit work",
            scheduled_for=int(self.due.timestamp()),
            status="pending",
            attempts=0,
        )
        self.store.create.return_value = self.reminder

    async def run_tool(self, name, **arguments):
        return json.loads(await self.registry.execute(self.turn, name, arguments))

    async def test_create_binds_host_scope_and_creator_not_model_supplied_fields(
        self,
    ) -> None:
        for private in (False, True):
            self.turn.context = replace(self.turn.context, event=event(private))
            self.store.create.reset_mock()
            result = await self.run_tool(
                "reminder_set",
                message="submit work",
                due_at=self.due.isoformat(),
                scope="foreign",
                creator_native_user_id=999,
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["reminder"]["handle"], "reminder#5")
            self.store.create.assert_called_once_with(
                scope_from_event(self.turn.context.event),
                creator_native_user_id=7,
                creator_principal_id=17,
                message="submit work",
                scheduled_for=int(self.due.timestamp()),
            )
        self.app.message_ledger = None
        await self.run_tool(
            "reminder_set", message="submit work", due_at=self.due.isoformat()
        )
        self.assertIsNone(self.store.create.call_args.kwargs["creator_principal_id"])

    async def test_list_and_cancel_are_current_scope_only(self) -> None:
        self.store.list_pending.return_value = [self.reminder]
        for private in (False, True):
            self.turn.context = replace(self.turn.context, event=event(private))
            scope = scope_from_event(self.turn.context.event)
            result = await self.run_tool("reminder_list", scope="foreign")
            self.assertEqual(result["reminders"][0]["handle"], "reminder#5")
            self.store.list_pending.assert_called_with(scope)
            for cancelled in (False, True):
                self.store.cancel.return_value = cancelled
                result = await self.run_tool(
                    "reminder_cancel", reminder_handle="reminder#5"
                )
                self.assertEqual(result["ok"], cancelled)
                self.assertEqual(result["cancelled"], cancelled)
                self.store.cancel.assert_called_with(scope, 5)

    async def test_invalid_handles_dates_and_store_validation_are_errors(self) -> None:
        for handle in ("5", "reminder#0", "reminder#-1", "garbage"):
            self.assertFalse(
                (await self.run_tool("reminder_cancel", reminder_handle=handle))["ok"]
            )
        self.store.cancel.assert_not_called()
        self.assertFalse(
            (await self.run_tool("reminder_set", due_at="not a date", message="work"))[
                "ok"
            ]
        )
        self.store.create.assert_not_called()
        self.store.create.side_effect = ValueError("scope quota reached")
        result = await self.run_tool(
            "reminder_set", due_at=self.due.isoformat(), message="work"
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "scope quota reached")

    async def test_missing_store_or_capability_is_unavailable(self) -> None:
        self.app.reminder_store = None
        for name in ("reminder_set", "reminder_list", "reminder_cancel"):
            self.assertEqual((await self.run_tool(name))["code"], "tool_unavailable")
        self.app.reminder_store = self.store
        self.turn.context = replace(self.turn.context, capabilities=frozenset())
        self.assertEqual(self.registry.definitions(self.turn.context), [])
        self.assertEqual(
            (await self.run_tool("reminder_list"))["code"], "tool_unavailable"
        )
        self.store.create.assert_not_called()
        self.store.cancel.assert_not_called()
        self.store.list_pending.assert_not_called()
