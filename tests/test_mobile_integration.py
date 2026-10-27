from __future__ import annotations

from tests.postgres_fixture import postgres_for_test

import copy
import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import nonebot

nonebot.init()

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, PrivateMessageEvent
from src.bot_security.service import MobileAuthorization, assert_approved
from src.bot_security.store import SecurityError, SecurityStore
from src.plugins.ai_chat.fleet_authorization import FleetAuthorization
from src.plugins.ai_chat.command_handlers import CommandHandlers
from src.plugins.ai_chat.mobile_authorization import handle_approval_event, redact_approval_event_logs
from src.plugins.ai_chat.qq_action_authorization import mobile_command, register_qq_executors, requires_mobile_tool
from tests.test_account_security import PASSWORD, QQ, BOT


def event(text, *, group=False):
    raw = dict(time=100, self_id=int(BOT), post_type="message", message_type="group" if group else "private",
        sub_type="normal" if group else "friend", message_id=42, user_id=int(QQ),
        message=[{"type": "text", "data": {"text": text}}], raw_message=text, font=0,
        sender={"user_id": int(QQ), "nickname": "test"})
    if group:
        raw["group_id"] = 1234
    return (GroupMessageEvent if group else PrivateMessageEvent).model_validate(raw)


class MobileIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = SecurityStore(postgres_for_test(self), b"x" * 32)
        self.addCleanup(self.store.close)
        self.account = self.store.bootstrap("kenneth", PASSWORD, QQ)
        self.sent = []
        async def sender(bot, qq, text):
            self.sent.append((bot, qq, text))
        self.mobile = MobileAuthorization(self.store, sender=sender, bot_selector=lambda: BOT)

    async def confirm_latest(self):
        text = next(text for _, _, text in reversed(self.sent) if "一次性口令：" in text)
        identifier = re.search(r"AP-[A-F0-9]+", text)[0]
        code = re.search(r"一次性口令：([0-9]{6})", text)[1]
        reply = await self.mobile.handle_message(qq_id=QQ, bot_id=BOT, private=True, text=f"确认 {identifier} {code}")
        self.assertIn("已确认", reply)
        return identifier

    async def test_admin_qq_command_executes_without_phone(self):
        bot = SimpleNamespace(send=AsyncMock())
        executed = []
        registry = SimpleNamespace(list_for= lambda _: [SimpleNamespace(task_id="task-original")])
        context = SimpleNamespace(mobile_authorization=self.mobile, running_tasks=registry)
        class Commands:
            def __init__(self):
                self.context = context
                self.services = SimpleNamespace(chat=SimpleNamespace(_conversation_id=lambda _: "private"))
            @mobile_command
            async def handle_task_stop(self, event, args):
                executed.append(registry.list_for(None)[0].task_id)
        commands = Commands()
        services = SimpleNamespace(context=context, commands=commands)
        register_qq_executors(services)
        with patch("src.plugins.ai_chat.qq_action_authorization.get_bots", return_value={BOT: bot}):
            await commands.handle_task_stop(event("/停止"), Message(""))
            self.assertEqual(executed, ["task-original"])
            self.assertFalse(self.sent)
            registry.list_for = lambda _: [SimpleNamespace(task_id="task-new")]
            await commands.handle_task_stop(event("/停止"), Message(""))
            self.assertEqual(executed, ["task-original", "task-new"])
            self.assertFalse(await self.mobile.run_once())

    async def test_private_code_interception_and_log_redaction(self):
        redact_approval_event_logs()
        text = "确认 AP-123456789ABC 012345"
        bot = SimpleNamespace(self_id=BOT, send_private_msg=AsyncMock(), send_group_msg=AsyncMock())
        for group in (False, True):
            incoming = event(text, group=group)
            self.assertNotIn("012345", incoming.get_log_string())
            self.assertTrue(await handle_approval_event(self.mobile, bot, incoming))
            self.assertNotIn("012345", str(bot.send_private_msg.call_args))
            self.assertNotIn("012345", str(bot.send_group_msg.call_args))
        self.assertFalse(await handle_approval_event(self.mobile, bot, event("机器人状态")))
        incoming = event("012345")
        self.assertNotIn("012345", incoming.get_log_string())
        self.assertTrue(await handle_approval_event(self.mobile, bot, incoming))
        self.assertNotIn("012345", str(bot.send_private_msg.call_args))
        self.assertFalse(await handle_approval_event(self.mobile, bot, event("012345", group=True)))

    async def test_tools_delegate_server_authorization_to_fleet_boundary(self):
        for name in ("memory_add", "job_cancel", "browser_click", "browser_fill", "unknown_tool", "sandbox_host_exec", "cluster_job_submit"):
            self.assertFalse(requires_mobile_tool(name), name)
        for name in ("web_search", "service_inspect", "say", "sandbox_create", "sandbox_exec",
                     "sandbox_write_file", "sandbox_read_file", "sandbox_destroy", "import_file_to_sandbox",
                     "import_agent_artifact", "send_file_from_sandbox", "send_image_from_sandbox"):
            self.assertFalse(requires_mobile_tool(name), name)

    async def test_shell_command_executes_without_qq_challenge(self):
        executed = []
        class Commands:
            @mobile_command
            async def handle_shell_command(self, event, args):
                executed.append(args.extract_plain_text())
        commands = Commands()
        commands.context = SimpleNamespace(mobile_authorization=self.mobile)
        for text in ("python --version", "reset", "destroy"):
            await commands.handle_shell_command(event("/shell " + text), Message(text))
        self.assertEqual(executed, ["python --version", "reset", "destroy"])
        self.assertFalse(self.sent)

    async def test_shell_reset_deletes_only_current_shell_and_keeps_access_checks(self):
        manager = SimpleNamespace(
            list=AsyncMock(return_value=[
                {"sandbox_id": "s123abc", "purpose": "shell"},
                {"sandbox_id": "s456def", "purpose": "task"},
            ]),
            destroy=AsyncMock(),
        )
        allowed = Mock(return_value=True)
        context = SimpleNamespace(
            sandbox_manager=manager, mobile_authorization=self.mobile, logger=Mock(),
            settings=SimpleNamespace(is_sandbox_user_allowed=allowed, admin_user_ids={int(QQ)}),
        )
        replies = SimpleNamespace(_finish_safely=AsyncMock(), _reply_message=lambda _, text: text)
        commands = CommandHandlers(SimpleNamespace(context=context, replies=replies))
        for is_group in (True, False):
            incoming = event("/shell reset", group=is_group)
            await commands.handle_shell_command(incoming, Message("reset"))
            owner = "shell:group:1234" if is_group else "shell:private:" + QQ
            manager.list.assert_awaited_once_with(owner)
            manager.destroy.assert_awaited_once_with(owner, "s123abc")
            manager.list.reset_mock()
            manager.destroy.reset_mock()

        allowed.return_value = False
        await commands.handle_shell_command(event("/shell reset", group=True), Message("reset"))
        manager.list.assert_not_awaited()
        manager.destroy.assert_not_awaited()
        self.assertFalse(self.sent)

    async def test_shell_destructive_aliases_require_admin_even_when_allowed(self):
        manager = SimpleNamespace(list=AsyncMock(), destroy=AsyncMock())
        context = SimpleNamespace(
            sandbox_manager=manager, mobile_authorization=self.mobile, logger=Mock(),
            settings=SimpleNamespace(is_sandbox_user_allowed=lambda _: True, admin_user_ids=set()),
        )
        replies = SimpleNamespace(_finish_safely=AsyncMock(), _reply_message=lambda _, text: text)
        commands = CommandHandlers(SimpleNamespace(context=context, replies=replies))
        for is_group in (True, False):
            for action in ("reset", "RESET", "destroy", "重建", "销毁"):
                await commands.handle_shell_command(event("/shell " + action, group=is_group), Message(action))
                self.assertIn("只有管理员", replies._finish_safely.await_args.args[1])
        manager.list.assert_not_awaited()
        manager.destroy.assert_not_awaited()
        self.assertFalse(self.sent)

    async def test_deployment_preflight_sends_private_code_and_polls_final_result(self):
        record = {"deployment_id": "deploy_test", "status": "preflight_queued", "actor_id": "admin:kenneth",
                  "contract_hash": "before", "resource_version": 1, "target_hosts": ["test-host"]}
        writes = []
        async def raw(method, path, body=None, **kwargs):
            if path == "/v1/operations?limit=200":
                return {"items": []}
            if path.endswith("/prepare"):
                self.assertEqual(kwargs["actor"], "admin:kenneth")
                return copy.deepcopy(record)
            if method == "POST":
                writes.append((path, body))
                self.assertEqual(record["status"], "awaiting_approval")
                record["status"] = "queued"
            return copy.deepcopy(record)
        fleet = FleetAuthorization(SimpleNamespace(_raw_request=raw), self.mobile)
        await fleet.request("POST", "/v1/deployments/prepare", {}, actor="qq:" + QQ, origin="test")
        self.assertEqual(self.sent, [])
        record.update(status="awaiting_approval", contract_hash="resolved-commit", resource_version=2)
        await fleet.poll()
        self.assertIn("resolved-commit", self.sent[-1][2])
        self.assertEqual(writes, [])
        self.store.finish_fleet_watch("/v1/deployments/deploy_test")
        with self.store.transaction() as db:
            db.execute("UPDATE admin_fleet_watches SET lease_until=0")
        await fleet.poll()
        self.assertEqual(len(self.sent), 1)
        await self.confirm_latest()
        await self.mobile.run_once()
        self.assertEqual(len(writes), 1)
        self.assertIn("等待服务器", self.sent[-1][2])
        record["status"] = "succeeded"
        with self.store.transaction() as db:
            db.execute("UPDATE admin_fleet_watches SET lease_until=0")
        await fleet.poll()
        self.assertIn("服务器执行结果", self.sent[-1][2])
        self.assertEqual(len(writes), 1)

    async def test_changed_remote_contract_cannot_execute_and_member_cannot_propose(self):
        record = {"operation_id": "op_test", "status": "awaiting_approval", "contract_hash": "original",
                  "resource_version": 1, "arguments": {"unit": "example.service"}}
        async def raw(method, path, body=None, **kwargs):
            self.assertEqual(method, "GET")
            return copy.deepcopy(record)
        fleet = FleetAuthorization(SimpleNamespace(_raw_request=raw), self.mobile)
        with self.assertRaises(SecurityError):
            await fleet.request("POST", "/v1/operations/prepare", {}, actor="qq:222222222", origin="test")
        await fleet.propose_record(record, self.account, "admin:kenneth", "test")
        identifier = await self.confirm_latest()
        record["arguments"]["unit"] = "different.service"
        await self.mobile.run_once()
        self.assertEqual(self.store.get(identifier, self.account["account_id"])["status"], "failed")

    async def test_isolated_worker_jobs_are_direct_and_still_track_all_hosts(self):
        records, writes = {}, []

        async def raw(method, path, body=None, **kwargs):
            if path == "/v1/operations?limit=200":
                return {"items": []}
            self.assertEqual(kwargs["actor"], "admin:kenneth")
            self.assertEqual(kwargs["origin"], "test")
            if method == "POST":
                self.assertEqual(path, "/v1/jobs")
                worker = body["constraints"]["worker_id"]
                row = {"job_id": "job_" + worker, "status": "queued", "worker_id": worker}
                records["/v1/jobs/" + row["job_id"]] = row
                writes.append(copy.deepcopy(body))
                return copy.deepcopy(row)
            return copy.deepcopy(records[path])

        fleet = FleetAuthorization(SimpleNamespace(_raw_request=raw), self.mobile)
        for index, (host, final_status) in enumerate((("h310", "succeeded"), ("h610", "failed"), ("tank", "cancelled"))):
            worker = host + "-worker"
            result = await fleet.request("POST", "/v1/jobs", {"kind": "probe.http", "constraints": {"worker_id": worker}},
                actor="qq:" + QQ, origin="test")
            self.assertEqual(result["status"], "queued")
            self.assertFalse(any("一次性口令：" in text for _, _, text in self.sent))
            self.assertEqual(len(writes), index + 1)
            path = "/v1/jobs/job_" + worker
            with self.store.transaction() as db:
                watch = db.execute("SELECT * FROM admin_fleet_watches WHERE path=?", (path,)).fetchone()
            self.assertIsNotNone(watch, worker)
            self.assertEqual(watch["actor"], "admin:kenneth")
            self.assertEqual(watch["origin"], "test")
            before = len(self.sent)
            await fleet.poll()
            self.assertEqual(len(self.sent), before)
            records[path].update(status=final_status, error_code="test_error" if final_status == "failed" else "")
            records[path]["result"] = {"public_url": "http://192.0.2.3/previews/test/"} if final_status == "succeeded" else {}
            with self.store.transaction() as db:
                db.execute("UPDATE admin_fleet_watches SET lease_until=0 WHERE path=?", (path,))
            await fleet.poll()
            self.assertEqual(self.sent[-1][:2], (BOT, QQ))
            self.assertIn(worker, self.sent[-1][2])
            self.assertIn(final_status, self.sent[-1][2])
            if final_status == "failed":
                self.assertIn("test_error", self.sent[-1][2])
            if final_status == "succeeded":
                self.assertIn(records[path]["result"]["public_url"], self.sent[-1][2])
            self.assertFalse(await self.mobile.run_once())
            await fleet.poll()
            self.assertEqual(len(self.sent), before + 1)
        self.assertEqual(len(writes), 3)

    async def test_worker_tool_approval_tracks_result_and_retries_notice_without_resubmit(self):
        writes = []
        record = {"job_id": "job_tool", "worker_id": "tank-worker", "status": "queued"}

        async def raw(method, path, body=None, **kwargs):
            if path == "/v1/operations?limit=200":
                return {"items": []}
            if method == "POST":
                writes.append(path)
            return copy.deepcopy(record)

        fleet = FleetAuthorization(SimpleNamespace(_raw_request=raw), self.mobile)
        payload = {"tool": "cluster_job_submit", "arguments": {"worker_id": "tank-worker"}}

        async def approved_tool(request):
            assert_approved("tool", request["payload"])
            return await fleet.request("POST", "/v1/jobs", {"constraints": payload["arguments"]}, actor="qq:" + QQ, origin="test")

        self.mobile.executors["tool"] = approved_tool
        await self.mobile.propose(self.account, kind="tool", payload=payload, summary="Run a test worker job")
        await self.confirm_latest()
        self.assertTrue(await self.mobile.run_once())
        self.assertEqual(writes, ["/v1/jobs"])
        self.assertIn("等待服务器", self.sent[-1][2])
        record.update(status="succeeded", result={"status_code": 200})
        sender = self.mobile.sender
        self.store.close()
        reopened = SecurityStore(postgres_for_test(self), b"x" * 32)
        self.addCleanup(reopened.close)
        restarted = MobileAuthorization(reopened, sender=AsyncMock(side_effect=RuntimeError("QQ unavailable")), bot_selector=lambda: BOT)
        recovered = FleetAuthorization(SimpleNamespace(_raw_request=raw), restarted)
        await recovered.poll()
        with reopened.transaction() as db:
            watch = db.execute("SELECT status FROM admin_fleet_watches WHERE path='/v1/jobs/job_tool'").fetchone()
            self.assertEqual(watch["status"], "watching")
            db.execute("UPDATE admin_fleet_watches SET lease_until=0")
        restarted.sender = sender
        await recovered.poll()
        self.assertIn("tank-worker", self.sent[-1][2])
        self.assertEqual(writes, ["/v1/jobs"])
        count = len(self.sent)
        await recovered.poll()
        self.assertEqual(len(self.sent), count)


if __name__ == "__main__":
    unittest.main()
