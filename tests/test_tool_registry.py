from __future__ import annotations

import inspect
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import nonebot

nonebot.init()

from src.plugins.ai_chat.ai_tools import (
    MEMORY_ADD_TOOL,
    QUERY_ALERTS_TOOL,
    WEB_SEARCH_TOOL,
)
from src.plugins.ai_chat.long_term_memory import LongTermMemoryError
from src.plugins.ai_chat import tool_policy
from src.plugins.ai_chat.tools import domain_registry
from src.plugins.ai_chat.tools.registry import FunctionTool, ToolRegistry
from src.plugins.ai_chat.tools.state import ToolContext, TurnState


class ToolRegistryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.app = SimpleNamespace(
            long_term_memory=Mock(), alert_store=Mock(), logger=Mock()
        )
        self.commands = Mock()
        self.commands._memory_scopes.return_value = ("group:10", "user:10:7")
        self.commands._memory_scope_keys.return_value = ["group:10", "user:10:7"]
        self.commands._can_edit_group_memory.return_value = False
        self.commands._looks_like_secret.return_value = False
        self.commands._memory_provenance.return_value = {
            "actor_principal_id": 17,
            "source_message_id": 19,
        }
        self.commands._memory_entry_payload.return_value = {"id": 8, "content": "saved"}
        services = SimpleNamespace(context=self.app, commands=self.commands)
        self.context = ToolContext(
            services, SimpleNamespace(user_id=7), frozenset({"alerts", "memory"})
        )
        self.turn = TurnState(self.context)
        override = patch.dict(tool_policy._TOOL_ENABLED_OVERRIDES, {}, clear=True)
        override.start()
        self.addCleanup(override.stop)

    def test_every_registered_definition_has_an_async_handler_and_host_policy(
        self,
    ) -> None:
        names = {
            handler.definition["function"]["name"]
            for handler in domain_registry.handlers
        }
        self.assertEqual(
            names,
            {
                "query_alerts",
                "memory_add",
                "memory_list",
                "memory_remove",
                "context_expand",
                "context_search",
                "group_members",
                "pin_message",
                "unpin_message",
                "use_skill",
                "inspect_source",
                "web_search",
                "read_image_text",
                "view_image",
                "view_video",
                "transcribe_voice",
                "reply_with_voice",
                "find_stickers",
                "send_sticker",
                "send_qq_face",
                "delegate_agent",
                "run_subagents",
                "resume_subagent",
                "reminder_set",
                "reminder_list",
                "reminder_cancel",
            },
        )
        self.assertEqual(len(names), len(domain_registry.handlers))
        for handler in domain_registry.handlers:
            self.assertTrue(inspect.iscoroutinefunction(handler.run))
            self.assertTrue(inspect.iscoroutinefunction(handler.implementation))
            self.assertEqual(
                handler.policy,
                tool_policy.policy_for_tool(handler.definition["function"]["name"]),
            )
        definitions = domain_registry.definitions(self.context)
        definitions[0]["function"]["description"] = "mutated by caller"
        self.assertNotEqual(
            domain_registry.definitions(self.context)[0]["function"]["description"],
            "mutated by caller",
        )
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            ToolRegistry((*domain_registry.handlers, domain_registry.handlers[0]))
        with self.assertRaisesRegex(TypeError, "no handler"):
            ToolRegistry([SimpleNamespace(definition=MEMORY_ADD_TOOL)])

    async def test_visibility_and_execution_use_the_same_gate_without_legacy_fallthrough(
        self,
    ) -> None:
        run = AsyncMock(return_value='{"ok": true}')
        available = Mock(return_value=True)
        registry = ToolRegistry([FunctionTool(QUERY_ALERTS_TOOL, available, run)])
        original = [WEB_SEARCH_TOOL, QUERY_ALERTS_TOOL, MEMORY_ADD_TOOL]
        self.assertEqual(
            registry.project_legacy_catalog(original, self.context), original
        )
        tool_policy.set_tool_enabled("query_alerts", False)
        self.assertEqual(
            registry.project_legacy_catalog(original, self.context),
            [WEB_SEARCH_TOOL, MEMORY_ADD_TOOL],
        )
        blocked = json.loads(await registry.execute(self.turn, "query_alerts", {}))
        self.assertEqual(blocked["code"], "tool_unavailable")
        tool_policy.set_tool_enabled("query_alerts", True)
        available.return_value = False
        self.assertEqual(registry.definitions(self.context), [])
        self.assertFalse(
            json.loads(await registry.execute(self.turn, "query_alerts", {}))["ok"]
        )
        run.assert_not_awaited()
        self.assertIsNone(await registry.execute(self.turn, "unmigrated", {}))
        available.return_value = True
        self.assertTrue(
            json.loads(await registry.execute(self.turn, "query_alerts", {"days": 1}))[
                "ok"
            ]
        )
        run.assert_awaited_once_with(self.turn, {"days": 1})

    async def test_infrastructure_read_requires_host_granted_capability(self) -> None:
        turn = TurnState(
            ToolContext(
                self.context.services, self.context.event, frozenset({"memory"})
            )
        )
        self.assertNotIn(
            "query_alerts",
            [
                item["function"]["name"]
                for item in domain_registry.definitions(turn.context)
            ],
        )
        response = json.loads(
            await domain_registry.execute(turn, "query_alerts", {"approval": "granted"})
        )
        self.assertFalse(response["ok"])
        self.app.alert_store.snapshot.assert_not_called()

    async def test_memory_add_preserves_scope_and_provenance(self) -> None:
        self.app.long_term_memory.add.return_value = (object(), True)
        result = json.loads(
            await domain_registry.execute(
                self.turn,
                "memory_add",
                {"scope": "user", "content": "prefers short answers"},
            )
        )
        self.assertTrue(result["ok"])
        self.app.long_term_memory.add.assert_called_once_with(
            "user:10:7",
            "user",
            "prefers short answers",
            creator_user_id=7,
            creator_principal_id=17,
            source_message_id=19,
            reason="model memory_add tool",
        )
        self.app.long_term_memory.add.reset_mock()
        self.commands._can_edit_group_memory.return_value = True
        await domain_registry.execute(
            self.turn, "memory_add", {"scope": "group", "content": "decision"}
        )
        self.assertEqual(
            self.app.long_term_memory.add.call_args.args[:2], ("group:10", "group")
        )

    async def test_memory_add_rejects_private_group_edits_invalid_scopes_and_secrets(
        self,
    ) -> None:
        for kind in ("group-forbidden", "private", "invalid", "secret"):
            with self.subTest(kind=kind):
                self.commands._memory_scopes.return_value = (
                    None if kind == "private" else "group:10",
                    "user:10:7",
                )
                self.commands._looks_like_secret.return_value = kind == "secret"
                scope = (
                    "invalid"
                    if kind == "invalid"
                    else "user"
                    if kind == "secret"
                    else "group"
                )
                result = json.loads(
                    await domain_registry.execute(
                        self.turn, "memory_add", {"scope": scope, "content": "content"}
                    )
                )
                self.assertFalse(result["ok"])
        self.app.long_term_memory.add.assert_not_called()

    async def test_memory_store_error_and_delete_scope_are_preserved(self) -> None:
        self.app.long_term_memory.add.side_effect = LongTermMemoryError(
            "memory unavailable"
        )
        result = json.loads(
            await domain_registry.execute(
                self.turn, "memory_add", {"content": "preference"}
            )
        )
        self.assertEqual(result["error"], "memory unavailable")
        self.commands._find_visible_memory.return_value = SimpleNamespace(
            scope_type="group"
        )
        result = json.loads(
            await domain_registry.execute(self.turn, "memory_remove", {"memory_id": 8})
        )
        self.assertFalse(result["ok"])
        self.app.long_term_memory.remove.assert_not_called()
        self.commands._find_visible_memory.return_value = None
        self.app.long_term_memory.remove.return_value = False
        result = json.loads(
            await domain_registry.execute(
                self.turn, "memory_remove", {"memory_id": 999}
            )
        )
        self.assertFalse(result["ok"])
        self.app.long_term_memory.remove.assert_called_once_with(
            999,
            ["group:10", "user:10:7"],
            actor_principal_id=17,
            source_message_id=19,
            reason="model memory_remove tool",
        )

    async def test_memory_list_never_uses_model_supplied_scope_keys(self) -> None:
        self.app.long_term_memory.list_entries.return_value = [object()]
        result = json.loads(
            await domain_registry.execute(
                self.turn, "memory_list", {"scope": "other-group:88"}
            )
        )
        self.assertTrue(result["ok"])
        self.commands._memory_scope_keys.assert_called_once_with(
            self.context.event, "all"
        )
        self.app.long_term_memory.list_entries.assert_called_once_with(
            ["group:10", "user:10:7"]
        )

    async def test_alert_store_error_is_not_reported_as_success(self) -> None:
        self.app.alert_store.snapshot.side_effect = RuntimeError("unavailable")
        result = json.loads(
            await domain_registry.execute(self.turn, "query_alerts", {})
        )
        self.assertFalse(result["ok"])
        self.app.alert_store.rank_incidents.assert_not_called()

    def test_mutable_state_is_not_shared_between_turns(self) -> None:
        other = TurnState(self.context)
        self.turn.used_ocr_texts.append("first turn only")
        self.turn.used_voice_texts.append("transcript")
        self.turn.sticker_handles_this_turn.add("media#1")
        self.turn.search_results.append(object())
        self.turn.subagent_delegations += 1
        self.turn.subagent_task_started = True
        self.assertEqual(
            (other.used_ocr_texts, other.used_voice_texts, other.search_results),
            ([], [], []),
        )
        self.assertEqual(other.sticker_handles_this_turn, set())
        self.assertEqual(other.subagent_delegations, 0)
        self.assertFalse(other.subagent_task_started)
