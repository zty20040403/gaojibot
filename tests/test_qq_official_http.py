from __future__ import annotations

import asyncio
import errno
import os
import tempfile
import struct
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import httpx

from src.qq_official.credentials import Credentials, CredentialError, validate_api_base, validate_gateway_url, verify_private_file
from src.qq_official.errors import QQAPIError
from src.qq_official.http import RestClient
from src.qq_official.outgoing import markdown_payload, text_payload
from src.qq_official.token import TokenManager


FAKE = Credentials("TEST_APP", "TEST_SECRET")
BASE = "http://127.0.0.1:12345"


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class CredentialTests(unittest.TestCase):
    def test_repr_never_discloses_identity_or_secret(self):
        self.assertEqual(repr(FAKE), "Credentials()")

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_protected_files_and_permission_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            app, secret = Path(directory) / "app", Path(directory) / "secret"
            for path, value in ((app, "TEST_APP\n"), (secret, "TEST_SECRET\n")):
                path.write_text(value)
                path.chmod(0o600)
            self.assertEqual(Credentials.from_files(app, secret), FAKE)
            for mode in (0o644, 0o666, 0o700, 0o640):
                with self.subTest(mode=mode):
                    secret.chmod(mode)
                    with self.assertRaises(CredentialError) as caught:
                        Credentials.from_files(app, secret)
                    self.assertNotIn(str(secret), str(caught.exception))
            secret.chmod(0o600)
            link = Path(directory) / "symlink"
            link.symlink_to(secret)
            with self.assertRaises(CredentialError):
                Credentials.from_files(app, link)

    @unittest.skipUnless(sys.platform == "darwin", "macOS native extended ACL")
    def test_mode_0600_with_everyone_read_acl_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credential"
            path.write_text("TEST_SECRET")
            path.chmod(0o600)
            subprocess.run(["/bin/chmod", "+a", "everyone allow read", str(path)], check=True,
                           capture_output=True)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with path.open("rb") as stream:
                with self.assertRaisesRegex(CredentialError, "extended ACL"):
                    verify_private_file(stream.fileno())
            with self.assertRaisesRegex(CredentialError, "extended ACL"):
                Credentials.from_files(path, path)

    @unittest.skipUnless(sys.platform == "darwin", "macOS inherited extended ACL")
    def test_new_mode_0600_file_with_inherited_acl_is_rejected_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["/bin/chmod", "+a", "everyone allow read,file_inherit,directory_inherit", directory],
                           check=True, capture_output=True)
            path = Path(directory) / "report"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                self.assertEqual(os.fstat(fd).st_mode & 0o777, 0o600)
                with self.assertRaisesRegex(CredentialError, "extended ACL"):
                    verify_private_file(fd)
                self.assertEqual(os.fstat(fd).st_size, 0)
            finally:
                os.close(fd)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux POSIX ACL")
    def test_linux_named_user_acl_is_rejected_even_when_mask_keeps_mode_0600(self):
        with tempfile.NamedTemporaryFile() as stream:
            # Linux POSIX ACL xattr wire version2; even a currently masked named
            # reader is rejected instead of trusting chmod's apparent privacy.
            entries = [(1, 6, 0xFFFFFFFF), (2, 4, os.geteuid() + 1), (4, 0, 0xFFFFFFFF),
                       (16, 0, 0xFFFFFFFF), (32, 0, 0xFFFFFFFF)]
            acl = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)
            try:
                os.setxattr(stream.fileno(), "system.posix_acl_access", acl)
            except OSError as error:
                if error.errno in {errno.ENOTSUP, errno.EOPNOTSUPP}:
                    self.skipTest("Test filesystem has no writable POSIX ACL support")
                raise
            self.assertEqual(os.fstat(stream.fileno()).st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(CredentialError, "extended ACL"):
                verify_private_file(stream.fileno())

    @unittest.skipIf(os.name == "nt", "POSIX descriptor inspection")
    def test_unavailable_native_acl_inspection_fails_closed(self):
        with tempfile.NamedTemporaryFile() as stream:
            with patch("src.qq_official.credentials.ctypes.CDLL", side_effect=OSError("TEST_SECRET")):
                with self.assertRaises(CredentialError) as caught:
                    verify_private_file(stream.fileno())
            self.assertNotIn("TEST_SECRET", str(caught.exception))

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux POSIX ACL")
    def test_linux_uninspectable_acl_is_not_treated_as_absent(self):
        with tempfile.NamedTemporaryFile() as stream:
            with patch("src.qq_official.credentials.os.getxattr", side_effect=OSError(errno.ENOTSUP, "unsupported")):
                with self.assertRaisesRegex(CredentialError, "could not be verified"):
                    verify_private_file(stream.fileno())

    @unittest.skipIf(os.name == "nt", "POSIX root service-group policy")
    def test_restricted_root_service_group_remains_allowed_but_extra_member_denied(self):
        with tempfile.NamedTemporaryFile() as stream:
            info = SimpleNamespace(st_mode=0o100640, st_uid=0, st_gid=1234)
            with patch("src.qq_official.credentials.os.fstat", return_value=info), \
                    patch("src.qq_official.credentials.os.geteuid", return_value=1001), \
                    patch("src.qq_official.credentials.os.getegid", return_value=1234), \
                    patch("src.qq_official.credentials.os.getgroups", return_value=[1234]), \
                    patch("pwd.getpwuid", return_value=SimpleNamespace(pw_name="test_service")), \
                    patch("pwd.getpwall", return_value=[SimpleNamespace(pw_name="test_service", pw_gid=1234)]), \
                    patch("grp.getgrgid", return_value=SimpleNamespace(gr_mem=["test_service"])) as group:
                verify_private_file(stream.fileno())
                group.return_value = SimpleNamespace(gr_mem=["test_service", "unrelated_user"])
                with self.assertRaisesRegex(CredentialError, "owner or permissions"):
                    verify_private_file(stream.fileno())

    def test_untrusted_endpoints_cannot_receive_credentials(self):
        for url in (
            "http://api.bot.qq.com", "https://api.bot.qq.com.evil.invalid", "https://evil.invalid",
            "https://api.bot.qq.com@evil.invalid", "https://api.bot.qq.com:444",
            "https://api.bot.qq.com/path", "https://api.bot.qq.com?token=x", "https://api.bot.qq.com/#x",
        ):
            with self.subTest(url=url), self.assertRaises(CredentialError):
                validate_api_base(url)
        self.assertEqual(validate_api_base("https://api.bot.qq.com/"), "https://api.bot.qq.com")
        for url in (BASE, "http://localhost:12345", "http://127.0.0.2:12345"):
            with self.subTest(url=url), self.assertRaises(CredentialError):
                validate_api_base(url)
        self.assertEqual(validate_api_base(BASE, allow_test_server=True, credentials=FAKE), BASE)
        with self.assertRaises(CredentialError):
            validate_api_base(BASE, allow_test_server=True, credentials=Credentials("APP_NOT_TEST", "SECRET_NOT_TEST"))
        with self.assertRaises(CredentialError):
            validate_api_base("https://api.bot.qq.com", allow_test_server=True, credentials=FAKE)
        self.assertEqual(validate_gateway_url("wss://api.sgroup.qq.com/websocket"), "wss://api.sgroup.qq.com/websocket")
        with self.assertRaises(CredentialError):
            validate_gateway_url("wss://other.qq.com/websocket")


class TokenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = Clock()
        self.requests = []
        self.responses = []

        async def handle(request):
            self.requests.append(request)
            await asyncio.sleep(0)
            result = self.responses.pop(0) if self.responses else {"access_token": "TEST_TOKEN", "expires_in": 100}
            if isinstance(result, Exception):
                raise result
            if isinstance(result, httpx.Response):
                return result
            return httpx.Response(200, json=result)

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.manager = TokenManager(FAKE, BASE, client=self.client, clock=self.clock, sleep=self.clock.sleep,
                                    jitter=lambda: 0.0, allow_test_server=True)

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_parallel_refresh_single_flight_and_wire_contract(self):
        results = await asyncio.gather(*(self.manager.get() for _ in range(25)))
        self.assertEqual(results, ["TEST_TOKEN"] * 25)
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request.url.path, "/app/getAppAccessToken")
        self.assertEqual(request.content, b'{"appId":"TEST_APP","clientSecret":"TEST_SECRET"}')
        self.assertNotIn("authorization", request.headers)
        self.assertNotIn("TEST_", repr(self.manager))

    async def test_same_token_never_extends_original_expiry(self):
        self.responses = [
            {"access_token": "TEST_TOKEN", "expires_in": 100},
            {"access_token": "TEST_TOKEN", "expires_in": 7200},
            {"access_token": "TEST_FRESH", "expires_in": 100},
        ]
        await self.manager.get()
        self.clock.now += 51
        self.assertEqual(await self.manager.get(), "TEST_TOKEN")
        self.assertEqual(self.manager.expires_at, 1100)
        results = await asyncio.gather(*(self.manager.get() for _ in range(10)))
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(results), 10)
        self.clock.now = 1101
        self.assertEqual(await self.manager.get(), "TEST_FRESH")

    async def test_returned_expired_token_is_not_used(self):
        await self.manager.get()
        self.clock.now += 101
        with self.assertRaises(QQAPIError) as caught:
            await self.manager.get()
        self.assertEqual(caught.exception.category, "stale_token_response")
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(self.clock.sleeps, [1.0, 2.0])

    async def test_invalidation_only_affects_expected_token(self):
        await self.manager.get()
        self.manager.invalidate("TEST_TOKEN")
        self.responses = [{"access_token": "TEST_NEW", "expires_in": 100}]
        self.assertEqual(await self.manager.get(), "TEST_NEW")
        self.manager.invalidate("TEST_TOKEN")
        self.assertEqual(await self.manager.get(), "TEST_NEW")
        self.assertEqual(len(self.requests), 2)

    async def test_invalidated_token_cannot_be_returned_as_fresh(self):
        await self.manager.get()
        self.manager.invalidate()
        with self.assertRaises(QQAPIError) as caught:
            await self.manager.get()
        self.assertEqual(caught.exception.category, "stale_token_response")

    async def test_token_failure_backoff_is_bounded_and_shared(self):
        self.responses = [httpx.ConnectError("TEST_SECRET") for _ in range(3)]
        results = await asyncio.gather(*(self.manager.get() for _ in range(20)), return_exceptions=True)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.clock.sleeps, [1.0, 2.0])
        self.assertTrue(all(isinstance(result, QQAPIError) for result in results))
        self.assertNotIn("TEST_SECRET", str(results))
        self.clock.now += 6
        self.assertEqual(await self.manager.get(), "TEST_TOKEN")

    async def test_refresh_failure_uses_still_valid_token_but_never_expired(self):
        await self.manager.get()
        self.clock.now += 51
        self.responses = [httpx.ReadTimeout("TEST_SECRET") for _ in range(3)]
        self.assertEqual(await self.manager.get(), "TEST_TOKEN")
        self.assertEqual(self.manager.expires_at, 1100)
        self.clock.now = 1101
        self.responses = [httpx.ReadTimeout("TEST_SECRET") for _ in range(3)]
        with self.assertRaises(QQAPIError):
            await self.manager.get()

    async def test_authentication_rejection_does_not_retry(self):
        self.responses = [httpx.Response(401, json={"message": "TEST_SECRET"})]
        with self.assertRaises(QQAPIError) as caught:
            await self.manager.get()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(caught.exception.category, "authentication")
        self.assertNotIn("TEST_SECRET", str(caught.exception))

    async def test_server_token_cooldown_is_shared_without_sleeping_under_lock(self):
        self.responses = [httpx.Response(429, headers={"retry-after": "30"}, json={})]
        results = await asyncio.gather(*(self.manager.get() for _ in range(10)), return_exceptions=True)
        self.assertTrue(all(isinstance(result, QQAPIError) for result in results))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.clock.sleeps, [])
        self.clock.now += 29
        with self.assertRaises(QQAPIError):
            await self.manager.get()
        self.assertEqual(len(self.requests), 1)
        self.clock.now += 1
        self.assertEqual(await self.manager.get(), "TEST_TOKEN")

    async def test_token_body_errors_and_invalid_expiry_are_not_cached(self):
        for value in (True, None, "nan", "inf", 0, -1):
            with self.subTest(value=value):
                clock = Clock()
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
                    200, json={"access_token": "TEST_TOKEN", "expires_in": value},
                ))) as client:
                    manager = TokenManager(FAKE, BASE, client=client, clock=clock, sleep=clock.sleep,
                                           allow_test_server=True, max_attempts=1)
                    with self.assertRaises(QQAPIError):
                        await manager.get()
        self.responses = [httpx.Response(200, json={"code": 40034006, "access_token": "TEST_TOKEN", "expires_in": 100})]
        with self.assertRaises(QQAPIError):
            await self.manager.get()


class RestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.seen = []
        self.result = httpx.Response(200, json={"id": "MESSAGE_TEST_1", "ext_info": {"ref_idx": "REF_TEST_1"}})

        async def handle(request):
            self.seen.append(request)
            if request.url.path == "/app/getAppAccessToken":
                return httpx.Response(200, json={"access_token": "TEST_TOKEN", "expires_in": 100})
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=True)
        self.token = TokenManager(FAKE, BASE, client=self.client, allow_test_server=True)
        self.rest = RestClient(self.token, client=self.client, allow_test_server=True)

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_sends_group_and_c2c_to_correct_route_with_receipts(self):
        for kind, route in (("group", "groups"), ("c2c", "users")):
            result = await self.rest.send_message(kind, "OPENID_TEST", text_payload("hello", msg_id="MSG_TEST", msg_seq=1))
            self.assertEqual(result["id"], "MESSAGE_TEST_1")
            self.assertEqual(self.seen[-1].url.path, f"/v2/{route}/OPENID_TEST/messages")
            self.assertEqual(self.seen[-1].headers["authorization"], "QQBot TEST_TOKEN")

    async def test_error_evidence_table_including_http_200_and_unknown_4xx(self):
        cases = [
            (200, {"code": 40034006}, "content_rejected", False),
            (200, {"err_code": "40034128"}, "window_exhausted", False),
            (200, {"code": 0, "err_code": 40034105}, "proactive_denied", False),
            (429, {}, "rate_limited", False),
            (401, {}, "authentication", False),
            (403, {"code": 9999}, "unknown", None),
            (500, {}, "unknown", None),
            (200, {"code": "unrecognized"}, "unknown", None),
            (200, {"code": 40034128, "err_code": 999}, "unknown", None),
        ]
        for status, body, category, accepted in cases:
            with self.subTest(status=status, body=body):
                # Each error contract starts with independent valid credentials;
                # the authentication case intentionally invalidates its token.
                self.token = TokenManager(FAKE, BASE, client=self.client, allow_test_server=True)
                self.rest = RestClient(self.token, client=self.client, allow_test_server=True)
                self.result = httpx.Response(status, json={**body, "message": "TEST_SECRET"})
                before = len(self.seen)
                with self.assertRaises(QQAPIError) as caught:
                    await self.rest.send_message("group", "OPENID_TEST", text_payload("hello"))
                self.assertEqual(caught.exception.category, category)
                self.assertIs(caught.exception.accepted, accepted)
                self.assertNotIn("TEST_SECRET", repr(caught.exception))
                self.assertEqual(len([request for request in self.seen[before:] if request.method == "POST" and "/messages" in request.url.path]), 1)

    async def test_delayed_401_invalidates_request_token_not_concurrently_refreshed_token(self):
        for send in (False, True):
            with self.subTest(send=send):
                clock, arrived, release = Clock(), asyncio.Event(), asyncio.Event()
                token_requests, api_requests = [], []

                async def handle(request):
                    if request.url.path == "/app/getAppAccessToken":
                        token_requests.append(request)
                        token = "TEST_OLD_TOKEN" if len(token_requests) == 1 else "TEST_NEW_TOKEN"
                        return httpx.Response(200, json={"access_token": token, "expires_in": 100})
                    api_requests.append(request)
                    arrived.set()
                    await release.wait()
                    return httpx.Response(401, json={})

                async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                    manager = TokenManager(FAKE, BASE, client=client, clock=clock, sleep=clock.sleep,
                                           allow_test_server=True)
                    rest = RestClient(manager, client=client, allow_test_server=True)
                    call = rest.send_message("group", "OPENID_TEST", text_payload("hello")) if send else rest.gateway()
                    pending = asyncio.create_task(call)
                    await arrived.wait()
                    clock.now += 51
                    self.assertEqual(await manager.get(), "TEST_NEW_TOKEN")
                    release.set()
                    with self.assertRaises(QQAPIError) as caught:
                        await pending
                    self.assertEqual(caught.exception.category, "authentication")
                    self.assertFalse(caught.exception.accepted)
                    self.assertEqual(await manager.get(), "TEST_NEW_TOKEN")
                    self.assertEqual(len(token_requests), 2)
                    self.assertEqual(len(api_requests), 1)
                    self.assertEqual(api_requests[0].headers["authorization"], "QQBot TEST_OLD_TOKEN")

    async def test_confirmed_auth_rejection_invalidates_used_token_without_retry(self):
        self.result = httpx.Response(401, json={})
        with patch.object(self.token, "invalidate", wraps=self.token.invalidate) as invalidate:
            with self.assertRaises(QQAPIError):
                await self.rest.send_message("group", "OPENID_TEST", text_payload("hello"))
            invalidate.assert_called_once_with("TEST_TOKEN")
        self.assertEqual(len(self.seen), 2)  # one token request, one send

    async def test_accepted_then_lost_response_is_ambiguous_and_not_retried(self):
        self.result = httpx.ReadTimeout("request body TEST_SECRET OPENID_TEST")
        with self.assertRaises(QQAPIError) as caught:
            await self.rest.send_message("group", "OPENID_TEST", text_payload("hello"))
        self.assertIsNone(caught.exception.accepted)
        self.assertEqual(caught.exception.category, "transport_ambiguous")
        self.assertEqual(len(self.seen), 2)
        self.assertNotIn("TEST_SECRET", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    async def test_connection_failure_proves_unsent_but_transport_does_not_retry(self):
        self.result = httpx.ConnectError("TEST_SECRET")
        with self.assertRaises(QQAPIError) as caught:
            await self.rest.send_message("group", "OPENID_TEST", text_payload("hello"))
        self.assertIs(caught.exception.accepted, False)
        self.assertEqual(len(self.seen), 2)

    async def test_redirect_is_never_followed_even_with_injected_client(self):
        self.result = httpx.Response(302, headers={"location": "https://evil.invalid/secret"}, json={})
        with self.assertRaises(QQAPIError):
            await self.rest.gateway()
        self.assertEqual(len(self.seen), 2)
        self.assertTrue(all(request.url.host == "127.0.0.1" for request in self.seen))

    async def test_error_metadata_safe_and_retry_after_bounded(self):
        self.result = httpx.Response(429, headers={"x-tps-trace-id": "TRACE_TEST_1", "retry-after": "30"}, json={})
        with self.assertRaises(QQAPIError) as caught:
            await self.rest.gateway()
        self.assertEqual(caught.exception.request_id, "TRACE_TEST_1")
        self.assertEqual(caught.exception.retry_after, 30)
        self.result = httpx.Response(429, headers={"x-tps-trace-id": "TEST_TOKEN", "retry-after": "inf"}, json={})
        with self.assertRaises(QQAPIError) as caught:
            await self.rest.gateway()
        self.assertIsNone(caught.exception.request_id)
        self.assertIsNone(caught.exception.retry_after)

    async def test_invalid_response_or_missing_receipt_is_not_success(self):
        for result in (
            httpx.Response(200, text="<html>TEST_SECRET</html>"),
            httpx.Response(200, json=[]),
            httpx.Response(200, json={}),
            httpx.Response(200, text="x" * (4 * 1024 * 1024 + 1)),
        ):
            self.result = result
            with self.subTest(result=result), self.assertRaises(QQAPIError) as caught:
                await self.rest.send_message("group", "OPENID_TEST", text_payload("hello"))
            self.assertIsNone(caught.exception.accepted)
            self.assertNotIn("TEST_SECRET", repr(caught.exception))

    async def test_foreign_paths_and_environment_switch_rejected_before_network(self):
        for path in ("//evil.invalid", "https://evil.invalid", "/../secret", "/%2e%2e/secret", "/gateway?token=x"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                await self.rest.request("GET", path)
        self.assertFalse(self.seen)
        with self.assertRaises(ValueError):
            RestClient(self.token, "http://127.0.0.1:12346", client=self.client, allow_test_server=True)


class OutgoingTests(unittest.TestCase):
    def test_payload_and_reference_use_explicit_credential_ids(self):
        self.assertEqual(text_payload("hi", msg_id="MSG_TEST", msg_seq=1, reference="IDX_TEST"), {
            "content": "hi", "msg_type": 0, "msg_id": "MSG_TEST", "msg_seq": 1,
            "message_reference": {"message_id": "IDX_TEST"},
        })
        self.assertEqual(markdown_payload("$x$\n```\n<x>\n```", event_id="EVENT_TEST", msg_seq=2), {
            "msg_type": 2, "markdown": {"content": "$x$\n```\n<x>\n```"}, "event_id": "EVENT_TEST", "msg_seq": 2,
        })

    def test_platform_tags_are_literal_even_in_markdown(self):
        for builder in (text_payload, markdown_payload):
            for content in ('<qqbot-at-user id="TEST_OPENID" />', '<faceType=1,faceId="306">', "<@TEST_OPENID>", "<emoji:306>"):
                with self.subTest(builder=builder, content=content):
                    payload = builder(content)
                    rendered = payload.get("content", payload.get("markdown", {}).get("content"))
                    self.assertTrue(rendered.startswith("&lt;"))

    def test_wire_limits_distinguish_characters_and_bytes(self):
        self.assertEqual(text_payload("中" * 2000)["content"], "中" * 2000)
        with self.assertRaises(ValueError):
            text_payload("中" * 1000, max_bytes=2000)
        with self.assertRaises(ValueError):
            text_payload("😀" * 2001)
        with self.assertRaises(ValueError):
            text_payload("<@TEST_OPENID>", max_chars=14)

    def test_invalid_credential_combinations_rejected(self):
        for kwargs in (
            {"msg_id": "MSG_TEST"}, {"msg_id": "MSG_TEST", "msg_seq": 65536},
            {"msg_id": "MSG_TEST", "msg_seq": True}, {"msg_seq": -1},
            {"msg_id": "MSG_TEST", "event_id": "EVENT_TEST", "msg_seq": 1},
            {"event_id": ""}, {"reference": ""},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                text_payload("hi", **kwargs)


if __name__ == "__main__":
    unittest.main()
