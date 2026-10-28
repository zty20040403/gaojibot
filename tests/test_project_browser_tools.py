"""Registry -> real PostgreSQL -> Unix RPC; QQ, Docker and page effects are fixtures."""

import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import nonebot

nonebot.init()

from src.plugins.ai_chat.project_browser_store import action_digest
from src.plugins.ai_chat.project_tool_executor import _identity
from src.plugins.ai_chat.tool_invocation import active_tool_call_id
from src.plugins.ai_chat.tools import domain_registry
from src.plugins.ai_chat.tools.state import ToolContext, TurnState
from tests.project_turn_fixture import start_project_turn
from tests.test_project_browser_controller import BrowserControllerFixture
from tests import test_project_lifecycle as lifecycle_fixture
from tests.test_routine_tools import event


class ProjectBrowserToolTests(BrowserControllerFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.io.abort(self.grant)
        self.runtime.ready, self.runtime.closed = True, False
        self.app = self.runtime.app
        self.app.project_runtime = self.runtime
        self.app.message_ledger = self.ledger
        self.app.settings = SimpleNamespace(projects_enabled=True, browser_enabled=True, sandbox_backend="oci",
            project_instance_id=self.manager.namespace, is_sandbox_user_allowed=lambda user: True)

        async def db_call(method, *args, **kwargs):
            return await asyncio.to_thread(method, *args, **kwargs)
        self.app.db_call = db_call
        self.identities = {self.principal: self.access, self.other: self.b}

        async def resolve(row, principal):
            access = self.identities[principal]
            if (access.scope.key != row["scope_key"]
                    or access.scope.bot_native_user_id != row["bot_native_user_id"]):
                raise PermissionError("fixture live identity mismatch")
            return access
        self.runtime.resolve_access = resolve

        async def sample(project):
            self.record(self.sample(), pid=project["project_id"])
        self.runtime.sampler = SimpleNamespace(sample_one=AsyncMock(side_effect=sample))
        self.legacy = SimpleNamespace(browser_manager=object(), sandbox_manager=self.client,
                                     execute=AsyncMock(return_value='{"ok":true,"legacy":true}'))
        self.context = ToolContext(SimpleNamespace(context=self.app),
            event().model_copy(update={"message_id": self.principal}), frozenset({"projects", "browser"}),
            self.legacy, journal_turn_id=self.turns[self.principal].turn_id)
        for method in ("navigate", "snapshot", "type_text", "press_key", "scroll", "wait_for"):
            setattr(self.engine, method, AsyncMock(return_value={"text": method}))
        self.engine.close_session = AsyncMock(return_value=True)
        self.engine.clear_profile = AsyncMock(return_value=True)
        self.counter = 0

    async def tool(self, name="browser_click", args=None, *, call_id=None, context=None):
        self.counter += 1
        token = active_tool_call_id.set(call_id or f"tool-{self.counter}")
        try:
            return json.loads(await domain_registry.execute(
                TurnState(context or self.context), name, {"ref": "b1"} if args is None else args))
        finally:
            active_tool_call_id.reset(token)

    def calls(self):
        return self.sql.execute("SELECT * FROM project_browser_calls ORDER BY operation_id,sequence").fetchall()

    async def test_all_nine_tools_share_project_session_then_close_and_clear_drain(self):
        operations = [
            ("browser_navigate", {"url": "https://example.test"}, "navigate"),
            ("browser_snapshot", {}, "snapshot"),
            ("browser_click", {"ref": "b1"}, "clicked"),
            ("browser_type", {"ref": "b1", "text": "private input", "submit": False}, "type_text"),
            ("browser_press_key", {"key": "Enter"}, "press_key"),
            ("browser_scroll", {"amount": 400}, "scroll"),
            ("browser_wait_for", {"text": "ready"}, "wait_for"),
        ]
        for name, args, expected in operations:
            result = await self.tool(name, args)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["page"], {"text": expected})
            self.assertEqual(result["project_handle"], f"prj#{self.pid}")
            self.assertNotIn("token", result)
            self.assertNotIn("session_id", result)
        self.assertEqual(len({row["operation_id"] for row in self.calls()}), 1)
        self.assertEqual([row["sequence"] for row in self.calls()], list(range(1, 8)))
        self.browser._spawn.assert_awaited_once()
        closed = await self.tool("browser_close", {})
        self.assertTrue(closed["closed"], closed)
        self.assertIsNone(self.io.status(self.access, self.pid))
        self.assertTrue(self.parent["State"]["Running"])
        cleared = await self.tool("browser_clear", {})
        self.assertTrue(cleared["cleared"], cleared)
        self.assertNotEqual(closed["operation_id"], cleared["operation_id"])
        self.assertEqual(len(self.docker.containers), 1)
        self.assertIsNone(self.io.status(self.access, self.pid))
        self.legacy.execute.assert_not_awaited()
        self.assertNotIn("private input", str([dict(row) for row in self.calls()]))

    async def test_closed_result_replay_after_restart_and_focus_switch_is_not_new_click(self):
        first = await self.tool(call_id="stable")
        await self.tool("browser_close", {})
        self.projects.create(self.access, "Another project", quota_bytes=64 * 1024**2)
        self.restart_application()
        self.assertEqual(await self.tool(call_id="stable"), first)
        self.engine.click.assert_awaited_once()
        changed = await self.tool(args={"ref": "b2"}, call_id="stable")
        self.assertEqual(changed["code"], "project_conflict")
        self.legacy.execute.assert_not_awaited()

    async def test_interrupted_unbound_admission_recovers_frozen_project_not_new_focus(self):
        with patch.object(self.controller.store, "bind", side_effect=OSError("fixture interrupted bind")):
            first = await self.tool(call_id="first")
        self.assertEqual(first["code"], "project_browser_unconfirmed")
        self.projects.create(self.access, "Changed focus", quota_bytes=64 * 1024**2)
        self.restart_application()
        replay = await self.tool(call_id="first")
        self.assertTrue(replay["ok"], replay)
        self.assertEqual(replay["project_handle"], f"prj#{self.pid}")
        self.engine.click.assert_awaited_once_with(self.scope.key, ref="b1")
        self.legacy.execute.assert_not_awaited()

    async def test_lost_action_response_recovers_after_app_and_runner_restart(self):
        original = self.client.project_browser
        async def lose(operation, **args):
            result = await original(operation, **args)
            if operation == "execute":
                raise OSError("fixture lost transport")
            return result
        with patch.object(self.client, "project_browser", side_effect=lose):
            first = await self.tool(call_id="lost")
        self.assertEqual(first["code"], "project_browser_unconfirmed")
        self.assertEqual(self.calls()[0]["state"], "dispatched")
        await self.close_runner()
        await self.start_runner()
        self.restart_application()
        recovered = await self.tool(call_id="lost")
        self.assertTrue(recovered["ok"], recovered)
        self.assertEqual(recovered["page"], {"text": "clicked"})
        self.assertEqual(self.calls()[0]["state"], "acknowledged")
        self.browser._spawn.assert_not_awaited()
        self.engine.click.assert_awaited_once()

    async def test_unknown_effect_blocks_replay_and_new_call_without_legacy_fallback(self):
        self.engine.click.side_effect = TimeoutError("fixture uncertain click")
        first = await self.tool(call_id="unknown")
        again = await self.tool(call_id="unknown")
        another = await self.tool(call_id="new")
        self.assertTrue(all(row["code"] == "project_conflict" for row in (first, again, another)))
        self.engine.click.assert_awaited_once()
        self.assertEqual(self.calls()[0]["state"], "needs_attention")
        self.legacy.execute.assert_not_awaited()

    async def test_another_turn_cannot_reuse_or_close_running_browser(self):
        self.assertTrue((await self.tool())["ok"])
        turn = start_project_turn(self.ledger, self.journal, self.scope, message_id=919)
        context = replace(self.context, journal_turn_id=turn.turn_id,
                          event=self.context.event.model_copy(update={"message_id": 919}))
        result = await self.tool("browser_close", {}, context=context)
        self.assertEqual(result["code"], "project_conflict")
        self.engine.close_session.assert_not_awaited()
        self.assertEqual(len(self.calls()), 1)

    async def test_viewer_and_disallowed_editor_cannot_read_project_browser(self):
        self.projects.open(self.b, self.pid)
        context = replace(self.context,
            event=event(actor=202).model_copy(update={"message_id": self.other}),
            journal_turn_id=self.turns[self.other].turn_id)
        denied = await self.tool("browser_snapshot", {}, context=context)
        self.assertEqual(denied["code"], "project_forbidden")
        self.app.settings.is_sandbox_user_allowed = lambda user: False
        denied = await self.tool("browser_snapshot", {})
        self.assertEqual(denied["code"], "project_forbidden")
        self.browser._spawn.assert_not_awaited()
        self.legacy.execute.assert_not_awaited()

    async def test_revoked_member_receives_no_page_and_cannot_fall_back(self):
        first = await self.tool(call_id="private")
        self.assertTrue(first["ok"])
        self.identities[self.principal] = replace(self.access, member=False)
        result = await self.tool(call_id="private")
        self.assertEqual(result["code"], "project_forbidden")
        self.assertNotIn("clicked", json.dumps(result))
        self.legacy.execute.assert_not_awaited()

    async def test_scope_or_bot_change_cannot_recover_old_browser(self):
        self.assertTrue((await self.tool(call_id="scoped"))["ok"])
        for changes in ({"group_id": 102}, {"self_id": 302}):
            context = replace(self.context, event=self.context.event.model_copy(update=changes))
            denied = await self.tool(call_id="scoped", context=context)
            self.assertEqual(denied["code"], "project_forbidden")
        self.engine.click.assert_awaited_once()
        self.legacy.execute.assert_not_awaited()

    async def test_quota_full_does_not_start_browser_or_legacy_executor(self):
        async def full(project):
            self.record(self.sample(project["quota_bytes"]))
        self.runtime.sampler.sample_one.side_effect = full
        denied = await self.tool("browser_snapshot", {})
        self.assertEqual(denied["code"], "project_quota_full")
        self.browser._spawn.assert_not_awaited()
        self.legacy.execute.assert_not_awaited()

    async def test_no_project_preserves_ordinary_browser(self):
        self.sql.execute("DELETE FROM project_focus")
        result = await self.tool()
        self.assertEqual(result, {"ok": True, "legacy": True})
        self.legacy.execute.assert_awaited_once_with("browser_click", {"ref": "b1"})
        self.browser._spawn.assert_not_awaited()

    async def test_sleeping_project_queues_wake_without_legacy_fallback(self):
        new = self.projects.create(self.access, "Sleeping", quota_bytes=64 * 1024**2)
        preparing = await self.tool()
        self.assertEqual(preparing["code"], "project_preparing")
        self.assertIsNotNone(self.projects.get(self.access, new["project_id"])["lifecycle_job_id"])
        self.browser._spawn.assert_not_awaited()
        self.legacy.execute.assert_not_awaited()

    async def test_unsupported_backend_and_bad_protocol_do_not_fall_back(self):
        self.sql.execute("UPDATE projects SET backend='vm' WHERE project_id=?", (self.pid,))
        unavailable = await self.tool()
        self.assertEqual(unavailable["code"], "project_browser_unavailable")
        self.assertIsNone(self.io.status(self.access, self.pid))
        self.sql.execute("UPDATE projects SET backend='oci' WHERE project_id=?", (self.pid,))
        with patch.object(self.client, "health", return_value={"project_browser_protocol": 1,
                          "channel_ready": True, "instance_id": "f" * 32}):
            unavailable = await self.tool()
        self.assertEqual(unavailable["code"], "project_browser_unavailable")
        self.browser._spawn.assert_not_awaited()
        self.legacy.execute.assert_not_awaited()

    async def test_invalid_arguments_never_reach_worker(self):
        for args in ({"ref": "b1", "project_id": self.pid}, {"ref": "#b1"}, {"ref": "b1\0"}):
            result = await self.tool(args=args)
            self.assertEqual(result["code"], "project_invalid_arguments")
        self.browser._spawn.assert_not_awaited()

    async def test_disabled_setting_and_capability_do_not_publish_project_tools(self):
        context = replace(self.context, agent_executor=None)
        browser_names = lambda c: {d["function"]["name"] for d in domain_registry.definitions(c)
                                  if d["function"]["name"].startswith("browser_")}
        self.assertEqual(len(browser_names(context)), 9)
        self.assertEqual(browser_names(replace(context, capabilities=frozenset({"projects"}))), set())
        self.app.settings.browser_enabled = False
        self.assertEqual(browser_names(context), set())
        self.assertEqual(browser_names(self.context), set())
        self.assertEqual((await self.tool(context=context))["code"], "tool_unavailable")

    async def test_unready_project_runtime_does_not_fall_back_to_conversation_browser(self):
        self.runtime.ready = False
        result = await self.tool()
        self.assertEqual(result["code"], "project_browser_unavailable")
        self.runtime.ready, self.runtime.closed = True, True
        result = await self.tool()
        self.assertEqual(result["code"], "project_browser_unavailable")
        self.browser._spawn.assert_not_awaited()
        self.legacy.execute.assert_not_awaited()

    async def test_private_project_uses_private_browser_without_group_executor(self):
        self.scope = replace(self.scope, kind="private", native_conversation_id="201")
        self.access = replace(self.access, scope=self.scope)
        self.identities[self.principal] = self.access
        self.pid = self.projects.create(self.access, "Private", quota_bytes=64 * 1024**2)["project_id"]
        self.lifecycle.request(self.access, self.pid, "wake", request_key="private-wake",
                               expected_version=self.project()["version"])
        job = self.jobs.claim_due("fixture", kinds=("project.lifecycle",))[0]
        while step := self.lifecycle.next_step(job):
            self.lifecycle.acknowledge(job, step, lifecycle_fixture.ProjectLifecycleTests.evidence(self, step))
        self.lifecycle.finish(job)
        self.volumes.create(self.scope.key, self.pid, quota_bytes=64 * 1024**2, fence=1)
        self.volumes.mutate(self.scope.key, self.pid, "mount", fence=2)
        fence = self.project()["lifecycle_fence"] - 1
        started = await self.workspaces.operate("start", self.scope.key, self.pid, generation=1, fence=fence)
        self.sql.execute("UPDATE projects SET sandbox_id=? WHERE project_id=?", (started["sandbox_id"], self.pid))
        self.identity.update(owner=self.scope.key, project_id=self.pid, fence=fence)
        self.engine.bound_owner = self.scope.key
        self.runtime._execution_access.return_value = self.access
        turn = start_project_turn(self.ledger, self.journal, self.scope, message_id=801)
        context = replace(self.context, agent_executor=None, journal_turn_id=turn.turn_id,
                          event=event(group=None).model_copy(update={"message_id": 801}))
        result = await self.tool("browser_snapshot", {}, context=context)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["project_handle"], f"prj#{self.pid}")
        self.engine.snapshot.assert_awaited_once_with(self.scope.key)
        self.legacy.execute.assert_not_awaited()

    async def test_pending_first_call_hash_is_bound_to_exact_tool_arguments(self):
        token = active_tool_call_id.set("first")
        try:
            request_key, _ = _identity(self.context, "browser_click", {"ref": "b1"})
        finally:
            active_tool_call_id.reset(token)
        with patch.object(self.controller.store, "bind", side_effect=OSError()):
            await self.tool(call_id="first")
        row = self.sql.execute("SELECT * FROM project_io WHERE request_key=?", (request_key,)).fetchone()
        self.assertEqual(row["arguments_sha256"], action_digest("browser_click", {"ref": "b1"}))
        self.assertEqual((await self.tool(args={"ref": "b2"}, call_id="first"))["code"], "project_conflict")
        self.engine.click.assert_not_awaited()
