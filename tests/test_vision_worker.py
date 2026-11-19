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

    def test_only_qq_hosts_are_accepted(self) -> None:
        self.assertTrue(
            VisionWorker._supported_source("https://multimedia.nt.qq.com.cn/x.png")
        )
        self.assertFalse(
            VisionWorker._supported_source("http://127.0.0.1/private.png")
        )

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

    async def test_source_resolver_returns_fresh_qq_url(self) -> None:
        worker = VisionWorker.__new__(VisionWorker)
        worker._source_resolver = AsyncMock(
            return_value="https://multimedia.nt.qq.com.cn/fresh.png"
        )
        job = VisionJob(1, "99", 2, "https://gchat.qpic.cn/old", "detail", "", 1)

        refreshed = await worker._refresh_source(job)

        self.assertEqual(
            refreshed,
            "https://multimedia.nt.qq.com.cn/fresh.png",
        )
        worker._source_resolver.assert_awaited_once_with("99", 2)


if __name__ == "__main__":
    unittest.main()
