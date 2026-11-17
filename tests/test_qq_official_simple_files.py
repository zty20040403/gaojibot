"""File upload and direct msg_type=7 delivery tests; all HTTP is mocked or loopback."""
import base64
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import httpx

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official.file_services import FileServiceConfig, FileServices
from src.plugins.ai_chat.adapters.qq_official.file_upload import (
    DAILY_BYTES,
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
            QQAPIError("upload", status=400, code=50015014),
            QQAPIError("upload", status=400, code=40093003),
            {"file_info": "TEST_RETRY"},
        ]))
        uploader = FileUploader(rest, client=SimpleNamespace())
        with self.assertRaisesRegex(FileUploadError, "群文件超过上限.*40093003"):
            await uploader.upload(ENDPOINT, b"TEST", "report.txt")
        self.assertEqual((await uploader.upload(ENDPOINT, b"TEST", "report.txt")).file_info, "TEST_RETRY")
        with self.assertRaisesRegex(FileUploadError, "100 MB"):
            await uploader.upload(ENDPOINT, b"x" * (MAX_FILE_BYTES + 1), "huge.bin")
        uploader._day, uploader._bytes = int(uploader.clock()) // 86400, DAILY_BYTES
        with self.assertRaisesRegex(FileUploadError, "2 GB"):
            await uploader.upload(ENDPOINT, b"y", "next.bin")


class FileDeliveryTests(ExecutionFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = DatabaseCalls()
        self.addAsyncCleanup(self.calls.close)
        self.deliveries = DeliveryStore(self.database)
        self.addCleanup(self.deliveries.close)
        self.outbox = OfficialSendStore(self.deliveries, admission=self.admission,
            pepper=b"TEST_FILE_PEPPER" * 3, clock=lambda: self.now)
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
        self.assertEqual(self.query("SELECT * FROM qq_official_file_artifacts"), [])
