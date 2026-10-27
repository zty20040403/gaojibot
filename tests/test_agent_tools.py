from __future__ import annotations

from tests.postgres_fixture import postgres_for_test

import base64
import json
import time
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import nonebot

nonebot.init()

from src.plugins.ai_chat.agent_tools import AgentToolExecutor
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import ForwardNode, MessageBody
from src.plugins.ai_chat.storage.jobs import DurableJobStore


class FakeBot:
    def __init__(self) -> None:
        self.sent_messages: list[Any] = []
        self.uploads: list[dict[str, Any]] = []
        self.confirm_uploads = True
        self.online = True
        self.upload_time = None
        self.group_files: list[dict[str, Any]] = [
            {
                "file_id": "file-1",
                "file_name": "input.txt",
                "file_size": 5,
                "upload_time": 123,
                "uploader": 7,
            }
        ]

    async def get_msg(self, *, message_id: int) -> dict[str, Any]:
        if message_id == 99:
            return {
                "message_id": message_id,
                "group_id": 100,
                "user_id": 7,
                "time": 125,
                "sender": {"card": "Alice"},
                "message": [
                    {
                        "type": "file",
                        "data": {
                            "file": "report.pdf",
                            "file_id": "message-file-1",
                            "file_size": 5,
                        },
                    }
                ],
            }
        return {
            "message_id": message_id,
            "group_id": 100,
            "user_id": 7,
            "time": 123,
            "sender": {"card": "Alice"},
            "message": [{"type": "text", "data": {"text": "hello project"}}],
        }

    async def call_api(self, api: str, **data: Any) -> Any:
        if api == "get_status":
            return {"online": self.online, "good": True}
        if api == "get_group_msg_history":
            return {
                "messages": [
                    {
                        "message_id": 1,
                        "user_id": 7,
                        "time": 123,
                        "sender": {"nickname": "Alice"},
                        "message": [
                            {"type": "text", "data": {"text": "hello project"}}
                        ],
                    },
                    {
                        "message_id": 2,
                        "user_id": 8,
                        "time": 124,
                        "sender": {"nickname": "Bob"},
                        "message": [
                            {"type": "text", "data": {"text": "unrelated"}}
                        ],
                    },
                ]
            }
        if api == "get_group_root_files":
            return {"files": list(self.group_files)}
        if api == "get_forward_msg":
            return {
                "messages": [
                    {
                        "user_id": 7,
                        "time": 126,
                        "sender": {"user_id": 7, "nickname": "Alice"},
                        "message": [
                            {"type": "text", "data": {"text": "inside"}},
                            {"type": "forward", "data": {"id": "nested-native"}},
                        ],
                    }
                ]
            }
        if api == "get_file":
            return {"base64": base64.b64encode(b"hello").decode("ascii")}
        if api == "get_group_file_url":
            return {}
        if api == "upload_group_file":
            self.uploads.append(data)
            if self.confirm_uploads:
                raw = base64.b64decode(str(data["file"]).removeprefix("base64://"))
                self.group_files.append(
                    {
                        "file_id": "uploaded",
                        "file_name": data["name"],
                        "file_size": len(raw),
                        "upload_time": int(time.time()) if self.upload_time is None else self.upload_time,
                        "uploader": 42,
                    }
                )
            return {"file_id": "uploaded"}
        raise AssertionError(f"unexpected API: {api}")

    async def send_group_msg(self, **data: Any) -> dict[str, int]:
        self.sent_messages.append(data)
        return {"message_id": len(self.sent_messages)}


