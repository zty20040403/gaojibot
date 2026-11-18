"""Unit tests for background report outcome parsing, state override, and use_skill exposure."""
from __future__ import annotations

from dataclasses import replace
import json
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official.background_agents import PROMPT, view
from src.plugins.ai_chat.background_jobs import BackgroundRunner, outcome_of
from src.plugins.ai_chat.background_tools import BackgroundToolSession
from src.plugins.ai_chat.config import settings
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey, InboundMessage
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.prompt import system_prompt


class _FakeCallRunner:
    async def __call__(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)


class _FakeJobStore:
    def __init__(self, row: dict) -> None:
        self.row = dict(row)

    def start(self, job_id: int) -> dict | None:
        self.row["state"] = "running"
        return dict(self.row)

    def get(self, job_id: int) -> dict | None:
        return dict(self.row)

    def finish(self, job_id: int, *, state: str, error: str, report: str) -> str:
        self.row.update(state=state, error=error, report=report, finished_at=200)
        return state

    def update(self, job_id: int, **fields: object) -> None:
        self.row.update(fields)


class JobOutcomeTests(unittest.IsolatedAsyncioTestCase):
    def test_outcome_of_parses_headers_and_markdown_wrappers(self) -> None:
        self.assertEqual(outcome_of("\n\n**结果：成功**\n已完成"), "success")
        self.assertEqual(outcome_of("### 结果:部分完成\n只拿到一半"), "partial")
        self.assertEqual(outcome_of("结果：失败\n/root/missing 不存在"), "failed")
        self.assertEqual(outcome_of("一切顺利"), "unknown")
        self.assertEqual(outcome_of(""), "unknown")

    async def test_failed_outcome_report_marks_job_failed_without_duplicating_error(self) -> None:
        row = {
            "job_id": 7,
            "title": "read missing file",
            "state": "queued",
            "error": "",
            "task": "read /root/missing",
            "report": "",
            "steering_json": "[]",
            "created_at": 100,
            "finished_at": None,
            "deadline_at": 500,
            "last_tool": "",
            "delivered_at": None,
        }
        store = _FakeJobStore(row)
        saved: list[str] = []

        async def save(_row, report: str) -> None:
            saved.append(report)

        async def session(*_args):
            return SimpleNamespace(prompt="TEST", tools=(), close=AsyncMock())

        async def engine(*_args, **_kwargs) -> str:
            return "结果：失败\n/root/不存在的文件 不存在，无法读取"

        runner = BackgroundRunner(
            store,
            calls=_FakeCallRunner(),
            session_factory=session,
            save_report=save,
            ask_main=AsyncMock(),
            record_usage=AsyncMock(),
            logger=logging.getLogger(__name__),
            engine=engine,
            clock=lambda: 150,
        )
        result = await runner.execute(SimpleNamespace(job_id=7))
        self.assertEqual(result["state"], "failed")
        self.assertEqual(store.row["state"], "failed")
        self.assertEqual(store.row["error"], "后台报告任务失败：/root/不存在的文件 不存在，无法读取")
        self.assertEqual(store.row["report"], "结果：失败\n/root/不存在的文件 不存在，无法读取")
        self.assertEqual(saved, [store.row["report"]])
        self.assertEqual(view(store.row, 200)["outcome"], "failed")

    async def test_background_session_exposes_and_runs_use_skill(self) -> None:
        account = AccountKey("qq-official", "TEST_APP", "test")
        endpoint = EndpointKey(account, "group", "TEST_GROUP")
        actor = ActorKey(account, "group_member", endpoint.native_conversation_id, "TEST_USER")
        inbound = InboundMessage(
            endpoint=endpoint,
            actor=actor,
            event_id=None,
            native_message_id="TEST_MSG",
            sender_display="Tester",
            platform_role="member",
            body=MessageBody((TextNode(0, "run"),)),
            mentions_bot=True,
            reply_to_native_message_id=None,
            occurred_at=100,
            reply_credential=None,
        )
        skill = SimpleNamespace(name="programs", title="Programs", body="程序部署说明")
        calls = DatabaseCalls()
        self.addAsyncCleanup(calls.close)
        app = SimpleNamespace(
            db_call=calls,
            settings=replace(settings, search_enabled=True),
            skill_registry=SimpleNamespace(get=lambda name: skill if name == "programs" else None, list=lambda: [skill]),
            message_ledger=None,
            semantic_recall=None,
            conversations=None,
        )
        session = BackgroundToolSession(
            app,
            inbound,
            prompt=PROMPT,
            question=AsyncMock(return_value=""),
            current=AsyncMock(return_value=None),
        )
        tool_names = {item["function"]["name"] for item in session.tools}
        self.assertIn("use_skill", tool_names)
        payload = json.loads(await session.execute_tool("use_skill", {"name": "programs"}))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["instructions"], "程序部署说明")
        schema = next(item["function"]["parameters"] for item in session.tools if item["function"]["name"] == "web_search")
        self.assertEqual(schema["required"], ["queries"])
        with patch("src.plugins.ai_chat.tools.sources.search_web", new_callable=AsyncMock, return_value=[]) as search:
            payload = json.loads(await session.execute_tool("web_search", {"queries": ["TEST_A", "TEST_B"], "max_results": 2}))
            self.assertTrue(payload["ok"])
            self.assertEqual(search.await_args.args, (["TEST_A", "TEST_B"],))
            self.assertEqual(search.await_args.kwargs["max_results"], min(2, settings.search_max_results))
            self.assertFalse(json.loads(await session.execute_tool("web_search", {"query": "TEST"}))["ok"])
            search.assert_awaited_once()
        self.assertIn("结果：失败", PROMPT)
        self.assertIn("use_skill", PROMPT)
        self.assertNotIn("outcome 是 partial 或 unknown 时", system_prompt())


if __name__ == "__main__":
    unittest.main()
