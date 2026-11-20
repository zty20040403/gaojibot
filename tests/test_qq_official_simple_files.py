"""File upload and direct msg_type=7 delivery tests; all HTTP is mocked or loopback."""
import asyncio
import base64
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official.file_services import FileServiceConfig, FileServices
from src.plugins.ai_chat.adapters.qq_official.file_upload import (
    MAX_FILE_BYTES,
    SMALL_FILE_BYTES,
    FileUploadError,
    FileUploader,
)
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.send_store import OfficialSendStore
from src.plugins.ai_chat.adapters.qq_official.sender import OfficialSender
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey
from src.qq_official.credentials import Credentials
from src.qq_official.errors import QQAPIError
from src.qq_official.http import RestClient
from src.qq_official.token import TokenManager
from tests.qq_official_turn_fixture import ExecutionFixture
from tests.qq_official_file_http_fixture import FileMediaFake

ENDPOINT = EndpointKey(AccountKey("qq-official", "TEST_FILES", "test"), "group", "TEST_GROUP")


class UploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_total_upload_deadline_reports_unknown_without_retry_or_cache(self):
        cancelled = asyncio.Event()
        rest = SimpleNamespace(request=AsyncMock())

        async def blocked(*_args):
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        rest.request.side_effect = blocked
        uploader = FileUploader(rest, client=SimpleNamespace())
        timeout_context = asyncio.timeout
        with patch(
            "src.plugins.ai_chat.adapters.qq_official.file_upload.asyncio.timeout",
            side_effect=lambda _: timeout_context(0.01),
        ) as timeout:
            with self.assertRaisesRegex(FileUploadError, "远端结果未知"):
                await uploader.upload(ENDPOINT, b"TEST_DATA", "test.txt")

        timeout.assert_called_once_with(300)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(uploader.cache)
        rest.request.assert_awaited_once()

    async def test_upstream_timeout_is_not_mislabeled_as_total_deadline(self):
        rest = SimpleNamespace(request=AsyncMock(side_effect=TimeoutError("upstream")))
        uploader = FileUploader(rest, client=SimpleNamespace())

        with self.assertRaisesRegex(TimeoutError, "upstream"):
            await uploader.upload(ENDPOINT, b"TEST_DATA", "test.txt")

        self.assertFalse(uploader.cache)
        rest.request.assert_awaited_once()

    async def test_unconfirmed_transport_or_response_reports_unknown(self):
        request = httpx.Request("POST", "https://example.test/files")
        for response in (httpx.ReadError("TEST_READ", request=request), QQAPIError("transport_ambiguous"),
                         QQAPIError("unexpected_upload_message_evidence", status=401, code=50015014), {}):
            with self.subTest(response=type(response).__name__):
                mocked = AsyncMock()
                if isinstance(response, BaseException):
                    mocked.side_effect = response
                else:
                    mocked.return_value = response
                rest = SimpleNamespace(request=mocked)
                uploader = FileUploader(rest, client=SimpleNamespace())

                with self.assertRaisesRegex(FileUploadError, "远端.*结果未知"):
                    await uploader.upload(ENDPOINT, b"TEST_DATA", "test.txt")

                self.assertFalse(uploader.cache)
                rest.request.assert_awaited_once()

    async def test_named_files_and_gif_use_small_upload_and_session_content_name_cache(self):
        rest = SimpleNamespace(request=AsyncMock(return_value={"file_info": "TEST_INFO", "ttl": 86400}))
        uploader = FileUploader(rest, client=SimpleNamespace())
        for name, kind in [("report.docx", 4), ("report.pdf", 4), ("picture.gif", 1), ("photo.png", 1)]:
            data = b"GIF89a" if kind == 1 else b"TEST_DOCUMENT"
            await uploader.upload(ENDPOINT, data, name)
            payload = rest.request.await_args.args[2]
            self.assertEqual(payload["file_type"], kind)
            self.assertEqual(base64.b64decode(payload["file_data"]), data)
            self.assertEqual(payload["file_name"], name)
            await uploader.upload(ENDPOINT, data, name)
        self.assertEqual(rest.request.await_count, 4)
        other = EndpointKey(ENDPOINT.account, "private", "TEST_PRIVATE")
        await uploader.upload(other, b"TEST_DOCUMENT", "report.docx")
        self.assertIn("/users/TEST_PRIVATE/files", rest.request.await_args.args[1])

    async def test_large_upload_sends_all_hashes_and_finishes_each_part_before_message_handle(self):
        content = b"a" * (SMALL_FILE_BYTES * 2 + 3)
        block_size = 4 * 1024 * 1024
        calls, blocks = [], []

        async def request(_method, path, payload):
            calls.append((path, payload))
            if path.endswith("/upload_prepare"):
                return {
                    "upload_id": "TEST_UPLOAD",
                    "block_size": str(block_size),
                    "parts": [{"index": index, "presigned_url": f"https://storage.example/part/{index}"} for index in range(3)],
                }
            return {"file_info": "TEST_UPLOADED"} if path.endswith("/files") else {}

        async def put(url, *, content):
            blocks.append(content)
            return httpx.Response(200, request=httpx.Request("PUT", url))

        uploader = FileUploader(SimpleNamespace(request=request), client=SimpleNamespace(put=put))
        result = await uploader.upload(ENDPOINT, content, "large.bin")
        self.assertEqual(result.file_info, "TEST_UPLOADED")
        self.assertEqual(b"".join(blocks), content)
        prepare = calls[0][1]
        self.assertEqual(prepare["md5"], hashlib.md5(content).hexdigest())
        self.assertEqual(prepare["sha1"], hashlib.sha1(content).hexdigest())
        self.assertEqual(prepare["md5_10m"], hashlib.md5(content[:10_002_432]).hexdigest())
        self.assertEqual([payload["part_index"] for path, payload in calls if path.endswith("part_finish")], [0, 1, 2])
        self.assertEqual(calls[-1][1]["upload_id"], "TEST_UPLOAD")

    async def test_wrapped_platform_error_reports_prepare_cause_and_enforces_caps(self):
        rest = SimpleNamespace(request=AsyncMock(side_effect=[
            QQAPIError("native_file_unavailable", accepted=False, status=400, code=50015014),
            QQAPIError("platform_rejected", accepted=False, status=400, code=40093003),
            {"file_info": "TEST_RETRY"},
        ]))
        uploader = FileUploader(rest, client=SimpleNamespace())
        with self.assertRaisesRegex(FileUploadError, "群文件超过上限.*40093003"):
            await uploader.upload(ENDPOINT, b"TEST", "report.txt")
        self.assertEqual((await uploader.upload(ENDPOINT, b"TEST", "report.txt")).file_info, "TEST_RETRY")
        with self.assertRaisesRegex(FileUploadError, "200 MB"):
            await uploader.upload(ENDPOINT, b"x" * (MAX_FILE_BYTES + 1), "huge.bin")

    async def test_one_based_ten_plus_parts_and_large_image_multipart_and_no_daily_cap(self):
        # 12 parts with 1-based string indices ("1".."12") must sort numerically and slice from offset 0
        block_size = 512 * 1024
        content = b"".join(bytes([i + 1]) * block_size for i in range(11)) + b"Z" * 123
        calls, blocks = [], []

        async def request(_method, path, payload):
            calls.append((path, payload))
            if path.endswith("/upload_prepare"):
                return {
                    "upload_id": "TEST_1BASED",
                    "block_size": str(block_size),
                    "parts": [
                        {"index": str(i), "presigned_url": f"https://storage.example/part/{i}"}
                        for i in [10, 2, 1, 12, 11, 3, 4, 5, 6, 7, 8, 9]
                    ],
                }
            return {"file_info": "TEST_1BASED_OK", "ttl": 86400} if path.endswith("/files") else {}

        async def put(url, *, content):
            blocks.append(content)
            return httpx.Response(200, request=httpx.Request("PUT", url))

        uploader = FileUploader(SimpleNamespace(request=request), client=SimpleNamespace(put=put))
        res = await uploader.upload(ENDPOINT, content, "archive.bin")
        self.assertEqual(res.file_info, "TEST_1BASED_OK")
        self.assertEqual(b"".join(blocks), content)
        self.assertEqual(
            [payload["part_index"] for path, payload in calls if path.endswith("part_finish")],
            list(range(1, 13)),
        )

        # >5 MiB image multipart uses file_name='picture' in upload_prepare and /files merge
        calls.clear()
        blocks.clear()
        img_bytes = b"\x89PNG\r\n\x1a\n" + b"p" * (SMALL_FILE_BYTES + 64)
        res_img = await uploader.upload(ENDPOINT, img_bytes, "big_photo.png")
        self.assertEqual(res_img.file_info, "TEST_1BASED_OK")
        self.assertEqual(calls[0][1]["file_type"], 1)
        self.assertEqual(calls[0][1]["file_name"], "picture")
        self.assertEqual(calls[-1][1]["file_name"], "picture")


