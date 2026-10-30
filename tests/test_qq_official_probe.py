import asyncio
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
import logging
from pathlib import Path
import tempfile
import unittest

from src.qq_official.credentials import Credentials
from src.qq_official.events import parse_dispatch
from src.qq_official.gateway import GatewayConnection
from src.qq_official.http import RestClient
from src.qq_official.probe import ProbeOptions, ProtectedReport, async_main, run_probe
from src.qq_official.testing.fake_platform import FakePlatform
from src.qq_official.token import TokenManager


GROUP = "GROUP_OPENID_TEST_1"
ACTOR = "MEMBER_OPENID_TEST_1"


def message(number=1, *, mentions=True, timestamp=None):
    return {
        "id": f"MESSAGE_ID_TEST_{number}", "group_openid": GROUP,
        "author": {"member_openid": ACTOR},
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "content": "PRIVATE_TEST_BODY <@BOT_OPENID_TEST_1>",
        "mentions": [{"is_you": True}] if mentions else [],
        "attachments": [{"voice_wav_url": "https://media.example.invalid/wav?signature=TEST_SECRET", "asr_refer_text": "PRIVATE_TEST_ASR"}],
    }


class ImmediateGateway:
    def __init__(self, events=()):
        self.events = events
        self.calls = 0
        self.health = {"state": "closed", "message_gap": False, "close_code": None}

    async def run(self, on_dispatch):
        self.calls += 1
        for event in self.events:
            await on_dispatch(event)

    async def stop(self):
        pass


class QQOfficialProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)

    def options(self, **kwargs):
        return ProbeOptions(report=self.path / "report.json", app_id="TEST_APP_ID", environment="test", **kwargs)

    async def clients(self, fake):
        token = TokenManager(Credentials(fake.app_id, fake.app_secret), base_url=fake.base_url, allow_test_server=True)
        self.addAsyncCleanup(token.aclose)
        rest = RestClient(token, allow_test_server=True)
        self.addAsyncCleanup(rest.aclose)
        return rest, GatewayConnection(rest)

    async def test_listen_default_redacts_and_raw_is_local_identity_only(self):
        event = {"op": 0, "t": "GROUP_MESSAGE_CREATE", "s": 1, "id": "EVENT_ID_TEST_1", "d": message()}
        gateway = ImmediateGateway([event, event])
        output = []
        report = await run_probe(self.options(raw=self.path / "raw.json"), object(), gateway, output=output.append)
        self.assertEqual(report["checks"], [])
        self.assertEqual(gateway.calls, 1)
        serialized = (self.path / "report.json").read_text() + "\n".join(output)
        for sensitive in (GROUP, ACTOR, "TEST_APP_ID", "PRIVATE_TEST_BODY", "PRIVATE_TEST_ASR", "TEST_SECRET", "media.example.invalid/wav"):

            self.assertNotIn(sensitive, serialized)
        self.assertEqual(report["events"][0]["actor"], report["events"][1]["actor"])
        identities = (self.path / "raw.json").read_text()
        self.assertIn(ACTOR, identities)
        self.assertNotIn("PRIVATE_TEST_BODY", identities)
        self.assertNotIn("TEST_SECRET", identities)
        self.assertEqual((self.path / "report.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.path / "raw.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(report["events"][0]["attachment_fields"], ["asr_refer_text", "voice_wav_url"])

    async def test_no_confirmation_does_not_even_connect(self):
        gateway = ImmediateGateway()
        report = await run_probe(self.options(group=GROUP, checks=("reply",)), object(), gateway, confirm=lambda _: False, output=lambda _: None)
        self.assertEqual(report["status"], "cancelled")
        self.assertEqual(gateway.calls, 0)

    def test_targets_and_checks_must_be_explicit_and_immutable(self):
        for kwargs in ({"checks": ("reply",)}, {"group": GROUP, "checks": ("reply", "reply")}, {"group": GROUP, "checks": ("kick",)}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.options(**kwargs).validate()

    def test_report_refuses_existing_or_symlink(self):
        target = self.path / "existing"
        target.write_text("keep")
        with self.assertRaises(FileExistsError):
            ProtectedReport(target)
        link = self.path / "link"
        link.symlink_to(target)
        with self.assertRaises(FileExistsError):
            ProtectedReport(link)
        self.assertEqual(target.read_text(), "keep")

    async def test_fake_platform_e2e_distinct_windows_and_non_at(self):
        async with FakePlatform(nonmention_reply_allowed=True) as fake:
            rest, gateway = await self.clients(fake)
            options = self.options(group=GROUP, checks=("reply", "non_at", "markdown_table", "markdown_code", "markdown_formula"), duration=10)
            task = asyncio.create_task(run_probe(options, rest, gateway, confirm=lambda _: True, output=lambda _: None))
            await asyncio.wait_for(fake.wait_ready(), 3)
            for n in range(1, 6):
                await fake.emit("GROUP_MESSAGE_CREATE", message(n, mentions=n != 2))
            report = await asyncio.wait_for(task, 5)
            self.assertEqual(len(fake.received), 5)
            self.assertEqual([item["payload"]["msg_id"] for item in fake.received], [f"MESSAGE_ID_TEST_{n}" for n in range(1, 6)])
            self.assertTrue(all(item["payload"]["msg_seq"] == 1 for item in fake.received))
            self.assertEqual([item["status"] for item in report["checks"]], ["accepted"] * 5)
            self.assertTrue(all(item["rendering"] == "manual_verification_required" for item in report["checks"][2:]))

    async def test_stale_replayed_message_never_gets_new_window(self):
        event = {"op": 0, "t": "GROUP_AT_MESSAGE_CREATE", "s": 1, "d": message(timestamp="2020-01-01T00:00:00Z")}
        gateway = ImmediateGateway([event])
        report = await run_probe(self.options(group=GROUP, checks=("reply",)), object(), gateway, confirm=lambda _: True, output=lambda _: None)
        self.assertEqual(report["checks"][0]["status"], "not_run")
        self.assertEqual(report["checks"][0]["reason"], "no_fresh_eligible_window")

    async def test_accepted_response_lost_is_ambiguous_and_never_retried(self):
        async with FakePlatform() as fake:
            rest, gateway = await self.clients(fake)
            fake.proactive_allowed[("group", GROUP)] = True
            fake.drop_next_response = True
            report = await run_probe(self.options(group=GROUP, checks=("proactive", "reply")), rest, gateway, confirm=lambda _: True, output=lambda _: None)
            self.assertEqual(len(fake.received), 1)
            self.assertEqual(report["checks"][0]["status"], "ambiguous")
            self.assertEqual(len(report["checks"]), 2)
            self.assertEqual(report["checks"][1]["reason"], "previous_send_ambiguous")

    async def test_probe_async_cli_runs_only_fake_target_and_suppresses_http_logs(self):
        async with FakePlatform() as fake:
            fake.proactive_allowed[("group", GROUP)] = True
            for name, value in (("appid", fake.app_id), ("secret", fake.app_secret)):
                path = self.path / name
                path.write_text(value)
                path.chmod(0o600)
            output = io.StringIO()
            logger = logging.getLogger("httpx")
            logger.setLevel(logging.INFO)
            logger.addHandler(logging.StreamHandler(output))
            with redirect_stdout(output):
                result = await async_main([
                    "--app-id-file", str(self.path / "appid"), "--app-secret-file", str(self.path / "secret"),
                    "--group", GROUP, "--checks", "proactive", "--confirm-batch",
                    "--report", str(self.path / "cli-report.json"), "--test-api-base", fake.base_url,
                ])
            self.assertEqual(result, 0)
            self.assertEqual(len(fake.received), 1)
            self.assertNotIn(GROUP, output.getvalue())
            self.assertNotIn(fake.app_secret, output.getvalue())
            self.assertNotIn(fake.access_token, output.getvalue())
            self.assertEqual(json.loads((self.path / "cli-report.json").read_text())["environment"], "test")

    async def test_invalid_event_summaries_are_bounded(self):
        gateway = ImmediateGateway([{"op": 0, "t": "BAD", "d": "SECRET_TEST"}] * 1100)
        report = await run_probe(self.options(), object(), gateway, output=lambda _: None)
        self.assertEqual(len(report["events"]), 1000)
        self.assertEqual(report["dropped_events"], 100)
        self.assertNotIn("SECRET_TEST", json.dumps(report))

    async def test_gateway_failure_diagnostics_survive_stop(self):
        class FailingGateway(ImmediateGateway):
            async def run(self, on_dispatch):
                self.health = {"state": "closed", "reason": "protocol_shard_or_intent_configuration", "close_code": 4014, "message_gap": True}

            async def stop(self):
                self.health = {"state": "closed", "reason": "stopped"}

        report = await run_probe(self.options(), object(), FailingGateway(), output=lambda _: None)
        self.assertEqual(report["status"], "connection_failed")
        self.assertEqual(report["gateway"]["close_code"], 4014)
        self.assertTrue(report["gateway"]["message_gap"])
        self.assertEqual(report["gateway"]["reason"], "protocol_shard_or_intent_configuration")

    async def test_sanitized_samples_roundtrip_preserve_reference_evidence(self):
        original = json.loads((Path(__file__).parent / "fixtures" / "qq_official" / "group_message.json").read_text())
        original["d"]["unknown_secret_field"] = "UNKNOWN_SECRET_TEST"
        original["d"]["msg_elements"][0]["attachments"][0]["content_type"] = "PRIVATE_TEST_MIME"
        report = await run_probe(self.options(), object(), ImmediateGateway([original]), output=lambda _: None)
        sample = report["samples"][0]
        parsed = parse_dispatch(sample)
        self.assertEqual(parsed.event_type, "GROUP_MESSAGE_CREATE")
        self.assertEqual(parsed.seq, 42)
        self.assertTrue(parsed.message.mentions_bot)
        self.assertEqual(parsed.message.ref_msg_idx, parsed.message.elements[0].msg_idx)
        self.assertIsNotNone(parsed.message.elements[0].author)
        self.assertIsNotNone(parsed.message.elements[0].elements[0].author)
        self.assertEqual(parsed.message.elements[0].attachments[0].content_type, "application/octet-stream")
        serialized = json.dumps(report)
        for forbidden in ("UNKNOWN_SECRET_TEST", "PRIVATE_TEST_MIME", "signature=TEST_ONLY", "MEMBER_OPENID_TEST", "GROUP_OPENID_TEST", "被引用的测试原文", "测试识别文本"):
            self.assertNotIn(forbidden, serialized)


    async def test_c2c_window_limit_uses_user_endpoint_and_four_reply_fixture(self):
        async with FakePlatform() as fake:
            rest, gateway = await self.clients(fake)
            user = "USER_OPENID_TEST_1"
            task = asyncio.create_task(run_probe(self.options(user=user, checks=("window_limit",)), rest, gateway, confirm=lambda _: True, output=lambda _: None))
            await asyncio.wait_for(fake.wait_ready(), 3)
            await fake.emit("C2C_MESSAGE_CREATE", {
                "id": "C2C_MESSAGE_ID_TEST_1", "author": {"user_openid": user},
                "timestamp": datetime.now(timezone.utc).isoformat(), "content": "PRIVATE_TEST_C2C",
            })
            report = await asyncio.wait_for(task, 5)
            self.assertEqual(len(fake.received), 4)
            self.assertTrue(all(item["kind"] == "user" and item["target"] == user for item in fake.received))
            self.assertEqual([item["payload"]["msg_seq"] for item in fake.received], [1, 2, 3, 4])
            self.assertEqual(report["checks"][-1]["code"], 40034128)
            self.assertEqual(report["checks"][-1]["status"], "rejected")
            self.assertNotIn(user, json.dumps(report))

    async def test_window_limit_rechecks_expiry_between_http_requests(self):
        now = [1000.0]
        async with FakePlatform(clock=lambda: now[0]) as fake:
            rest, gateway = await self.clients(fake)

            class SlowSend:
                async def send_message(self, kind, target, payload):
                    response = await rest.send_message(kind, target, payload)
                    now[0] += 300
                    return response

            task = asyncio.create_task(run_probe(self.options(group=GROUP, checks=("window_limit",)), SlowSend(), gateway, clock=lambda: now[0], confirm=lambda _: True, output=lambda _: None))
            await asyncio.wait_for(fake.wait_ready(), 3)
            await fake.emit("GROUP_AT_MESSAGE_CREATE", message(timestamp=datetime.fromtimestamp(1000, timezone.utc).isoformat()))
            report = await asyncio.wait_for(task, 5)
            self.assertEqual(len(fake.received), 1)
            self.assertEqual(report["checks"][1]["status"], "not_run")
            self.assertEqual(report["checks"][1]["reason"], "reply_window_expired_before_send")
