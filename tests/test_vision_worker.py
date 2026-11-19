from __future__ import annotations


import asyncio
import json
import unittest
from unittest.mock import AsyncMock



from src.plugins.ai_chat.vision_worker import (
    VisionJob,
    VisionJobError,
    VisionResult,
    VisionWorker,
)


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

class VisionWorkerCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_analysis_uses_one_model_call_then_cache(self) -> None:
        worker = VisionWorker.__new__(VisionWorker)
        worker.cache_seconds = 600
        worker.cache_entries = 32
        worker._analysis_cache = {}
        worker._analysis_inflight = {}
        worker._cache_lock = asyncio.Lock()
        result = VisionResult(
            summary="终端报错截图",
            description="终端显示连接超时。",
            extracted_text="timeout",
            observations=(),
            safety="safe",
            mode="summary",
        )

        async def analyze(*_args):
            await asyncio.sleep(0.01)
            return result

        worker._request_analysis = AsyncMock(side_effect=analyze)
        job = VisionJob(1, "99", 0, "https://gchat.qpic.cn/a", "summary", "", 1)
        first, second = await asyncio.gather(
            worker._analyze_cached("same", job, b"image", "image/png"),
            worker._analyze_cached("same", job, b"image", "image/png"),
        )
        await asyncio.sleep(0)
        third = await worker._analyze_cached("same", job, b"image", "image/png")

        self.assertEqual((first, second, third), (result, result, result))
        self.assertEqual(worker._request_analysis.await_count, 1)

if __name__ == "__main__":
    unittest.main()
