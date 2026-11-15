"""Verify key routing and behavior rules in the built system prompt."""
from __future__ import annotations

import unittest

from src.plugins.ai_chat.prompt import system_prompt


class PromptRoutingTests(unittest.TestCase):
    def test_system_prompt_contains_required_routing_constraints(self) -> None:
        text = system_prompt()
        for phrase in (
            "搜索先用用户的原词",
            "不要拿链接去 web_search",
            "只看了标题和简介",
            "send_sticker 只能发表情包库里的图",
            '不要说"听成""听错了"',
        ):
            self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
