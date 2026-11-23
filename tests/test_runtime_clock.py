from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch



from src.plugins.ai_chat.agent_loop import _build_messages
from src.plugins.ai_chat.runtime_clock import runtime_clock_prompt


class RuntimeClockTests(unittest.TestCase):
    def test_clock_is_converted_to_shanghai_time(self) -> None:
        prompt = runtime_clock_prompt(
            datetime(2026, 8, 28, 6, 23, 45, tzinfo=timezone.utc)
        )

        self.assertIn("2026-08-28 14:23", prompt)
        self.assertNotIn("14:23:45", prompt)
        self.assertIn("Asia/Shanghai", prompt)
        self.assertIn("UTC+08:00", prompt)
        self.assertIn("星期五", prompt)

    def test_chat_question_uses_fresh_clock_without_changing_system_prefix(self) -> None:
        with patch(
            "src.plugins.ai_chat.agent_loop.runtime_clock_prompt",
            return_value="本轮动态时间",
        ):
            messages = _build_messages("question", system_prompt="TEST policy")

        self.assertNotIn("本轮动态时间", messages[0]["content"])
        self.assertIn("本轮动态时间", messages[-1]["content"])


if __name__ == "__main__":
    unittest.main()
