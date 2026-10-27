from __future__ import annotations

import asyncio
import json
import unittest
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import nonebot
from src.bot_storage import DatabaseCalls

nonebot.init()

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
from src.plugins.ai_chat import tool_policy
from src.plugins.ai_chat.agent_tools import AGENT_TOOL_METHODS, AgentToolExecutor
from src.plugins.ai_chat.ai_tools import SANDBOX_TOOLS, TOOL_DEFINITIONS
from src.plugins.ai_chat.tools import available_tools
from src.plugins.ai_chat.tools.executor import HANDLERS
from src.plugins.ai_chat.tools.registry import ToolRegistry, FunctionTool
from src.plugins.ai_chat.tools.state import ToolContext, TurnState


class ExecutorToolHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        event = GroupMessageEvent(
            time=1,
            self_id=999,
            post_type="message",
            message_type="group",
            sub_type="normal",
            user_id=7,
            group_id=100,
            message_id=10,
            message=Message("build"),
            original_message=Message("build"),
            raw_message="build",
            font=0,
            sender={"user_id": 7, "nickname": "Alice"},
        )
        self.executor = AgentToolExecutor(
            bot=AsyncMock(),
            event=event,
            owner="group:100",
            sandbox_manager=Mock(),
            max_file_bytes=100000,
            browser_manager=Mock(),
            source_store=Mock(),
        )
        self.app = SimpleNamespace(settings=SimpleNamespace(sandbox_backend="oci"))
        self.app.db_call = DatabaseCalls()
        self.addAsyncCleanup(self.app.db_call.close)
        self.turn = TurnState(
            ToolContext(
                SimpleNamespace(context=self.app),
                event,
                frozenset({"sandbox", "conversation", "browser", "shared_content"}),
                self.executor,
            )
        )
        self.registry = ToolRegistry(HANDLERS)
        override = patch.dict(tool_policy._TOOL_ENABLED_OVERRIDES, {}, clear=True)
        override.start()
        self.addCleanup(override.stop)

    async def test_every_registered_adapter_reaches_its_actual_executor_method(self):
        self.assertEqual(
            {item.definition["function"]["name"] for item in HANDLERS},
            set(AGENT_TOOL_METHODS),
        )
        for name, method in AGENT_TOOL_METHODS.items():
            with self.subTest(name=name):
                capabilities = {"sandbox", "conversation", "browser", "shared_content"}
                if name == "view_bilibili":
                    capabilities.remove("shared_content")
                self.turn.context = replace(
                    self.turn.context, capabilities=frozenset(capabilities)
                )
                arguments = {"sentinel": name}
                expected = json.dumps({"ok": True, "handler": method})
                with patch.object(
                    self.executor, method, new=AsyncMock(return_value=expected)
                ) as handler:
                    result = await self.registry.execute(self.turn, name, arguments)
                self.assertEqual(result, expected)
                handler.assert_awaited_once_with(arguments)

    async def test_unadvertised_sandbox_and_browser_tools_cannot_bypass_registry(self):
        self.turn.context = replace(
            self.turn.context, capabilities=frozenset({"conversation"})
        )
        with patch.object(self.executor, "execute", new=AsyncMock()) as execute:
            for definition in (
                *SANDBOX_TOOLS,
                *(
                    h.definition
                    for h in HANDLERS
                    if h.definition["function"]["name"].startswith("browser_")
                ),
            ):
                name = definition["function"]["name"]
                result = json.loads(
                    await self.registry.execute(
                        self.turn, name, {"approval": "granted"}
                    )
                )
                self.assertEqual(result["code"], "tool_unavailable")
            execute.assert_not_awaited()
        visible = {
            item["function"]["name"]
            for item in self.registry.definitions(self.turn.context)
        }
        self.assertIn("say", visible)
        self.assertIn("search_messages", visible)
        self.assertNotIn("sandbox_exec", visible)

    async def test_missing_executor_or_dependency_is_never_advertised_or_called(self):
        original = self.turn.context
        self.turn.context = replace(original, agent_executor=None)
        self.assertEqual(self.registry.definitions(self.turn.context), [])
        for handler in HANDLERS:
            name = handler.definition["function"]["name"]
            self.assertEqual(
                json.loads(await self.registry.execute(self.turn, name, {}))["code"],
                "tool_unavailable",
            )
        self.turn.context = original
        for dependency, tool in (
            ("sandbox_manager", "sandbox_exec"),
            ("browser_manager", "browser_click"),
            ("source_store", "inspect_shared_content"),
        ):
            with (
                patch.object(self.executor, dependency, None),
                patch.object(self.executor, "execute", new=AsyncMock()) as execute,
            ):
                self.assertNotIn(
                    tool,
                    {
                        item["function"]["name"]
                        for item in self.registry.definitions(original)
                    },
                )
                self.assertFalse(
                    json.loads(await self.registry.execute(self.turn, tool, {}))["ok"]
                )
                execute.assert_not_awaited()

    async def test_global_disable_is_rechecked_after_catalog_generation(self):
        self.assertIn(
            "sandbox_exec",
            {
                item["function"]["name"]
                for item in self.registry.definitions(self.turn.context)
            },
        )
        tool_policy.set_tool_enabled("sandbox_exec", False)
        with patch.object(self.executor, "execute", new=AsyncMock()) as execute:
            result = json.loads(
                await self.registry.execute(self.turn, "sandbox_exec", {})
            )
            self.assertEqual(result["code"], "tool_unavailable")
            execute.assert_not_awaited()

    async def test_vm_projection_does_not_mutate_docker_definitions(self):
        original = deepcopy(SANDBOX_TOOLS)
        self.app.settings.sandbox_backend = "vm"
        vm = {
            item["function"]["name"]: item
            for item in self.registry.definitions(self.turn.context)
        }
        self.assertNotIn("nix_search", vm)
        self.assertIn("Debian", vm["sandbox_create"]["function"]["description"])
        self.assertIn(
            "apt",
            vm["sandbox_exec"]["function"]["parameters"]["properties"]["packages"][
                "description"
            ],
        )
        with patch.object(self.executor, "execute", new=AsyncMock()) as execute:
            self.assertEqual(
                json.loads(await self.registry.execute(self.turn, "nix_search", {}))[
                    "code"
                ],
                "tool_unavailable",
            )
            execute.assert_not_awaited()
        vm["sandbox_exec"]["function"]["description"] = "changed by caller"
        self.app.settings.sandbox_backend = "oci"
        docker = {
            item["function"]["name"]: item
            for item in self.registry.definitions(self.turn.context)
        }
        self.assertIn("nix_search", docker)
        for definition in original:
            self.assertEqual(docker[definition["function"]["name"]], definition)
        self.assertEqual(SANDBOX_TOOLS, original)

    def test_catalog_order_is_stable_when_capabilities_change(self):
        order = [item["function"]["name"] for item in TOOL_DEFINITIONS]
        for capabilities in (
            {"sandbox"},
            {"conversation", "browser"},
            {"conversation", "shared_content"},
        ):
            context = replace(self.turn.context, capabilities=frozenset(capabilities))
            visible = [item["function"]["name"] for item in available_tools(context)]
            self.assertEqual(visible, [name for name in order if name in visible])
            self.assertEqual(len(visible), len(set(visible)))
            if "conversation" in capabilities:
                self.assertEqual(
                    "view_bilibili" in visible, "shared_content" not in capabilities
                )

    async def test_executor_error_payload_and_cancellation_are_not_rewritten(self):
        with patch.object(
            self.executor,
            "_sandbox_exec",
            new=AsyncMock(side_effect=ValueError("invalid")),
        ):
            result = json.loads(
                await self.registry.execute(self.turn, "sandbox_exec", {})
            )
            self.assertFalse(result["ok"])
            self.assertIn("error", result)
        with patch.object(
            self.executor,
            "_sandbox_exec",
            new=AsyncMock(side_effect=asyncio.CancelledError()),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self.registry.execute(self.turn, "sandbox_exec", {})

    def test_projection_cannot_rename_registered_tools(self):
        def rename(definition, context):
            definition["function"]["name"] = "different-tool"

        handler = FunctionTool(
            SANDBOX_TOOLS[0], lambda context: True, AsyncMock(), rename
        )
        with self.assertRaisesRegex(ValueError, "registered name"):
            ToolRegistry([handler]).definitions(self.turn.context)
