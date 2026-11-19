"""Tests for promise tool, systemd wake timers, and turn_context injection."""

from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from src.plugins.ai_chat.application.turn_context import turn_window
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey, InboundMessage
from src.plugins.ai_chat.self_files import _CONTAINER_SYNC_SCRIPT, SelfFilesStore
from src.plugins.ai_chat.tools.promises import build_promise_units, parse_when, promise
from src.plugins.ai_chat.tools.state import ToolContext, TurnState


class _FakeSandboxManager:
    def __init__(self):
        self.execs = []

    async def ensure_default(self, owner):
        return {"id": "s123456"}

    async def list_owned(self, owner):
        return [{"id": "s123456", "purpose": "conversation"}]

    async def _running(self, sid):
        return True

    async def start(self, owner, sid):
        pass

    async def _guest_exec_raw(self, sid, path, argv, **kwargs):
        self.execs.append((sid, path, argv, kwargs))
        return 0, b"", b""


class _FakeLedger:
    def history(self, scope, *, limit=120, newest=True, **kwargs):
        return []

    def canonical_id_for_native(self, *args, **kwargs):
        return None


class _FakeConversations:
    def tail_after(self, key):
        return 0

    def set_tail_after(self, key, val):
        pass

    def memory(self, key):
        return ""


class PromiseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SelfFilesStore(self.tmp.name)
        acc = AccountKey("qq-official", "TEST_APP", "sandbox")
        self.ep = EndpointKey(acc, "group", "TEST_G1")
        self.actor = ActorKey(acc, "group_member", "TEST_G1", "TEST_U1")

    def tearDown(self):
        self.tmp.cleanup()

    def test_git_sync_shares_a_deadline_and_does_not_commit_after_an_unknown_diff(self):
        (Path(self.tmp.name) / '.git').mkdir()
        script = _CONTAINER_SYNC_SCRIPT.replace('/work/.gaoji/self', self.tmp.name)
        for failure, message in ((SimpleNamespace(returncode=128), 'git diff failed'),
                                 (subprocess.TimeoutExpired('git', 5), 'git diff timed out')):
            with self.subTest(message=message), patch('sys.stdin', io.StringIO('{}')), \
                    patch('time.monotonic', side_effect=(100, 102, 120)), \
                    patch('subprocess.run', side_effect=(SimpleNamespace(returncode=0), failure)) as run:
                with self.assertRaisesRegex(SystemExit, message):
                    exec(script, {})
                self.assertEqual([(c.args[0][3], c.kwargs['timeout']) for c in run.call_args_list], [('add', 23), ('diff', 5)])

    def test_parse_when_and_units(self):
        ts1, iso1 = parse_when("10m", now=1000)
        self.assertEqual(ts1, 1600)
        self.assertIn("+08:00", iso1)

        ts2, _ = parse_when("2h", now=1000)
        self.assertEqual(ts2, 8200)

        ts3, _ = parse_when("2099-01-02 09:30", now=1000)
        self.assertGreater(ts3, 1000)

        from src.plugins.ai_chat.self_files import PromiseItem
        item = PromiseItem("p123456", ts1, iso1, "Alice", "看看日志", unit_name="gaoji-promise-p123456")
        timer_txt, svc_txt = build_promise_units(item)
        self.assertIn("OnCalendar=", timer_txt)
        self.assertIn("UTC", timer_txt)
        self.assertIn("gaoji wake", svc_txt)
        self.assertIn("回访", svc_txt)

    async def test_promise_lifecycle_create_list_done_and_turn_window(self):
        sb = _FakeSandboxManager()
        app = SimpleNamespace(
            self_files=self.store,
            sandbox_manager=sb,
            message_ledger=_FakeLedger(),
            conversations=_FakeConversations(),
        )
        inbound = InboundMessage(
            endpoint=self.ep,
            actor=self.actor,
            event_id="e1",
            native_message_id="m1",
            sender_display="Alice",
            platform_role="",
            body=MessageBody((TextNode(0, "明天帮我看下部署日志"),)),
            mentions_bot=True,
            reply_to_native_message_id=None,
            occurred_at=1000,
            reply_credential=None,
        )
        ctx = ToolContext(
            services=SimpleNamespace(context=app),
            inbound=inbound,
            platform=SimpleNamespace(account=self.ep.account),
            capabilities=frozenset({"memory"}),
        )
        turn = TurnState(context=ctx, model_profile=SimpleNamespace(name="test"), user_text="明天帮我看下部署日志")

        created = json.loads(await promise(turn, {"action": "create", "what": "看部署日志", "when": "30m"}))
        self.assertTrue(created["ok"])
        self.assertTrue(created["timer_installed"])
        pid = created["id"]
        self.assertEqual(len(sb.execs), 2)

        # turn_window surfaces the open promise because it is due within 24h
        _, _, extra = turn_window(app, inbound, SimpleNamespace(max_input_tokens=128_000))
        self.assertIn("[未兑现的承诺]", extra)
        self.assertIn("看部署日志", extra)

        listed = json.loads(await promise(turn, {"action": "list"}))
        self.assertEqual(len(listed["promises"]), 1)
        self.assertFalse(listed["promises"][0]["done"])

        done = json.loads(await promise(turn, {"action": "done", "id": pid}))
        self.assertTrue(done["done"])
        _, _, extra_after = turn_window(app, inbound, SimpleNamespace(max_input_tokens=128_000))
        self.assertNotIn("看部署日志", extra_after)

        # Graceful degradation when sandbox is disabled/broken
        sb._guest_exec_raw = AsyncMock(side_effect=RuntimeError("no sandbox"))
        degraded = json.loads(await promise(turn, {"action": "create", "what": "查问题", "when": "1h"}))
        self.assertTrue(degraded["ok"])
        self.assertFalse(degraded["timer_installed"])


if __name__ == "__main__":
    unittest.main()
