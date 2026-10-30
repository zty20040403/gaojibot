"""Controlled CLI file checks use only loopback, fixed bytes and TEST identities."""
from __future__ import annotations

import asyncio
import base64
from contextlib import redirect_stdout
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import zlib

from aiohttp import web
import httpx

from src.qq_official.credentials import Credentials
from src.qq_official.gateway import GatewayConnection
from src.qq_official.http import RestClient, request_json_response, scoped_request_guard, scoped_request_observer
from src.qq_official.probe import ProbeOptions, async_main, run_probe
from src.qq_official.probe_files import FILE_CHECKS, sample, upload_receipt, UnconfirmedUpload
from src.qq_official.testing.fake_platform import FakePlatform
from src.qq_official.token import TokenManager


class ProbeMediaFake(FakePlatform):
    """Additive fake /files endpoint; existing fake validates reply windows."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.uploads = []
        self.upload_status = 200
        self.upload_body = {"file_info": "SECRET_TEST_FILE_INFO", "file_uuid": "SECRET_TEST_FILE_UUID", "ttl": 60}
        self.drop_upload = False
        self.deep_send_response = False
        self.after_upload = None

    async def start(self):
        app = web.Application()
        app.router.add_post("/app/getAppAccessToken", self._token)
        app.router.add_get("/gateway/bot", self._gateway)
        app.router.add_get("/ws", self._websocket)
        app.router.add_post("/v2/{collection:groups|users}/{target}/messages", self._send)
        app.router.add_post("/v2/{collection:groups|users}/{target}/files", self._files)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.base_url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        self.gateway_url = self.base_url.replace("http:", "ws:") + "/ws"

    async def _files(self, request):
        if not self._authorized(request):
            raise web.HTTPUnauthorized()
        body = await request.json()
        if (set(body) != {"file_type", "file_name", "file_data", "srv_send_msg"}
                or body["srv_send_msg"] is not False or body["file_type"] not in {1, 4}):
            raise web.HTTPBadRequest()
        data = base64.b64decode(body["file_data"], validate=True)
        if not 0 < len(data) <= 1024:
            raise web.HTTPBadRequest()
        self.uploads.append((request.match_info["collection"], request.match_info["target"], body))
        if self.after_upload:
            await self.after_upload()
        if self.drop_upload:
            request.transport.close()
            return web.Response()
        return web.json_response(self.upload_body, status=self.upload_status, headers={"Connection": "close"})

    async def _send(self, request):
        body = await request.json()
        if body.get("msg_type") != 7:
            result = await super()._send(request)
            return self._send_response(result)
        if body.get("media") != {"file_info": "SECRET_TEST_FILE_INFO"}:
            raise web.HTTPBadRequest()
        # Delegate all normal window/sequence/error/lost-response behavior to
        # the established fake. Preserve the actual wire in its observations.
        class TextView:
            def __getattr__(self, name):
                return getattr(request, name)

            async def json(self):
                return {**body, "msg_type": 0, "content": "TEST_MEDIA"}
        before = len(self.received)
        result = await super()._send(TextView())
        if len(self.received) > before:
            self.received[-1]["payload"] = body
        return self._send_response(result)

    def _send_response(self, result):
        if self.deep_send_response:
            return web.Response(text='{"nest":' + '[' * 10000 + '0' + ']' * 10000 + '}',
                                content_type="application/json")
        return result


class ProbeFilesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.now = [1_800_000_000.0]

    async def run_files(self, fake, *, checks=FILE_CHECKS, group=True, user=False,
                        confirm=True, prepare_send=None, raw=False, send_fault=False):
        token = TokenManager(Credentials(fake.app_id, fake.app_secret), fake.base_url,
                             allow_test_server=True, max_attempts=1)
        self.addAsyncCleanup(token.aclose)
        rest = RestClient(token, allow_test_server=True)
        self.addAsyncCleanup(rest.aclose)
        if prepare_send:
            original = rest.prepare_send
            async def altered(kind, target):
                prepared = await original(kind, target)
                await prepare_send()
                return prepared
            rest.prepare_send = altered
        if send_fault:
            original_send = rest.send_message
            async def failed_after_handoff(*args, **kwargs):
                await original_send(*args, **kwargs)
                raise TypeError("SECRET_LOCAL_SEND_ERROR")
            rest.send_message = failed_after_handoff
        options = ProbeOptions(report=self.path / "report.json", app_id=fake.app_id,
            environment="test", group="TEST_GROUP" if group else None,
            user="TEST_USER" if user else None, checks=tuple(checks), duration=5,
            raw=self.path / "raw.json" if raw else None)
        output = []
        task = asyncio.create_task(run_probe(options, rest, GatewayConnection(rest),
            confirm=lambda _: confirm, output=output.append, clock=lambda: self.now[0]))
        if confirm:
            await asyncio.wait_for(fake.wait_ready(), 3)
            for kind in (["group"] if group else []) + (["user"] if user else []):
                for number in range(len(checks)):
                    await fake.emit("GROUP_MESSAGE_CREATE" if kind == "group" else "C2C_MESSAGE_CREATE", {
                        "id": f"TEST_{kind}_MESSAGE_{number}",
                        **({"group_openid": "TEST_GROUP"} if kind == "group" else {}),
                        "author": {"member_openid": "TEST_ACTOR"} if kind == "group" else {"user_openid": "TEST_USER"},
                        "timestamp": datetime.fromtimestamp(self.now[0], timezone.utc).isoformat(),
                        "content": "PRIVATE_TEST_CHAT_BODY",
                        "mentions": [{"is_you": True}],
                    })
        return await asyncio.wait_for(task, 6), output

    async def test_group_and_private_three_real_uploads_each_then_distinct_type7_windows(self):
        async with ProbeMediaFake(clock=lambda: self.now[0]) as fake:
            report, output = await self.run_files(fake, user=True, raw=True)
            self.assertEqual(len(fake.uploads), 6)
            self.assertEqual(len(fake.received), 6)
            self.assertIn("最多 12 次业务请求", output[0])
            self.assertIn("最多 6 次上传、6 条消息", output[0])
            self.assertEqual([(x["stage"], x["status"]) for x in report["checks"]],
                             [("upload", "uploaded"), ("send", "accepted")] * 6)
            for index, received in enumerate(fake.received):
                wire = received["payload"]
                kind = "group" if index < 3 else "user"
                self.assertEqual(wire["msg_id"], f"TEST_{kind}_MESSAGE_{index % 3}")
                self.assertEqual(wire["msg_seq"], 1)
                self.assertEqual(wire["msg_type"], 7)
                self.assertFalse(fake.uploads[index][2]["srv_send_msg"])
                fixed = sample(FILE_CHECKS[index % 3])
                self.assertEqual(base64.b64decode(fake.uploads[index][2]["file_data"]), fixed.content)
                self.assertEqual(report["checks"][index * 2]["sample_sha256"], fixed.summary["sample_sha256"])
            public = json.dumps(report) + "\n".join(output)
            for secret in ("TEST_GROUP", "TEST_USER", "TEST_ACTOR", "PRIVATE_TEST_CHAT_BODY",
                           "SECRET_TEST_FILE_INFO", "SECRET_TEST_FILE_UUID", fake.app_secret, fake.access_token):
                self.assertNotIn(secret, public)
            self.assertNotIn("SECRET_TEST_FILE", (self.path / "raw.json").read_text())
            self.assertNotIn("image_upload", [p["check"] for p in report["pending"]])

    async def test_cancelled_preview_has_no_token_gateway_upload_or_message(self):
        async with ProbeMediaFake() as fake:
            report, _ = await self.run_files(fake, confirm=False)
            self.assertEqual(report["status"], "cancelled")
            self.assertEqual((fake.token_calls, fake.gateway_queries, len(fake.uploads), len(fake.received)), (0, 0, 0, 0))

    async def test_upload_lost_response_stops_target_without_retry_or_later_checks(self):
        async with ProbeMediaFake(clock=lambda: self.now[0]) as fake:
            fake.drop_upload = True
            report, _ = await self.run_files(fake)
            self.assertEqual(len(fake.uploads), 1)
            self.assertEqual(fake.received, [])
            self.assertEqual(report["checks"][0]["status"], "ambiguous")
            self.assertEqual(report["checks"][-1]["reason"], "previous_send_ambiguous")

    async def test_unknown_upload_stops_only_its_target_not_other_explicit_target(self):
        async with ProbeMediaFake(clock=lambda: self.now[0]) as fake:
            fake.drop_upload = True
            async def later():
                if fake.uploads[-1][0] == "users":
                    fake.drop_upload = False
            fake.after_upload = later
            report, _ = await self.run_files(fake, checks=("pdf_upload",), user=True)
            self.assertEqual(len(fake.uploads), 2)
            self.assertEqual(len(fake.received), 1)
            self.assertEqual(fake.received[0]["target"], "TEST_USER")
            self.assertEqual(report["checks"][0]["status"], "ambiguous")

    async def test_upload_conflicting_message_or_malformed_success_never_sends(self):
        cases = [(200, {"id": "TEST_ALREADY_SENT"}), (202, {}), (200, {"ttl": True}),
                 (200, {"file_info": ""}), (200, {"file_uuid": "bad\nvalue"}),
                 (200, {"code": 40034006, "id": "TEST_ALREADY_SENT"})]
        for number, (status, updates) in enumerate(cases):
            with self.subTest(status=status, updates=updates):
                self.path = Path(self.directory.name) / str(number)
                self.path.mkdir()
                async with ProbeMediaFake(clock=lambda: self.now[0]) as fake:
                    fake.upload_status = status
                    fake.upload_body.update(updates)
                    report, _ = await self.run_files(fake, checks=("pdf_upload", "reply"))
                    self.assertEqual(len(fake.uploads), 1)
                    self.assertEqual(fake.received, [])
                    self.assertEqual(report["checks"][0]["status"], "ambiguous")

    async def test_confirmed_upload_rejection_has_no_type7_or_format_fallback(self):
        async with ProbeMediaFake(clock=lambda: self.now[0]) as fake:
            fake.upload_status = 400
            fake.upload_body = {"code": 50015014}
            report, _ = await self.run_files(fake, checks=("pdf_upload",))
            self.assertEqual(report["checks"][0]["status"], "rejected")
            self.assertEqual(len(fake.uploads), 1)
            self.assertEqual(fake.received, [])

    async def test_upload_latency_expiring_original_window_prevents_message(self):
        async with ProbeMediaFake(clock=lambda: self.now[0]) as fake:
            async def delayed():
                self.now[0] += 296
            fake.after_upload = delayed
            report, _ = await self.run_files(fake, checks=("pdf_upload",))
            self.assertEqual(len(fake.uploads), 1)
            self.assertEqual(fake.received, [])
            self.assertEqual(report["checks"][0]["status"], "uploaded")
            self.assertEqual(report["checks"][1]["reason"], "reply_window_expired_before_send")

    async def test_token_preparation_cannot_refresh_original_window_or_upload_ttl(self):
        for seconds in (56, 296):
            with self.subTest(seconds=seconds):
                self.path = Path(self.directory.name) / str(seconds)
                self.path.mkdir()
                async with ProbeMediaFake(clock=lambda: self.now[0]) as fake:
                    async def elapsed():
                        self.now[0] += seconds
                    report, _ = await self.run_files(fake, checks=("zip_upload",), prepare_send=elapsed)
                    self.assertEqual(len(fake.uploads), 1)
                    self.assertEqual(fake.received, [])
                    self.assertEqual(report["checks"][1]["status"], "not_run")

    async def test_message_response_lost_never_reuploads_resequences_or_sends_next_check(self):
        async with ProbeMediaFake(clock=lambda: self.now[0]) as fake:
            fake.drop_next_response = True
            report, _ = await self.run_files(fake)
            self.assertEqual(len(fake.uploads), 1)
            self.assertEqual(len(fake.received), 1)
            self.assertEqual(fake.received[0]["payload"]["msg_seq"], 1)
            self.assertEqual(report["checks"][1]["status"], "ambiguous")
            self.assertEqual(report["checks"][-1]["reason"], "previous_send_ambiguous")

    async def test_deep_json_after_accepted_message_is_reported_unknown_without_replay(self):
        async with ProbeMediaFake(clock=lambda: self.now[0]) as fake:
            fake.deep_send_response = True
            report, output = await self.run_files(fake)
            self.assertEqual(len(fake.uploads), 1)
            self.assertEqual(len(fake.received), 1)
            send = report["checks"][1]
            self.assertEqual((send["stage"], send["status"], send["category"]),
                             ("send", "ambiguous", "invalid_response"))
            self.assertEqual(report["checks"][-1]["reason"], "previous_send_ambiguous")
            self.assertNotIn("RecursionError", json.dumps(report) + "\n".join(output))

    async def test_text_deep_json_keeps_existing_error_record_and_stops_target(self):
        async with ProbeMediaFake(clock=lambda: self.now[0]) as fake:
            fake.deep_send_response = True
            report, _ = await self.run_files(fake, checks=("reply", "pdf_upload"))
            self.assertEqual(fake.uploads, [])
            self.assertEqual(len(fake.received), 1)
            self.assertEqual(report["checks"][0]["status"], "ambiguous")
            self.assertEqual(report["checks"][0]["category"], "invalid_response")
            self.assertEqual(report["checks"][-1]["reason"], "previous_send_ambiguous")

    async def test_unexpected_file_send_exception_after_handoff_still_records_safe_unknown(self):
        async with ProbeMediaFake(clock=lambda: self.now[0]) as fake:
            report, output = await self.run_files(fake, send_fault=True)
            self.assertEqual(len(fake.uploads), 1)
            self.assertEqual(len(fake.received), 1)
            self.assertEqual((report["checks"][1]["stage"], report["checks"][1]["status"],
                              report["checks"][1]["reason"]), ("send", "ambiguous", "send_unavailable"))
            self.assertEqual(report["checks"][-1]["reason"], "previous_send_ambiguous")
            self.assertNotIn("SECRET_LOCAL_SEND_ERROR", json.dumps(report) + "\n".join(output))

    async def test_real_tcp_connect_delay_checks_window_and_media_ttl_before_http_headers(self):
        from httpcore._backends.auto import AutoBackend
        original = AutoBackend.connect_tcp
        for seconds in (56, 296):
            with self.subTest(seconds=seconds):
                self.path = Path(self.directory.name) / str(seconds)
                self.path.mkdir()
                async with ProbeMediaFake(clock=lambda: self.now[0]) as fake:
                    delayed = []
                    async def connect(backend, *args, **kwargs):
                        stream = await original(backend, *args, **kwargs)
                        # Upload's Connection: close forces a real new TCP
                        # connection after the ordinary pre-send guard passed.
                        if fake.uploads:
                            delayed.append(True)
                            self.now[0] += seconds
                        return stream
                    with patch.object(AutoBackend, "connect_tcp", connect):
                        report, _ = await self.run_files(fake, checks=("pdf_upload",))
                    self.assertEqual(delayed, [True])
                    self.assertEqual(len(fake.uploads), 1)
                    self.assertEqual(fake.received, [])
                    self.assertEqual(report["checks"][1]["status"], "not_run")
                    self.assertEqual(report["checks"][1]["reason"], "reply_or_upload_or_batch_expired_before_send")

    async def test_actual_cli_file_option_runs_only_fixed_test_target(self):
        async with ProbeMediaFake() as fake:
            for name, value in (("id", fake.app_id), ("secret", fake.app_secret)):
                path = self.path / name
                path.write_text(value)
                path.chmod(0o600)
            output = io.StringIO()
            with redirect_stdout(output):
                task = asyncio.create_task(async_main(["--app-id-file", str(self.path / "id"),
                    "--app-secret-file", str(self.path / "secret"), "--group", "TEST_GROUP",
                    "--checks", "pdf_upload", "--duration", "5", "--confirm-batch",
                    "--report", str(self.path / "cli.json"), "--test-api-base", fake.base_url]))
                await asyncio.wait_for(fake.wait_ready(), 3)
                await fake.emit("GROUP_AT_MESSAGE_CREATE", {"id": "TEST_FRESH", "group_openid": "TEST_GROUP",
                    "author": {"member_openid": "TEST_ACTOR"},
                    "timestamp": datetime.now(timezone.utc).isoformat(), "content": "TEST_PRIVATE"})
                self.assertEqual(await asyncio.wait_for(task, 5), 0)
            self.assertEqual(len(fake.uploads), 1)
            self.assertEqual(len(fake.received), 1)
            self.assertNotIn("SECRET_TEST_FILE", output.getvalue())


class ProbeFileContractTests(unittest.TestCase):
    def test_samples_are_valid_deterministic_small_png_pdf_zip_and_contract_matches_product(self):
        for check in FILE_CHECKS:
            fixed = sample(check)
            self.assertEqual(fixed, sample(check))
            self.assertLessEqual(len(fixed.content), 1024)
        png = sample("image_upload").content
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        pos = 8
        while pos < len(png):
            length = struct.unpack(">I", png[pos:pos + 4])[0]
            raw = png[pos + 4:pos + 8 + length]
            self.assertEqual(struct.unpack(">I", png[pos + 8 + length:pos + 12 + length])[0], zlib.crc32(raw))
            pos += 12 + length
        pdf = sample("pdf_upload").content
        self.assertTrue(pdf.endswith(b"%%EOF\n"))
        xref = int(pdf.split(b"startxref\n")[1].splitlines()[0])
        self.assertTrue(pdf[xref:].startswith(b"xref\n"))
        self.assertNotIn(b"/JavaScript", pdf)
        with zipfile.ZipFile(io.BytesIO(sample("zip_upload").content)) as archive:
            self.assertEqual(archive.namelist(), ["README.txt"])
            self.assertIn(b"harmless ZIP sample", archive.read("README.txt"))

    def test_opaque_upload_handles_never_enter_repr_and_invalid_contract_is_unknown(self):
        body = {"file_info": "SECRET_HANDLE", "file_uuid": "SECRET_UUID", "ttl": 0}
        media = upload_receipt(200, body, started_at=1000)
        self.assertNotIn("SECRET", repr(media))
        self.assertLessEqual(media.expires_at, 1000 + 86400)
        for patch_body in ({"ttl": -1}, {"ttl": "60"}, {"file_info": "x\ud800"}, {"code": "0"}):
            with self.subTest(patch_body=patch_body), self.assertRaises(UnconfirmedUpload):
                upload_receipt(200, {**body, **patch_body}, started_at=1000)

    def test_clean_process_probe_import_never_imports_application_or_nonebot(self):
        import src.qq_official.probe as module
        protocol_path = str(Path(module.__file__).parent)
        source = """import importlib.abc, sys
