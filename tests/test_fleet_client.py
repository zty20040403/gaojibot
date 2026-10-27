from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import nonebot

# Client tests must not load local dotenv credentials during plugin initialization.
with patch("nonebot.config.DotEnvSettingsSource._read_env_files", return_value={}):
    nonebot.init()

from src.plugins.ai_chat.agent.external import ExternalCalls, active_external
from src.plugins.ai_chat.fleet_client import FleetControlClient, FleetControlError
from src.plugins.ai_chat.fleet_output import project_jobs_logs


class FleetControlClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.credential = "synthetic-fleet-credential-0123456789abcdef"
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.response_body = b'{"ok":true}'
        self.client = FleetControlClient(
            "http://control.test", "/nonexistent/synthetic-fleet-token",
            transport=httpx.MockTransport(self.handle),
        )
        self.token_patch = patch.object(self.client, "_token", return_value=self.credential)
        self.token_patch.start()

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, content=self.response_body)

    async def asyncTearDown(self) -> None:
        self.token_patch.stop()
        await self.client.close()

    async def test_desktop_file_credential_works_without_bypassing_write_authorization(self):
        from src.local_runtime.cluster_credentials import cluster_credentials
        self.token_patch.stop()
        with patch.dict(os.environ, {"AI_CLUSTER_ENABLED": "true"}):
            with cluster_credentials(self.credential):
                token_file = Path(os.environ["AI_CLUSTER_CONTROL_TOKEN_FILE"])
                client = FleetControlClient("http://control.test", str(token_file), transport=httpx.MockTransport(self.handle))
                try:
                    await client.capabilities()
                    self.assertEqual(self.requests[-1].url.path, "/v1/capabilities")
                    self.assertEqual(self.requests[-1].headers["Authorization"], "Bearer " + self.credential)
                    before = len(self.requests)
                    with self.assertRaises(FleetControlError):
                        await client._request("POST", "/v1/jobs", {"operation": "service.restart"})
                    self.assertEqual(len(self.requests), before)
                finally:
                    await client.close()
            self.assertFalse(token_file.exists())

    def reply(self, status: int, value: object) -> None:
        self.status = status
        self.response_body = json.dumps(value, ensure_ascii=False).encode()

    async def request_error(self, payload=None) -> FleetControlError:
        with self.assertRaises(FleetControlError) as caught:
            await self.client._raw_request("POST", "/v1/ops/call", payload)
        return caught.exception

    def detail(self, error: FleetControlError) -> dict:
        self.assertEqual(error.code, "invalid_request")
        self.assertFalse(error.retryable)
        self.assertLessEqual(len(str(error)), 1000)
        return json.loads(str(error).split(": ", 1)[1])

    def log_response(self, stdout=b"disk usage: 10000\n", stderr=b"", **metadata) -> dict:
        return {"ok": True, "operation": "jobs.logs", "result": {
            "encoding": "base64",
            "stdout_base64": base64.b64encode(stdout).decode(),
            "stderr_base64": base64.b64encode(stderr).decode(),
            "next_stdout_offset": 100 + len(stdout),
            "next_stderr_offset": 20 + len(stderr),
            "complete": False, "truncated": True, **metadata,
        }}

    async def logs(self, response: dict, payload=None) -> dict:
        self.reply(200, response)
        return await self.client._raw_request("POST", "/v1/ops/call", payload or {
            "operation": "jobs.logs", "params": {"job_id": "job-test", "stdout_offset": 0},
        }, actor="qq:2", origin="test:scope")

    async def test_422_exposes_validation_fields_without_input_or_context(self) -> None:
        payload = {"operation": "exec.run", "params": {
            "host": "h310", "env": {"PASSWORD": "synthetic-body-secret"},
        }}
        self.reply(422, {"detail": [
            {"loc": ["body", "params", "argv"], "msg": "Field required", "type": "missing",
             "input": payload, "ctx": {"error": "synthetic-context-secret"},
             "url": "https://internal.test/?token=synthetic-upstream-secret"},
            {"loc": ["body", "params", "timeout"], "msg": "Input should be a valid integer",
             "type": "int_parsing", "input": "synthetic-other-secret"},
        ], "request": payload})
        error = await self.request_error(payload)
        detail = self.detail(error)["detail"]
        self.assertEqual(detail[0], {
            "loc": ["body", "params", "argv"], "msg": "Field required", "type": "missing",
        })
        self.assertEqual(detail[1]["type"], "int_parsing")
        self.assertNotIn("synthetic-", str(error))
        self.assertNotIn('"input"', str(error))
        self.assertNotIn('"ctx"', str(error))

    async def test_ops_422_string_detail_keeps_missing_parameter_actionable(self) -> None:
        self.reply(422, {"detail": "Invalid operation parameters: 'argv' is a required property"})
        error = await self.request_error({"operation": "exec.run", "params": {"host": "h310"}})
        self.assertIn("'argv' is a required property", self.detail(error)["detail"])

    async def test_exec_timeout_in_command_keeps_parameter_path_actionable(self) -> None:
        payload = {"operation": "exec.run", "params": {"host": "h310", "command": {
            "argv": ["sh", "-lc", "df /var"], "timeout_seconds": 30,
        }}}
        self.reply(422, {"detail": [{
            "loc": ["body", "params", "command", "timeout_seconds"],
            "msg": "Extra inputs are not permitted", "type": "extra_forbidden", "input": 30,
        }]})
        detail = self.detail(await self.request_error(payload))["detail"][0]
        self.assertEqual(detail["loc"], ["body", "params", "command", "timeout_seconds"])
        self.assertEqual(detail["type"], "extra_forbidden")

    async def test_error_redacts_echoes_in_every_allowed_field_before_truncating(self) -> None:
        secret = "synthetic-echo-secret"
        escaped = "synthetic-line\nsecret"
        entry = {
            "loc": ["body", secret], "type": "value_error." + secret,
            "msg": f"Invalid {secret}; {repr(escaped)}; {self.credential}; "
                   "Bearer synthetic-upstream-token; password='two word secret'; "
                   "https://user:synthetic-url-secret@internal.test/?key=hidden",
            "input": {"value": secret},
        }
        self.reply(422, {"detail": [entry]})
        error = await self.request_error({"params": {"arbitrary": [secret, escaped]}})
        self.detail(error)
        for value in (secret, "synthetic-line", "synthetic-upstream-token", "two word secret",
                      "synthetic-url-secret", self.credential):
            self.assertNotIn(value, str(error))
        self.assertIn("[REDACTED]", str(error))

    async def test_errors_never_expose_signature_or_token_even_without_labels(self) -> None:
        def handler(request):
            self.reply(422, {"detail": "Rejected " + request.headers["X-KC-Signature"]
                            + " and " + self.credential})
            return self.handle(request)

        self.client._client._transport = httpx.MockTransport(handler)
        with self.assertRaises(FleetControlError) as caught:
            await self.client._raw_request("POST", "/v1/ops/call", {"operation": "exec.run"},
                                           actor="qq:2", origin="test:scope")
        self.detail(caught.exception)
        self.assertNotIn(self.requests[0].headers["X-KC-Signature"], str(caught.exception))
        self.assertNotIn(self.credential, str(caught.exception))

    async def test_other_4xx_support_string_and_allowlisted_object_details(self) -> None:
        for status, detail in (
            (400, "Invalid operation parameters: 'argv' is a required property"),
            (409, {"message": "Idempotency key belongs to a different approved intent", "code": "conflict",
                   "credentials": {"token": "synthetic-hidden-token"}}),
            (415, {"msg": "Expected application/json", "type": "media_type"}),
        ):
            with self.subTest(status=status):
                self.reply(status, {"detail": detail})
                error = await self.request_error()
                projected = self.detail(error)["detail"]
                self.assertEqual(projected, detail if isinstance(detail, str) else {
                    key: value for key, value in detail.items() if key != "credentials"
                })
                self.assertNotIn("synthetic-hidden-token", str(error))

    async def test_special_status_codes_and_retry_rules_do_not_change(self) -> None:
        cases = [
            (401, "unauthorized", False, "Fleet control rejected the credential"),
            (403, "forbidden", False, "Fleet access was denied"),
            (404, "unsupported", False, "Fleet capability is unavailable"),
            (408, "upstream_busy", True, "Fleet control is temporarily busy"),
            (429, "upstream_busy", True, "Fleet control is temporarily busy"),
            (500, "upstream_error", True, "Fleet control returned HTTP 500"),
            (503, "upstream_error", True, "Fleet control returned HTTP 503"),
        ]
        for status, code, retryable, message in cases:
            with self.subTest(status=status):
                self.reply(status, {"detail": "synthetic-upstream-secret"})
                error = await self.request_error()
                self.assertEqual((error.code, error.retryable, str(error)), (code, retryable, message))

    async def test_unusable_4xx_bodies_fall_back_without_reflecting_raw_content(self) -> None:
        bodies = [b"", b"<html>synthetic-secret</html>", b"\xff", b'{"detail":', b"[]",
                  b'{"detail":null}', b'{"detail":42}', b'{"detail":{"input":"secret"}}',
                  b'{"detail":[null,12,{"ctx":{"secret":"hidden"}}]}',
                  b'{"detail":' + b"[" * 2000 + b"]" * 2000 + b"}"]
        for body in bodies:
            with self.subTest(body=body[:40]):
                self.status, self.response_body = 422, body
                error = await self.request_error()
                self.assertEqual(str(error), "Fleet control returned HTTP 422")
                self.assertEqual(error.code, "invalid_request")

    async def test_error_summary_is_bounded_valid_json_with_truncation_marker(self) -> None:
        self.reply(422, {"detail": [{"loc": ["body", "params", "argv", index, "extra"],
                    "msg": "Expected a string. " * 100, "type": "string_type"} for index in range(40)]})
        summary = self.detail(await self.request_error())
        self.assertTrue(summary["truncated"])
        self.assertGreater(len(summary["detail"]), 0)
        self.assertLessEqual(len(summary["detail"]), 5)
        self.assertEqual(summary["detail"][0]["loc"], ["body", "params", "argv", 0])
        self.reply(422, {"detail": "Invalid: " + "x" * 10000 + self.credential})
        summary = self.detail(await self.request_error())
        self.assertTrue(summary["truncated"])
        self.assertNotIn(self.credential, json.dumps(summary))

    async def test_response_size_limit_still_precedes_error_projection(self) -> None:
        for status in (200, 422):
            with self.subTest(status=status):
                self.status, self.response_body = status, b"x" * (2 * 1024 * 1024 + 1)
                error = await self.request_error()
                self.assertEqual(error.code, "response_too_large")

    async def test_logs_decode_utf8_replace_and_preserve_pagination_and_request(self) -> None:
        response = self.log_response(b"disk usage: 10000\n\xff", b"warning\n")
        original = copy.deepcopy(response)
        payload = {"operation": "jobs.logs", "params": {
            "job_id": "job-test", "stdout_offset": 0, "stderr_offset": 20, "limit": 65536,
        }}
        original_payload = copy.deepcopy(payload)
        result = (await self.logs(response, payload))["result"]
        self.assertEqual(result["stdout"], "disk usage: 10000\n\ufffd")
        self.assertEqual(result["stderr"], "warning\n")
        self.assertEqual(result["encoding"], "utf-8")
        for key in ("next_stdout_offset", "next_stderr_offset", "complete", "truncated"):
            self.assertEqual(result[key], original["result"][key])
        self.assertIn("stdout_offset", result["pagination_hint"])
        self.assertIn("65536", result["pagination_hint"])
        self.assertNotIn("stdout_base64", result)
        self.assertNotIn("stderr_base64", result)
        self.assertEqual(response, original)
        self.assertEqual(payload, original_payload)
        request = self.requests[0]
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        self.assertEqual(request.content, body)
        message = "\n".join(("POST", "/v1/ops/call", "qq:2", "test:scope",
                            request.headers["X-KC-Time"], hashlib.sha256(body).hexdigest())).encode()
        self.assertEqual(request.headers["X-KC-Signature"],
                         hmac.new(self.credential.encode(), message, hashlib.sha256).hexdigest())

    async def test_logs_accept_empty_streams_and_exact_page_budget(self) -> None:
        result = (await self.logs(self.log_response(b"", b"", complete=True, truncated=False)))["result"]
        self.assertEqual((result["stdout"], result["stderr"]), ("", ""))
        self.assertIn("no further page", result["pagination_hint"])
        result = (await self.logs(self.log_response(b"a" * 32768, b"b" * 32768)))["result"]
        self.assertEqual(len(result["stdout"]) + len(result["stderr"]), 65536)
        result = (await self.logs(self.log_response(b"a" * 65536)))["result"]
        self.assertEqual(len(result["stdout"]), 65536)

    async def test_finished_job_full_page_still_requires_pagination(self) -> None:
        payload = {"operation": "jobs.logs", "params": {"job_id": "job-test", "limit": 8}}
        for stdout, stderr in ((b"12345678", b""), (b"", b"12345678"), (b"1234", b"5678")):
            with self.subTest(stdout=stdout, stderr=stderr):
                result = (await self.logs(self.log_response(
                    stdout, stderr, complete=True, truncated=False), payload))["result"]
                self.assertTrue(result["complete"])
                self.assertTrue(result["more_possible"])
                self.assertIn("complete means the job finished", result["pagination_hint"])
        for stdout in (b"last", b""):
            result = (await self.logs(self.log_response(
                stdout, complete=True, truncated=False), payload))["result"]
            self.assertFalse(result["more_possible"])
        result = (await self.logs(self.log_response(
            b"x" * 65536, complete=True, truncated=False)))["result"]
        self.assertTrue(result["more_possible"])

    async def test_logs_reject_invalid_base64_and_never_claim_empty_success(self) -> None:
        for stream in ("stdout", "stderr"):
            for encoded in (None, 123, [], "%%%synthetic-bad-value", "YQ", "YQ==\n", "\ufffd"):
                with self.subTest(stream=stream, encoded=encoded):
                    response = self.log_response()
                    response["result"][stream + "_base64"] = encoded
                    with self.assertRaises(FleetControlError) as caught:
                        await self.logs(response)
                    self.assertEqual(caught.exception.code, "invalid_response")
                    self.assertFalse(caught.exception.retryable)
                    self.assertIn(stream + "_base64", str(caught.exception))
                    self.assertNotIn("synthetic-bad-value", str(caught.exception))
            response = self.log_response()
            del response["result"][stream + "_base64"]
            with self.assertRaises(FleetControlError):
                await self.logs(response)

    async def test_oversize_logs_fail_instead_of_skipping_bytes_at_upstream_offsets(self) -> None:
        for stdout, stderr in ((b"a" * 65537, b""), (b"", b"a" * 65537),
                               (b"a" * 32768, b"b" * 32769), (b"a" * 100000, b"")):
            with self.subTest(lengths=(len(stdout), len(stderr))):
                with self.assertRaises(FleetControlError) as caught:
                    await self.logs(self.log_response(stdout, stderr))
                self.assertEqual(caught.exception.code, "invalid_response")
                self.assertIn("65536 bytes", str(caught.exception))

    async def test_logs_redact_credentials_but_keep_nonsecret_numbers_and_linebreaks(self) -> None:
        stdout = (f"disk: 10000 bytes\n{self.credential}\nBearer synthetic-upstream-token\n"
                  "API_KEY='synthetic multiword secret'\nhttps://user:hidden@upstream.test\n"
                  "-----BEGIN PRIVATE KEY-----\nsynthetic-private-key\n-----END PRIVATE KEY-----\n")
        result = (await self.logs(self.log_response(stdout.encode(), b"synthetic-request-secret"), {
            "operation": "jobs.logs", "params": {"stdout_offset": 0, "token": "synthetic-request-secret"},
        }))["result"]
        self.assertTrue(result["stdout"].startswith("disk: 10000 bytes\n"))
        for value in (self.credential, "synthetic-upstream-token", "synthetic multiword secret",
                      "user:hidden", "synthetic-private-key", "synthetic-request-secret"):
            self.assertNotIn(value, json.dumps(result))
        self.assertIn("[REDACTED]", result["stdout"])

    async def test_log_evidence_is_not_redacted_as_request_secrets(self) -> None:
        evidence = ("1000000 /var 65536 0\nh310 job-test /var/log/app.log\n"
                    "https://h310.test/logs?offset=0&limit=65536\n")
        result = (await self.logs(self.log_response(evidence.encode()), {
            "operation": "jobs.logs", "params": {
                "host": "h310", "job_id": "job-test", "stdout_offset": 0,
                "stderr_offset": 0, "limit": 65536,
            },
        }))["result"]
        self.assertEqual(result["stdout"], evidence)

    async def test_other_operations_failed_envelopes_and_other_paths_remain_unchanged(self) -> None:
        cases = [({**self.log_response(), "operation": "jobs.result"}, {"operation": "jobs.result"}),
                 ({**self.log_response(), "ok": False}, {"operation": "jobs.logs"}),
                 ({**self.log_response(), "operation": "exec.run"}, {"operation": "jobs.logs"}),
                 ({"ok": True, "result": {"encoding": "utf-8", "stdout": "text"}}, {"operation": "jobs.logs"}),
                 ({"ok": True, "result": {}}, {"operation": "jobs.logs"}),
                 ({"ok": True, "result": "value"}, {"operation": "jobs.logs"})]
        for response, payload in cases:
            with self.subTest(response=response, payload=payload):
                self.assertEqual(await self.logs(response, payload), response)
                self.assertIs(project_jobs_logs(response, payload=payload, secrets=()), response)
        response = self.log_response()
        self.reply(200, response)
        self.assertEqual(await self.client._raw_request("POST", "/v1/jobs", {"operation": "jobs.logs"}), response)
        self.assertEqual(await self.client._raw_request("GET", "/v1/ops/call", {"operation": "jobs.logs"}), response)

    async def test_authorization_stays_locked_without_mobile_authorization(self) -> None:
        with self.assertRaises(FleetControlError) as caught:
            await self.client.ops_call({"operation": "jobs.logs"}, actor="qq:2", origin="test:scope")
        self.assertEqual(caught.exception.code, "approval_required")
        self.assertEqual(self.requests, [])

    async def test_projection_precedes_external_storage_and_replay_does_not_resend(self) -> None:
        item = {"task_id": 52, "revision": 1, "run_id": "run-test", "call_id": "call-test",
                "status": "pending", "remote_path": "", "response": None}
        store = Mock()
        store.control.return_value = {"revision": 1}
        store.get.return_value = SimpleNamespace(requester_user_id=2, scope_key="test:scope")
        store.revision_actor.return_value = 2
        store.external_calls.return_value = [item]

        def finish(_, response):
            self.assertNotIn("stdout_base64", json.dumps(response))
            self.assertNotIn("stderr_base64", json.dumps(response))
            item.update(status="resolved", response=response)

        store.finish_external.side_effect = finish
        tracker = ExternalCalls(store, 52, "run-test")
        tracker.call_id, tracker.tool_name = "call-test", "ops_call"
        self.client.authorization = SimpleNamespace(request=AsyncMock(side_effect=self.client._raw_request))
        self.reply(200, self.log_response(b"disk usage: 10000\n"))
        payload = {"operation": "jobs.logs", "params": {"job_id": "job-test"}}
        original_payload = copy.deepcopy(payload)
        context_token = active_external.set(tracker)
        try:
            result = await self.client.ops_call(payload, actor="qq:2", origin="test:scope")
            replay = await self.client.ops_call(payload, actor="qq:2", origin="test:scope")
        finally:
            active_external.reset(context_token)
        self.assertEqual(result["result"]["stdout"], "disk usage: 10000\n")
        self.assertEqual(replay, result)
        self.assertEqual(item["response"], result)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(payload, original_payload)
        recorded = store.begin_external.call_args.args[-1]["body"]
        self.assertEqual(json.loads(self.requests[0].content), recorded)


if __name__ == "__main__":
    unittest.main()
