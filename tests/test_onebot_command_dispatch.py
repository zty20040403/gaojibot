from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import nonebot
nonebot.init()
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.exception import FinishedException
from src.plugins.ai_chat import matcher_handlers
from src.plugins.ai_chat.adapters.onebot_commands import dispatch_onebot_command


class OneBotCommandDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_matchers_pass_original_event_args_and_bot_through_common_dispatch(self):
        class Matcher:
            def handle(self):
                def capture(handler):
                    self.handler = handler
                    return handler
                return capture
        class Registry:
            def __init__(self):
                self.entries = {}
            def __getattr__(self, name):
                return self.entries.setdefault(name, Matcher())
        names = {"model_command": "handle_model_command", "effort_command": "handle_effort_command",
                 "task_status": "handle_task_status", "usage_command": "handle_usage_command", "task_stop": "handle_task_stop",
                 "control_command": "handle_control_command"}
        commands = SimpleNamespace(**{method: AsyncMock() for method in names.values()})
        registry = Registry()
        with patch.object(matcher_handlers, "matchers", registry):
            matcher_handlers.register_matcher_handlers(SimpleNamespace(handlers=SimpleNamespace(commands=commands)))
        event = SimpleNamespace(message=Message("/help"))
        bot = object()
        args = Message([MessageSegment.text(" original args "), MessageSegment.image("fixture.png")])
        with patch("src.plugins.ai_chat.adapters.onebot_commands.dispatch_command", wraps=__import__(
            "src.plugins.ai_chat.application.commands", fromlist=["dispatch_command"]).dispatch_command) as common:
            for matcher, method in names.items():
                positional = (bot, event, args) if matcher == "task_status" else (bot, event) if matcher == "control_command" else (event,) if matcher == "usage_command" else (event, args)
                await getattr(registry, matcher).handler(*positional)
                getattr(commands, method).assert_awaited_once_with(*positional)
            self.assertEqual(common.call_count, 6)

    async def test_native_finish_exception_is_not_swallowed_or_rewritten(self):
        callback = AsyncMock(side_effect=FinishedException)
        with self.assertRaises(FinishedException):
            await dispatch_onebot_command("model", callback)
        callback.assert_awaited_once_with()