class Reject(importlib.abc.MetaPathFinder):
 def find_spec(self, fullname, *args):
  if fullname == 'nonebot' or fullname.startswith(('nonebot.', 'src.plugins')): raise AssertionError(fullname)
sys.meta_path.insert(0, Reject())
import src.qq_official
src.qq_official.__path__.insert(0, sys.argv[1])
from src.qq_official.probe import argument_parser
assert 'pdf_upload' in argument_parser().format_help()
"""
        result = subprocess.run([sys.executable, "-c", source, protocol_path], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_samples_do_not_read_arbitrary_files_or_network(self):
        with patch("builtins.open", side_effect=AssertionError("no file input")):
            for check in FILE_CHECKS:
                self.assertEqual(len(sample(check).summary["sample_sha256"]), hashlib.sha256().digest_size * 2)
        with self.assertRaises(ValueError):
            sample("/tmp/private-user-file.pdf")


class HeaderGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_header_events_recheck_without_reissuing_intent_or_observation(self):
        for name in ("http11.send_request_headers.started", "http2.send_request_headers.started"):
            guards, intents, observations = [], [], []
            class TraceTransport(httpx.AsyncBaseTransport):
                async def handle_async_request(self, request):
                    trace = request.extensions["trace"]
                    await trace("connection.connect_tcp.complete", {"SECRET": "NEVER_READ"})
                    await trace(name, {"SECRET": "NEVER_READ"})
                    return httpx.Response(200, json={"id": "TEST_OK"})
            async def guard(stage):
                guards.append(stage)
            async with httpx.AsyncClient(transport=TraceTransport()) as client:
                with scoped_request_guard(guard, before_headers=True), scoped_request_observer(lambda: observations.append(1)):
                    await request_json_response(client, "POST", "https://fixture.invalid/messages", stage="send",
                                                before_dispatch=lambda: intents.append(1))
            self.assertEqual(guards, ["send", "send"])
            self.assertEqual(intents, [1])
            self.assertEqual(observations, [1])

    async def test_default_nested_and_reset_leave_ordinary_transport_unchanged(self):
        traced, calls = [], []
        async def handler(request):
            traced.append("trace" in request.extensions)
            if "trace" in request.extensions:
                await request.extensions["trace"]("http11.send_request_headers.started", {})
            return httpx.Response(200, json={})
        async def outer(stage):
            calls.append("outer")
        async def inner(stage):
            calls.append("inner")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            async def send():
                await request_json_response(client, "POST", "https://fixture.invalid/messages")
            with scoped_request_guard(outer, before_headers=True):
                await send()
                with scoped_request_guard(inner):
                    await send()
                await send()
            await send()
        self.assertEqual(traced, [True, False, True, False])
        self.assertEqual(calls, ["outer", "outer", "inner", "outer", "outer"])
