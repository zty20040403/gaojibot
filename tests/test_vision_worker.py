from __future__ import annotations

import json
import unittest

from src.plugins.ai_chat.vision_worker import VisionJobError, VisionWorker


class VisionWorkerParsingTests(unittest.TestCase):
    def test_parses_transient_summary(self) -> None:
        result = VisionWorker.parse_result(
            json.dumps(
                {
                    "summary": "雨夜街边等车的人",
                    "description": "一个人撑伞站在有霓虹灯的街边。",
                    "text": "公交站",
                    "observations": ["路面有积水", "画面偏暗"],
                    "safety": "safe",
                },
                ensure_ascii=False,
            ),
            mode="summary",
        )

        self.assertEqual(result.summary, "雨夜街边等车的人")
        self.assertEqual(result.observations, ("路面有积水", "画面偏暗"))
        self.assertEqual(result.mode, "summary")

    def test_detail_result_is_not_a_media_handle(self) -> None:
        result = VisionWorker.parse_result(
            '{"summary":"代码报错截图",'
            '"description":"终端显示连接超时。",'
            '"text":"Connection timed out",'
            '"observations":[],"safety":"review"}',
            mode="detail",
        )

        self.assertEqual(result.mode, "detail")
        self.assertNotIn("media", result.as_dict())

    def test_invalid_response_is_rejected(self) -> None:
        with self.assertRaises(VisionJobError):
            VisionWorker.parse_result("not-json", mode="summary")


if __name__ == "__main__":
    unittest.main()