class FakeSandboxManager:
    def __init__(self) -> None:
        self.files: dict[tuple[str, str], bytes] = {
            ("s123abc", "result.txt"): b"done"
        }
        self.created: list[str] = []
        self.destroyed: list[str] = []
        self.stopped: list[str] = []
        self.executed: list[str] = []

    async def create(self, owner: str, runtime: str) -> dict[str, str]:
        del owner
        sandbox_id = f"s{len(self.created) + 1:06x}"
        self.created.append(sandbox_id)
        return {
            "sandbox_id": sandbox_id,
            "runtime": runtime,
            "status": "running",
        }

    async def destroy(self, owner: str, sandbox_id: str) -> None:
        del owner
        self.destroyed.append(sandbox_id)

    async def stop_owned(self, owner: str, sandbox_id: str) -> None:
        del owner
        self.stopped.append(sandbox_id)

    async def start_owned(self, owner: str, sandbox_id: str) -> None:
        del owner, sandbox_id

    async def list(self, owner: str) -> list[dict[str, str]]:
        del owner
        return [
            {
                "sandbox_id": sandbox_id,
                "status": "running",
                "purpose": "task",
            }
            for sandbox_id in {"s123abc", *self.created}
        ]

    async def write_file(
        self,
        owner: str,
        sandbox_id: str,
        path: str,
        content: bytes,
        *,
        allow_large: bool = False,
    ) -> int:
        del owner, allow_large
        self.files[(sandbox_id, path)] = content
        return len(content)

    async def read_file(
        self,
        owner: str,
        sandbox_id: str,
        path: str,
        max_bytes: int,
    ) -> bytes:
        del owner
        content = self.files[(sandbox_id, path)]
        if len(content) > max_bytes:
            raise ValueError("too large")
        return content

    async def exec(
        self,
        owner: str,
        sandbox_id: str,
        command: str,
        timeout_seconds: int | None = None,
        packages: list[str] | None = None,
    ) -> SimpleNamespace:
        del owner, sandbox_id, timeout_seconds, packages
        self.executed.append(command)
        if command.startswith("pdffonts "):
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "name type encoding emb sub uni object ID\n"
                    "-----------------------------------------\n"
                    "ABCDEE+Sarasa TrueType Identity-H yes yes yes 7 0\n"
                ),
                stderr="",
            )
        if command.startswith("pdftotext "):
            return SimpleNamespace(returncode=0, stdout="中文报告\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    async def search_nix_packages(self, query: str) -> list[dict[str, str]]:
        return [
            {
                "attribute": query,
                "package": f"{query}-1.0",
                "description": "test package",
            }
        ]


class FakeContentSource:
    def as_tool_payload(self, *, cached: bool) -> dict[str, object]:
        return {
            "handle": "source#12",
            "platform": "bilibili",
            "title": "Test video",
            "cached": cached,
        }


class FakeContentSourceStore:
    def __init__(self) -> None:
        self.targets: list[str] = []

    async def inspect(self, scope, target: str, **kwargs):
        del scope, kwargs
        self.targets.append(target)
        return FakeContentSource(), False

    def get_cached(self, scope, source_handle: str):
        del scope
        self.targets.append(source_handle)
        return FakeContentSource()


class AgentToolExecutorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = FakeBot()
        self.sandbox = FakeSandboxManager()
        self.ledger = MessageLedger(postgres_for_test(self))
        self.addCleanup(self.ledger.close)
        self.scope = ConversationScope(
            platform="onebot-v11",
            kind="group",
            native_conversation_id="100",
            actor_native_user_id="7",
            bot_native_user_id="42",
        )
        self.executor = AgentToolExecutor(
            bot=self.bot,  # type: ignore[arg-type]
            event=SimpleNamespace(group_id=100),  # type: ignore[arg-type]
            owner="group:100:user:7",
            sandbox_manager=self.sandbox,  # type: ignore[arg-type]
            max_file_bytes=1024,
            ledger=self.ledger,
            scope=self.scope,
        )

    async def test_offline_file_is_not_uploaded(self) -> None:
        self.bot.online = False
        result = json.loads(await self.executor.send_file_content(b"test", "test.txt"))
        self.assertFalse(result["ok"])
        self.assertTrue(result["not_sent"])
        self.assertTrue(result["retryable"])
        self.assertEqual(result["state"], "not_sent")
        self.assertFalse(self.bot.uploads)

    async def test_zero_upload_timestamp_confirms_file_without_waiting(self) -> None:
        self.bot.upload_time = 0
        with patch("src.plugins.ai_chat.agent_tools.asyncio.sleep", new_callable=AsyncMock) as sleep:
            result = json.loads(await self.executor.send_file_content(b"test", "test.txt"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "acknowledged")
        self.assertEqual(result["receipt"]["attempts"], 1)
        self.assertEqual(len(self.bot.uploads), 1)
        sleep.assert_not_awaited()

    async def test_zero_timestamp_does_not_bypass_receipt_identity(self) -> None:
        base = {"file_id": "receipt", "file_name": "test.txt", "file_size": 4,
                "uploader": 42, "upload_time": 0}
        for change in ({"file_name": "other.txt"}, {"file_size": 5}, {"uploader": 7},
                       {"upload_time": 90}):
            with self.subTest(change=change):
                self.bot.group_files = [{**base, **change}]
                result = await self.executor.confirm_group_file("test.txt", 4, attempts=1, not_before=100)
                self.assertFalse(result["ok"])

    async def test_file_status_failure_does_not_attempt_upload(self) -> None:
        for response in (None, {"online": "true"}, {"online": True, "good": False}):
            with self.subTest(response=response), patch.object(self.bot, "call_api", return_value=response) as api:
                self.assertIsNotNone(await self.executor.file_delivery_blocker())
                api.assert_awaited_once_with("get_status")
        with patch.object(self.bot, "call_api", side_effect=TimeoutError()) as api:
            result = json.loads(await self.executor.send_file_content(b"test", "test.txt"))
            self.assertTrue(result["not_sent"])
            api.assert_awaited_once_with("get_status")

    async def test_search_messages_filters_current_history(self) -> None:
        result = json.loads(
            await self.executor.execute(
                "search_messages",
                {"query": "PROJECT", "limit": 10},
            )
            or "{}"
        )
        self.assertTrue(result["ok"])
        self.assertEqual(
            [item["handle"] for item in result["messages"]],
            ["msg#1"],
        )

    async def test_unified_shared_content_tool_uses_scoped_source_store(self) -> None:
        source_store = FakeContentSourceStore()
        self.executor.source_store = source_store  # type: ignore[assignment]
        result = json.loads(
            await self.executor.execute(
                "inspect_shared_content",
                {"target": "source#12"},
            )
            or "{}"
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"]["handle"], "source#12")
        self.assertEqual(source_store.targets, ["source#12"])

    async def test_import_requires_file_from_current_group(self) -> None:
        listed = json.loads(
            await self.executor.execute(
                "list_recent_files",
                {"limit": 10},
            )
            or "{}"
        )
        file_handle = listed["files"][0]["handle"]

        rejected = json.loads(
            await self.executor.execute(
                "import_file_to_sandbox",
                {
                    "sandbox_id": "s123abc",
                    "file_handle": "groupfile#not-in-this-scope",
                    "destination": "input.txt",
                },
            )
            or "{}"
        )
        self.assertFalse(rejected["ok"])

        imported = json.loads(
            await self.executor.execute(
                "import_file_to_sandbox",
                {
                    "sandbox_id": "s123abc",
                    "file_handle": file_handle,
                    "destination": "input.txt",
                },
            )
            or "{}"
        )
        self.assertTrue(imported["ok"])
        self.assertEqual(self.sandbox.files[("s123abc", "input.txt")], b"hello")

    async def test_imports_attachment_directly_from_replied_message(self) -> None:
        canonical_message_id = await self.executor.ensure_canonical_message(99)
        self.assertIsNotNone(canonical_message_id)
        message = json.loads(
            await self.executor.execute(
                "get_message_by_id",
                {"message_handle": f"msg#{canonical_message_id}"},
            )
            or "{}"
        )
        attachment = message["message"]["attachments"][0]
        self.assertEqual(
            attachment["handle"],
            f"file#{canonical_message_id}.0",
        )
        self.assertNotIn("file_id", attachment)

        imported = json.loads(
            await self.executor.execute(
                "import_file_to_sandbox",
                {
                    "sandbox_id": "s123abc",
                    "message_handle": f"msg#{canonical_message_id}",
                    "destination": "report.pdf",
                },
            )
            or "{}"
        )
        self.assertTrue(imported["ok"])
        self.assertEqual(
            self.sandbox.files[("s123abc", "report.pdf")],
            b"hello",
        )

    async def test_napcat_missing_container_path_falls_back_to_url(self) -> None:
        class FakeDownload:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def raise_for_status(self) -> None:
                return None

            async def aiter_bytes(self):
                yield b"downloaded"

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def stream(self, method: str, url: str) -> FakeDownload:
                self.request = (method, url)
                return FakeDownload()

        with patch(
            "src.plugins.ai_chat.agent_tools.httpx.AsyncClient",
            return_value=FakeClient(),
        ):
            content = await self.executor._read_napcat_file(
                {
                    "file": "/app/.config/QQ/NapCat/temp/report.pdf",
                    "url": "https://example.invalid/report.pdf",
                }
            )

        self.assertEqual(content, b"downloaded")

    async def test_napcat_group_file_url_is_used_before_container_path(self) -> None:
        async def call_api(api: str, **data: Any) -> Any:
            self.assertEqual(api, "get_group_file_url")
            self.assertEqual(data["group_id"], 100)
            self.assertEqual(data["file_id"], "message-file-1")
            self.assertEqual(data["busid"], 102)
            return {"url": "https://example.invalid/report.pdf"}

        with patch.object(self.bot, "call_api", side_effect=call_api):
            response = await self.executor._resolve_napcat_file(
                {"file_name": "report.pdf"},
                "message-file-1",
            )

        self.assertEqual(
            response,
            {"url": "https://example.invalid/report.pdf"},
        )

    async def test_say_allows_repeated_progress_messages(self) -> None:
        seeded = await self.executor.ensure_canonical_message(500)
        self.assertEqual(seeded, 1)
        results = []
        for index in range(10):
            result = await self.executor.execute(
                "say",
                {"text": f"step {index}"},
            )
            results.append(json.loads(result or "{}"))

        self.assertTrue(all(item["ok"] for item in results))
        self.assertEqual(len(self.bot.sent_messages), 10)
        self.assertEqual(results[0]["message_handle"], "msg#2")

    async def test_background_sandbox_exec_is_persistently_handed_off(self) -> None:
        store = DurableJobStore(postgres_for_test(self))
        self.addCleanup(store.close)
        self.executor.job_store = store
        self.executor._task_sandbox_ids.add("s123abc")

        result = json.loads(
            await self.executor.handoff_tool(
                "sandbox_exec",
                {
                    "sandbox_id": "s123abc",
                    "command": "python -m unittest",
                    "packages": ["python3Packages.pytest"],
                    "timeout_seconds": 120,
                    "background": True,
                },
                "fingerprint123",
            )
            or "{}"
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["job_handle"], "job#1")
        self.assertNotIn("s123abc", self.executor._task_sandbox_ids)
        stored = store.get(1)
        self.assertEqual(stored.kind, "agent.sandbox_exec")  # type: ignore[union-attr]
        self.assertEqual(stored.max_attempts, 3)  # type: ignore[union-attr]
        self.assertEqual(  # type: ignore[union-attr]
            stored.payload["packages"], ["python3Packages.pytest"]
        )
        status = json.loads(
            await self.executor.execute(
                "job_status",
                {"job_handle": "job#1"},
            )
            or "{}"
        )
        self.assertEqual(status["status"], "pending")

    async def test_nix_search_returns_structured_package_matches(self) -> None:
        result = json.loads(
            await self.executor.execute("nix_search", {"query": "ffmpeg"})
            or "{}"
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["packages"][0]["attribute"], "ffmpeg")

    async def test_message_tools_fail_closed_without_canonical_ledger(self) -> None:
        degraded = AgentToolExecutor(
            bot=self.bot,  # type: ignore[arg-type]
            event=SimpleNamespace(group_id=100),  # type: ignore[arg-type]
            owner="group:100:user:7",
            sandbox_manager=self.sandbox,  # type: ignore[arg-type]
            max_file_bytes=1024,
        )

        result = json.loads(
            await degraded.execute(
                "get_message_by_id",
                {"message_handle": "msg#1"},
            )
            or "{}"
        )

        self.assertFalse(result["ok"])
        self.assertIn("拒绝", result["error"])

    async def test_recent_file_uploader_does_not_expose_qq_id(self) -> None:
        await self.executor.ensure_canonical_message(1)
        result = json.loads(
            await self.executor.execute(
                "list_recent_files",
                {"limit": 10},
            )
            or "{}"
        )

        self.assertTrue(result["ok"])
        recent_file = result["files"][0]
        self.assertIn("[mention#", recent_file["uploader"])
        self.assertNotIn("7", recent_file["uploader"].split(" ")[0])
        self.assertTrue(recent_file["handle"].startswith("groupfile#"))
        self.assertNotIn("file_id", recent_file)
        self.assertNotIn("_native_file_id", recent_file)

    async def test_send_file_uploads_base64_to_current_group(self) -> None:
        result = json.loads(
            await self.executor.execute(
                "send_file_from_sandbox",
                {
                    "sandbox_id": "s123abc",
                    "path": "result.txt",
                    "filename": "../result.txt",
                },
            )
            or "{}"
        )
        self.assertTrue(result["ok"])
        self.assertEqual(self.bot.uploads[0]["group_id"], 100)
        self.assertEqual(self.bot.uploads[0]["name"], "result.txt")
        self.assertEqual(
            self.bot.uploads[0]["file"],
            "base64://" + base64.b64encode(b"done").decode("ascii"),
        )

    async def test_send_pdf_checks_embedded_font_before_upload(self) -> None:
        self.sandbox.files[("s123abc", "report.pdf")] = b"%PDF-fake"

        result = json.loads(
            await self.executor.execute(
                "send_file_from_sandbox",
                {"sandbox_id": "s123abc", "path": "report.pdf"},
            )
            or "{}"
        )

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["pdf_validation"],
            {
                "checked": True,
                "embedded_font": True,
                "extractable_text": True,
            },
        )
        self.assertEqual(
            self.sandbox.executed,
            ["pdffonts report.pdf", "pdftotext report.pdf -"],
        )

    async def test_send_pdf_rejects_text_without_embedded_font(self) -> None:
        self.sandbox.files[("s123abc", "broken.pdf")] = b"%PDF-fake"

        async def exec_without_embedded_font(
            owner: str,
            sandbox_id: str,
            command: str,
            timeout_seconds: int | None = None,
        ) -> SimpleNamespace:
            del owner, sandbox_id, timeout_seconds
            if command.startswith("pdffonts "):
                return SimpleNamespace(
                    returncode=0,
                    stdout=(
                        "name type encoding emb sub uni object ID\n"
                        "-----------------------------------------\n"
                        "Helvetica Type 1 WinAnsi no no no 2 0\n"
                    ),
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout="中文报告\n", stderr="")

        with patch.object(self.sandbox, "exec", exec_without_embedded_font):
            result = json.loads(
                await self.executor.execute(
                    "send_file_from_sandbox",
                    {"sandbox_id": "s123abc", "path": "broken.pdf"},
                )
                or "{}"
            )

        self.assertFalse(result["ok"])
        self.assertIn("字体没有嵌入", result["error"])
        self.assertEqual(self.bot.uploads, [])

    async def test_unconfirmed_upload_keeps_artifact_pending(self) -> None:
        self.bot.confirm_uploads = False
        self.executor._task_sandbox_ids.add("s123abc")
        self.executor._pending_artifacts["s123abc"] = {"result.txt"}

        with patch("src.plugins.ai_chat.agent_tools.asyncio.sleep", return_value=None):
            result = json.loads(
                await self.executor.execute(
                    "send_file_from_sandbox",
                    {
                        "sandbox_id": "s123abc",
                        "path": "result.txt",
                        "filename": "result.txt",
                    },
                )
                or "{}"
            )
        lifecycle = await self.executor.retain_task_sandboxes()

        self.assertFalse(result["ok"])
        self.assertTrue(result["uploaded"])
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(lifecycle["retained"], ("s123abc",))
        self.assertEqual(lifecycle["stopped"], ("s123abc",))
        self.assertEqual(self.sandbox.destroyed, [])

    async def test_task_sandboxes_are_stopped_and_retained_once(self) -> None:
        first = json.loads(
            await self.executor.execute("sandbox_create", {"runtime": "python"})
            or "{}"
        )
        second = json.loads(
            await self.executor.execute("sandbox_create", {"runtime": "node"})
            or "{}"
        )
        first_id = first["sandbox"]["sandbox_id"]
        second_id = second["sandbox"]["sandbox_id"]

        lifecycle = await self.executor.retain_task_sandboxes()
        repeated = await self.executor.retain_task_sandboxes()

        self.assertEqual(lifecycle["stopped"], (first_id, second_id))
        self.assertEqual(lifecycle["failed"], ())
        self.assertEqual(lifecycle["retained"], (first_id, second_id))
        self.assertEqual(repeated["stopped"], ())
        self.assertEqual(self.sandbox.destroyed, [])
        self.assertEqual(self.sandbox.stopped, [first_id, second_id])

    async def test_unsent_artifact_retains_sandbox_until_followup_delivery(self) -> None:
        self.executor._task_sandbox_ids.add("s123abc")
        self.executor._pending_artifacts["s123abc"] = {"report.pdf"}

        retained = await self.executor.retain_task_sandboxes()

        self.assertEqual(retained["retained"], ("s123abc",))
        self.assertEqual(self.sandbox.destroyed, [])

        self.sandbox.files[("s123abc", "/workspace/report.pdf")] = b"%PDF-fake"
        result = json.loads(
            await self.executor.execute(
                "send_file_from_sandbox",
                {
                    "sandbox_id": "s123abc",
                    "path": "/workspace/report.pdf",
                    "filename": "中文报告.pdf",
                },
            )
            or "{}"
        )
        lifecycle = await self.executor.retain_task_sandboxes()

        self.assertTrue(result["ok"])
        self.assertEqual(lifecycle["stopped"], ("s123abc",))
        self.assertEqual(self.sandbox.destroyed, [])

    async def test_requested_destroy_does_not_wait_for_unconfirmed_artifacts(self):
        self.executor._task_sandbox_ids.add("s123abc")
        self.executor._pending_artifacts["s123abc"] = {"unsent.pdf"}
        result = json.loads(await self.executor.execute("sandbox_destroy", {"sandbox_id": "s123abc"}))
        self.assertTrue(result["ok"])
        self.assertEqual(self.sandbox.destroyed, ["s123abc"])
        self.assertNotIn("s123abc", self.executor._pending_artifacts)
        self.assertEqual((await self.executor.retain_task_sandboxes())["stopped"], ())

    async def test_destroy_uses_existing_scope_and_propagates_ownership_refusal(self):
        from src.plugins.ai_chat.sandbox import SandboxError
        self.sandbox.destroy = AsyncMock(side_effect=SandboxError("你无权访问这个沙盒。"))
        result = json.loads(await self.executor.execute("sandbox_destroy", {"sandbox_id": "s123abc"}))
        self.assertFalse(result["ok"])
        self.sandbox.destroy.assert_awaited_once_with(self.executor.owner, "s123abc")

    async def test_forward_expansion_never_exposes_native_user_or_forward_ids(self) -> None:
        await self.executor.ensure_canonical_message(1)
        stored = self.ledger.record_message(
            self.scope,
            native_message_id="forward-parent",
            sender_native_user_id="7",
            sender_display="Alice",
            body=MessageBody((ForwardNode(0, "forward-native"),)),
            occurred_at=126,
        )
        result = json.loads(
            await self.executor.execute(
                "view_forward",
                {"message_handle": f"msg#{stored.canonical_message_id}"},
            )
            or "{}"
        )

        self.assertTrue(result["ok"])
        child = result["children"][0]
        self.assertEqual(child["sender_handle"], "[mention#1]")
        self.assertTrue(child["has_nested_forward"])
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("forward-native", encoded)
        self.assertNotIn("nested-native", encoded)
        self.assertNotIn('"user_id"', encoded)


if __name__ == "__main__":
    unittest.main()
