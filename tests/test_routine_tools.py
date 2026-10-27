from __future__ import annotations

from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import nonebot

nonebot.init()

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, PrivateMessageEvent
from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.quota import UsageStore
from src.plugins.ai_chat.routines import RoutineStore
from src.plugins.ai_chat.routine_commands import command_result
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from src.plugins.ai_chat.tools import domain_registry
from src.plugins.ai_chat.tools.state import ToolContext, TurnState
from tests.postgres_fixture import postgres_for_test


def event(*, actor=201, group=101, bot=301):
    common = dict(
        time=1,
        self_id=bot,
        post_type="message",
        user_id=actor,
        message_id=5,
        message=Message("test"),
        original_message=Message("test"),
        raw_message="test",
        font=0,
        sender={"user_id": actor, "nickname": "test"},
    )
    if group is None:
        return PrivateMessageEvent(**common, sub_type="friend", message_type="private")
    return GroupMessageEvent(
        **common, sub_type="normal", message_type="group", group_id=group
    )


class RoutineToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        db = postgres_for_test(self)
        stores = (
            DurableJobStore(db),
            DeliveryStore(db, recover_interrupted=False),
            UsageStore(db),
        )
        for store in stores:
            self.addCleanup(store.close)
        routine_store = RoutineStore(
            db, jobs=stores[0], outbox=stores[1], usage=stores[2]
        )
        self.addCleanup(routine_store.close)
        self.app = SimpleNamespace(routine_store=routine_store, db_call=DatabaseCalls())
        self.addAsyncCleanup(self.app.db_call.close)
        self.services = SimpleNamespace(
            context=self.app, group_enabled=Mock(return_value=True)
        )
        self.event = event()
        self.context = ToolContext(self.services, self.event, frozenset({"routines"}))

    async def call(self, name, arguments, context=None):
        return json.loads(
            await domain_registry.execute(
                TurnState(context or self.context), name, arguments
            )
        )

    async def test_tools_create_list_and_edit_with_owner_version(self):
        result = await self.call(
            "routine_set", {"name": "daily", "cron": "0 9 * * *", "prompt": "Summarize"}
        )
        self.assertTrue(result["ok"])
        item = (await self.call("routine_list", {}))["routines"][0]
        self.assertEqual(item["budget"]["charged_tokens"], 0)
        edited = await self.call(
            "routine_set",
            {
                "name": "daily",
                "cron": "0 10 * * *",
                "prompt": "News",
                "expected_version": 1,
            },
        )
        self.assertEqual(edited["routine"]["version"], 2)
        stale = await self.call(
            "routine_cancel", {"routine_handle": item["handle"], "expected_version": 1}
        )
        self.assertFalse(stale["ok"])
        other = replace(self.context, event=event(actor=202))
        self.assertEqual((await self.call("routine_list", {}, other))["routines"], [])
        self.assertFalse(
            (
                await self.call(
                    "routine_cancel",
                    {"routine_handle": item["handle"], "expected_version": 2},
                    other,
                )
            )["ok"]
        )
        self.assertTrue(
            (
                await self.call(
                    "routine_cancel",
                    {"routine_handle": item["handle"], "expected_version": 2},
                )
            )["ok"]
        )

    async def test_schema_rejects_forged_owner_and_string_budget(self):
        for extra in (
            {"creator_id": "202"},
            {"scope_key": "another"},
            {"max_tokens": "2000"},
            {"daily_runs": True},
        ):
            result = await self.call(
                "routine_set",
                {"name": "daily", "cron": "0 9 * * *", "prompt": "Summary", **extra},
            )
            self.assertFalse(result["ok"])
        self.assertEqual((await self.call("routine_list", {}))["routines"], [])

    async def test_disabled_groups_background_runs_and_host_toggle_cannot_execute(self):
        self.services.group_enabled.return_value = False
        self.assertFalse((await self.call("routine_list", {}))["ok"])
        self.services.group_enabled.return_value = True
        self.assertFalse(
            (
                await self.call(
                    "routine_list", {}, replace(self.context, routine_run_id="running")
                )
            )["ok"]
        )
        with patch(
            "src.plugins.ai_chat.tools.registry.tool_enabled", return_value=False
        ):
            self.assertFalse((await self.call("routine_list", {}))["ok"])
        self.app.routine_store = None
        self.assertFalse((await self.call("routine_list", {}))["ok"])

    async def test_private_routine_does_not_appear_in_group_or_another_bot(self):
        private = replace(self.context, event=event(group=None))
        self.assertTrue(
            (
                await self.call(
                    "routine_set",
                    {"name": "private", "cron": "0 9 * * *", "prompt": "Summary"},
                    private,
                )
            )["ok"]
        )
        self.assertEqual((await self.call("routine_list", {}))["routines"], [])
        other = replace(private, event=event(group=None, bot=302))
        self.assertEqual((await self.call("routine_list", {}, other))["routines"], [])

    async def test_slash_command_uses_same_scoped_versioned_api(self):
        created = await command_result(
            self.services, self.event, "添加 晨报 | 0 9 * * * | 汇总群聊"
        )
        self.assertIn("routine#1 v1", created)
        self.assertIn("晨报", await command_result(self.services, self.event, "列表"))
        updated = await command_result(
            self.services, self.event, "修改 routine#1 v1 | 0 10 * * * | 汇总新闻"
        )
        self.assertIn("v2", updated)
        stale = await command_result(self.services, self.event, "取消 routine#1 v1")
        self.assertIn("version changed", stale)
        self.assertIn(
            "已取消",
            await command_result(self.services, self.event, "取消 routine#1 v2"),
        )
        self.assertIn("关闭", await command_result(self.services, self.event, "列表"))
        self.assertIn(
            "请填写", await command_result(self.services, self.event, "取消 wrong")
        )
        self.assertIn(
            "/例行",
            await command_result(self.services, self.event, "添加 missing-fields"),
        )
