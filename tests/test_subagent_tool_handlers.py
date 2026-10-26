from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import nonebot

nonebot.init()

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
from src.plugins.ai_chat.onebot_codec import scope_from_event
from src.plugins.ai_chat.tools.registry import ToolRegistry
from src.plugins.ai_chat.tools.state import SubAgentActions, ToolContext, TurnState
from src.plugins.ai_chat.tools.subagents import HANDLERS


class SubAgentToolHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.actions = SubAgentActions(
            delegate=AsyncMock(return_value={"status": "queued", "task_id": 7}),
            run=AsyncMock(return_value="task#7 queued"),
            resume=AsyncMock(return_value="task#7 resumed"),
        )
        self.app = SimpleNamespace(subagent_coordinator=object())
        self.turn = TurnState(
            ToolContext(
                SimpleNamespace(context=self.app), Mock(), frozenset({"subagents"})
            ),
            subagent_actions=self.actions,
        )
        self.registry = ToolRegistry(HANDLERS)

    async def run_tool(self, name: str, **arguments) -> dict:
        return json.loads(await self.registry.execute(self.turn, name, arguments))

    async def test_single_delegate_limit_and_validation_remain_per_turn(self) -> None:
        self.assertFalse(
            (await self.run_tool("delegate_agent", role="researcher", objective=""))[
                "ok"
            ]
        )
        self.assertEqual(self.turn.subagent_delegations, 0)
        for _ in range(3):
            response = await self.run_tool(
                "delegate_agent", role=" researcher ", objective=" compare "
            )
            self.assertTrue(response["ok"])
            self.assertEqual(response["status"], "queued")
        self.assertFalse(
            (
                await self.run_tool(
                    "delegate_agent", role="researcher", objective="again"
                )
            )["ok"]
        )
        self.assertEqual(self.actions.delegate.await_count, 3)
        self.actions.delegate.assert_awaited_with("researcher", "compare")
        self.assertEqual(TurnState(self.turn.context).subagent_delegations, 0)

    async def test_workflow_and_resume_share_one_launch_gate(self) -> None:
        self.assertFalse((await self.run_tool("run_subagents", goal=""))["ok"])
        self.assertFalse(self.turn.subagent_task_started)
        self.assertTrue(
            (await self.run_tool("run_subagents", goal=" build report "))["ok"]
        )
        self.actions.run.assert_awaited_once_with("build report")
        self.assertFalse((await self.run_tool("run_subagents", goal="again"))["ok"])
        self.assertFalse((await self.run_tool("resume_subagent", task_id=7))["ok"])
        self.actions.resume.assert_not_awaited()
        self.turn.subagent_task_started = False
        for task_id in ("bad", 0, -1):
            self.assertFalse(
                (await self.run_tool("resume_subagent", task_id=task_id))["ok"]
            )
        self.assertTrue((await self.run_tool("resume_subagent", task_id="7"))["ok"])
        self.actions.resume.assert_awaited_once_with(7)
        self.assertFalse((await self.run_tool("run_subagents", goal="again"))["ok"])

    async def test_errors_and_cancellation_do_not_reset_launch_guards(self) -> None:
        self.actions.delegate.side_effect = ValueError("unknown role")
        self.assertFalse(
            (await self.run_tool("delegate_agent", role="invalid", objective="work"))[
                "ok"
            ]
        )
        self.assertEqual(self.turn.subagent_delegations, 1)
        self.actions.resume.side_effect = RuntimeError("task is not resumable")
        self.assertFalse((await self.run_tool("resume_subagent", task_id=7))["ok"])
        self.assertTrue(self.turn.subagent_task_started)
        self.turn.subagent_task_started = False
        self.actions.run.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.run_tool("run_subagents", goal="work")
        self.assertTrue(self.turn.subagent_task_started)

    async def test_missing_actions_or_capability_never_launch_a_task(self) -> None:
        self.turn.subagent_actions = None
        for name, arguments in (
            ("delegate_agent", {"objective": "work"}),
            ("run_subagents", {"goal": "work"}),
            ("resume_subagent", {"task_id": 7}),
        ):
            self.assertFalse((await self.run_tool(name, **arguments))["ok"])
        self.assertEqual(self.turn.subagent_delegations, 0)
        self.assertFalse(self.turn.subagent_task_started)
        self.turn.subagent_actions = self.actions
        self.turn.context = replace(self.turn.context, capabilities=frozenset())
        self.assertEqual(
            (await self.run_tool("run_subagents", goal="work"))["code"],
            "tool_unavailable",
        )
        self.actions.delegate.assert_not_awaited()
        self.actions.run.assert_not_awaited()
        self.actions.resume.assert_not_awaited()

    async def test_chat_dispatch_binds_existing_coordinator_with_current_scope_and_user(
        self,
    ) -> None:
        import src.plugins.ai_chat as ai_chat

        current = GroupMessageEvent(
            time=1,
            self_id=999,
            post_type="message",
            sub_type="normal",
            user_id=7,
            message_type="group",
            message_id=10,
            group_id=100,
            message=Message("help"),
            original_message=Message("help"),
            raw_message="help",
            font=0,
            sender={"user_id": 7, "nickname": "Alice", "role": "member"},
        )
        coordinator = SimpleNamespace(
            dispatcher=None,
            prepare_entry=AsyncMock(return_value=None),
            delegate=AsyncMock(return_value={"summary": "done"}),
            run=AsyncMock(return_value="done"),
            resume=AsyncMock(return_value="done"),
        )
        task_store = SimpleNamespace(recent=Mock(return_value=[]))
        settings = replace(
            ai_chat.app_context.settings,
            subagent_entry_enabled=False,
            tool_intent_enabled=False,
            search_enabled=False,
            voice_enabled=False,
            ocr_enabled=False,
        )
        for name, arguments, operation in (
            (
                "delegate_agent",
                {"role": "researcher", "objective": "work"},
                coordinator.delegate,
            ),
            ("run_subagents", {"goal": "work"}, coordinator.run),
            ("resume_subagent", {"task_id": 7}, coordinator.resume),
        ):
            with self.subTest(tool=name):

                async def model(_text, _history, tools, execute_tool, **_kwargs):
                    self.assertIn(name, [t["function"]["name"] for t in tools])
                    result = json.loads(await execute_tool(name, arguments))
                    self.assertTrue(result["ok"])
                    return "done"

                with (
                    patch.object(ai_chat.app_context, "settings", settings),
                    patch.object(
                        ai_chat.app_context, "subagent_coordinator", coordinator
                    ),
                    patch.object(ai_chat.app_context, "subagent_store", task_store),
                    patch.object(ai_chat.app_context, "message_ledger", None),
                    patch.object(ai_chat.app_context, "context_store", None),
                    patch.object(ai_chat.app_context, "turn_journal", None),
                    patch.object(ai_chat.app_context, "pin_store", None),
                    patch.object(ai_chat.app_context, "source_store", None),
                    patch.object(ai_chat.app_context, "vision_worker", None),
                    patch.object(ai_chat.handlers, "video_analyzer", None),
                    patch.object(
                        ai_chat.handlers.chat,
                        "_group_turn_context_plan",
                        return_value=None,
                    ),
                    patch.object(
                        ai_chat.handlers.commands,
                        "_current_long_term_memory",
                        return_value="",
                    ),
                    patch.object(ai_chat.app_context.memory, "append_turn"),
                    patch.object(ai_chat.app_context.group_context, "append"),
                    patch(
                        "src.plugins.ai_chat.tool_executor.ask_deepseek_with_tools",
                        new=model,
                    ),
                ):
                    await ai_chat.handlers.tools._ask_ai(
                        AsyncMock(), current, "help", available_image_sources=[]
                    )
                operation.assert_awaited_once()
                self.assertEqual(
                    operation.call_args.kwargs["scope_key"],
                    scope_from_event(current).key,
                )
                self.assertEqual(operation.call_args.kwargs["requester_user_id"], 7)
                if name != "resume_subagent":
                    packet = operation.call_args.kwargs["context_packet"]
                    self.assertEqual(packet.scope_key, scope_from_event(current).key)
                    self.assertEqual(packet.requester_user_id, 7)
                    self.assertEqual(packet.objective, "work")
