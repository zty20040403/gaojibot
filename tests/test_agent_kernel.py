from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch



from src.plugins.ai_chat.agent_loop import ask_deepseek_with_tools
from src.plugins.ai_chat.tool_policy import (
    TOOL_POLICIES,
    ToolPolicy,
    approval_from_user_text,
)


def _tool(name: str, properties=None, required=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": required or [],
                "additionalProperties": False,
            },
        },
    }


def _tool_response(name: str, arguments: dict[str, object], call_id: str):
    call = SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments, ensure_ascii=False),
        ),
    )
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="", tool_calls=[call])
            )
        ]
    )


def _final(text: str = "完成。"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=text, tool_calls=[])
            )
        ]
    )


class AgentKernelTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_approval_policy_rejects_unapproved_tool(self) -> None:
        execute = AsyncMock(return_value='{"ok":true}')
        events = []

        async def sink(event):
            events.append(event)

        with patch.dict(TOOL_POLICIES, {"browser_clear": ToolPolicy(risk="critical", approval="explicit")}), patch(
            "src.plugins.ai_chat.agent_loop._create_completion",
            new=AsyncMock(
                side_effect=[
                    _tool_response("browser_clear", {}, "call-clear"),
                    _final("需要你明确确认。"),
                ]
            ),
        ):
            answer = await ask_deepseek_with_tools(
                "帮我看看网页",
                [],
                [_tool("browser_clear")],
                execute,
                approval_checker=lambda _policy, name, arguments: (
                    approval_from_user_text("帮我看看网页", name, arguments)
                ),
                event_sink=sink,
            )

        self.assertEqual(answer, "需要你明确确认。")
        execute.assert_not_awaited()
        self.assertEqual(events[0].state, "rejected")
        self.assertEqual(events[0].risk, "critical")

    async def test_duplicate_non_idempotent_call_is_stopped(self) -> None:
        execute = AsyncMock(return_value='{"ok":true}')
        events = []

        async def sink(event):
            events.append(event)

        with patch(
            "src.plugins.ai_chat.agent_loop._create_completion",
            new=AsyncMock(
                side_effect=[
                    _tool_response("say", {"text": "处理中"}, "call-1"),
                    _tool_response("say", {"text": "处理中"}, "call-2"),
                    _final(),
                ]
            ),
        ):
            await ask_deepseek_with_tools(
                "处理任务",
                [],
                [_tool("say", {"text": {"type": "string"}}, ["text"])],
                execute,
                event_sink=sink,
                max_tool_rounds=4,
            )

        execute.assert_awaited_once()
        rejected = [event for event in events if event.kind == "tool_rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertIn("重复", rejected[0].result)
        self.assertTrue(rejected[0].fingerprint)

    async def test_timeout_runs_compensation_and_records_duration(self) -> None:
        async def blocked(_name, _arguments):
            await asyncio.sleep(1)
            return '{"ok":true}'

        compensator = AsyncMock(return_value='{"ok":true,"action":"cancel"}')
        events = []

        async def sink(event):
            events.append(event)

        short_policy = ToolPolicy(
            risk="high",
            idempotency="non-idempotent",
            side_effects=("execute:code",),
            timeout_seconds=0.01,
            compensation="cancel-process",
            max_identical_calls=1,
        )
        with patch.dict(TOOL_POLICIES, {"sandbox_exec": short_policy}), patch(
            "src.plugins.ai_chat.agent_loop._create_completion",
            new=AsyncMock(
                side_effect=[
                    _tool_response(
                        "sandbox_exec",
                        {"sandbox_id": "s123abc", "command": "sleep 1"},
                        "call-exec",
                    ),
                    _final("命令超时。"),
                ]
            ),
        ):
            answer = await ask_deepseek_with_tools(
                "运行命令",
                [],
                [
                    _tool(
                        "sandbox_exec",
                        {
                            "sandbox_id": {"type": "string"},
                            "command": {"type": "string"},
                        },
                        ["sandbox_id", "command"],
                    )
                ],
                blocked,
                compensate_tool=compensator,
                event_sink=sink,
            )

        self.assertEqual(answer, "命令超时。")
        self.assertIn("timed-out", [event.state for event in events])
        self.assertIn("compensated", [event.state for event in events])
        compensator.assert_awaited_once()

    async def test_background_tool_is_handed_to_durable_executor(self) -> None:
        execute = AsyncMock(return_value='{"ok":true}')
        handoff = AsyncMock(
            return_value='{"ok":true,"job_handle":"job#7"}'
        )
        events = []

        async def sink(event):
            events.append(event)

        with patch(
            "src.plugins.ai_chat.agent_loop._create_completion",
            new=AsyncMock(
                side_effect=[
                    _tool_response(
                        "sandbox_exec",
                        {
                            "sandbox_id": "s123abc",
                            "command": "build",
                            "background": True,
                        },
                        "call-bg",
                    ),
                    _final(),
                ]
            ),
        ):
            await ask_deepseek_with_tools(
                "后台构建",
                [],
                [
                    _tool(
                        "sandbox_exec",
                        {
                            "sandbox_id": {"type": "string"},
                            "command": {"type": "string"},
                            "background": {"type": "boolean"},
                        },
                        ["sandbox_id", "command"],
                    )
                ],
                execute,
                handoff_tool=handoff,
                event_sink=sink,
            )

        execute.assert_not_awaited()
        handoff.assert_awaited_once()
        self.assertIn("handed-off", [event.state for event in events])


if __name__ == "__main__":
    unittest.main()