class FileDeliveryTests(ExecutionFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = DatabaseCalls()
        self.addAsyncCleanup(self.calls.close)
        self.deliveries = DeliveryStore(self.database)
        self.addCleanup(self.deliveries.close)
        self.outbox = OfficialSendStore(self.deliveries, admission=self.admission, clock=lambda: self.now)
        with self.tx() as c:
            observe_ready(c, self.token, {"op": 0, "t": "READY", "d": {
                "session_id": "TEST_SESSION", "user": {"id": "TEST_BOT", "bot": True}}}, self.now)
            c.execute("UPDATE qq_official_endpoints SET proactive_state='unknown'")
        self.handle = self.claim().handle
        self.turn_id = self.begin(self.handle)
        fake = self.fake = FileMediaFake()
        fake.clock = lambda: self.now
        fake.app_id = self.account.app_id
        await fake.start()
        fake.proactive_allowed[("group", self.endpoint.native_conversation_id)] = True
        self.addAsyncCleanup(fake.close)
        tokens = TokenManager(Credentials(self.account.app_id, fake.app_secret), fake.base_url, allow_test_server=True)
        self.addAsyncCleanup(tokens.aclose)
        rest = RestClient(tokens, allow_test_server=True)
        self.addAsyncCleanup(rest.aclose)
        sender = OfficialSender(self.account, self.token, self.outbox, rest, self.ledger,
            journal=self.journal, database_calls=self.calls, ensure_current=AsyncMock(), wall_clock=lambda: self.now)
        self.addAsyncCleanup(sender.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        runtime = SimpleNamespace(_calls=self.calls, deliveries=self.deliveries, jobs=self.jobs,
            outbox=self.outbox, sender=sender, _stopping=False, _token=self.token,
            store=SimpleNamespace(admitted=self.admission, clock=lambda: self.now))
        self.service = FileServices(runtime, self.token, rest,
            FileServiceConfig(Path(temporary.name), None, generation_enabled=True))
        self.addAsyncCleanup(self.service.close)

    async def test_arbitrary_file_is_uploaded_sent_directly_and_linked_to_turn(self):
        outcome = await self.service.send_bytes(
            endpoint=self.endpoint,
            requester=self.actor,
            content=b"TEST named document",
            filename="notes.txt",
            logical_key="TEST_DIRECT_FILE",
            source_scope_key=self.endpoint.scope.key,
            expires_at=self.now + 3600,
            source_native_message_id="TEST_INPUT",
            source_canonical_message_id=self.message.canonical_message_id,
            turn_id=self.turn_id,
        )
        self.assertEqual(outcome.status, "committed")
        self.assertEqual(self.fake.received[-1]["payload"]["msg_type"], 7)
        self.assertEqual(len(self.query("SELECT * FROM turn_send_links")), 1)
        self.assertIn("notes.txt", self.query("SELECT body_json FROM messages WHERE direction='outbound'")[0]["body_json"])
