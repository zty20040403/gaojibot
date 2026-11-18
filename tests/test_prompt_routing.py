"""Verify key routing and behavior rules in the built system prompt."""
from __future__ import annotations

import unittest

from src.plugins.ai_chat.prompt import system_prompt


class PromptRoutingTests(unittest.TestCase):
    def test_system_prompt_contains_required_routing_constraints(self) -> None:
        text = system_prompt()
        for phrase in (
            "假小子：短发、卫衣、说话大大咧咧",
            "要查好几样就写进同一个程序一起查，或者同一轮里一起调用多个工具",
            '有人问"刚才聊到哪了"，就用 history 总结最近一段',
            "很多步的活交给后台，同一次输出里先回一句接了、大概多久",
            "先用 say 说一声",
            "搜索先用用户的原词",
            "不要拿链接去 web_search",
            "只看了标题和简介",
            "send_sticker 只能发表情包库里的图",
            '不要说"听成""听错了"',
        ):
            self.assertIn(phrase, text)
        for removed in ("[常驻程序]", "frontend_processed", "请求预留", "供应商实际账单"):
            self.assertNotIn(removed, text)


if __name__ == "__main__":
    unittest.main()
