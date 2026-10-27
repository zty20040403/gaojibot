from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from nonebot.adapters.onebot.v11 import Message, MessageSegment, PrivateMessageEvent

from tests import test_external_continuations as continuations
from tests.postgres_fixture import postgres_for_test
from src.bot_security.store import SecurityError
from src.plugins.ai_chat.agent.collaboration_runtime import (
    CollaborationGuard, CollaborationRevoked, CollaborationUnavailable,
    active_collaboration_guard, execution_event, guard_collaboration,
)
from src.plugins.ai_chat.agent.control import active_model_policy
from src.plugins.ai_chat.agent.external import ExternalCalls, poll_external
from src.plugins.ai_chat.collaboration_handlers import collaboration_access, collaboration_command
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.server_task_authorization import ServerTaskAuthorization, server_task
from src.plugins.ai_chat.workers.durable_jobs import JobDeferred


class CollaborationExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        continuations.ExternalContinuationTests.setUp(self)
        self.addCleanup(continuations.ExternalContinuationTests.tearDown, self)
        self.ledger = MessageLedger(postgres_for_test(self))
        self.addCleanup(self.ledger.close)
        self.identities = {user: self.ledger.ensure_principal_identity("onebot-v11", user, f"Member {user}")
                           for user in (2, 3, 4)}
        self.actor_event = self.event.model_copy(update={"user_id": 3, "message_id": 43})
        self.fresh = replace(self.packet, requester_user_id=3, conversation_id="group:1:user:3",
                             memory_context="collaborator-only-memory")
        self.store.finish_run(self.run.run_id, "succeeded", result={"summary": "original public result"})
        self.store.set_task_state(self.task.task_id, "completed", result={"answer": "original"})
        self.services.context.message_ledger = self.ledger
        self.services.context.db_call = self.db_call
        self.bot = SimpleNamespace(self_id="123", get_group_member_list=AsyncMock(
            return_value=[{"user_id": user, "nickname": f"Member {user}"} for user in (2, 3, 4)]))

    @staticmethod
    async def db_call(method, *args, **kwargs):
        return await asyncio.to_thread(method, *args, **kwargs)

    def grant(self, enabled=True):
        return self.store.set_collaborator(self.task.task_id, scope_key=self.scope, requester_user_id=2,
            principal_id=self.identities[3], enabled=enabled,
            expected_version=self.store.control(self.task.task_id)["version"])

    def revise(self, *, packet=None, event=None):
        return self.coordinator.revise(self.task.task_id, scope_key=self.scope, requester_user_id=3,
            instruction="Include the second disk", step_keys=["disk"],
            expected_version=self.store.control(self.task.task_id)["version"],
            revision_context=packet or self.fresh,
            revision_event=(event or self.actor_event).model_dump(mode="json"))

    def guard(self):
        return CollaborationGuard(self.services.context, self.bot, self.store.get(self.task.task_id),
                                  self.actor_event, self.store.control(self.task.task_id)["revision"])

    async def command(self, text, *, event=None, mention=None):
        args = Message(MessageSegment.text(f"task#{self.task.task_id} {text}"))
        if mention is not None:
            args += MessageSegment.at(mention)
        return await collaboration_command(self.services, self.bot, event or self.event, args)

    async def test_revision_keeps_owner_and_delivery_but_uses_actual_actor_context(self):
        before = self.store.control(self.task.task_id)["dispatch"]
        self.grant()
        self.store.update_control(self.task.task_id, expected_version=2,
                                  policy={"mode": "locked", "profile": "qwen-local"})
        result = self.revise()
        task = self.store.get(self.task.task_id)
        self.assertEqual(task.requester_user_id, 2)
        self.assertEqual(result["dispatch"]["event"], before["event"])
        self.assertEqual(result["dispatch"]["bot_id"], before["bot_id"])
        self.assertEqual(self.store.revision_actor(task.task_id), 3)
        self.assertEqual(self.store.revision_actor(task.task_id, 1), 2)
        self.assertEqual(result["dispatch"]["execution"]["context_packet"], self.fresh.as_payload())
        self.assertEqual(execution_event(self.store, task, result).user_id, 3)
        self.assertEqual(self.store.execution_model_policy(task.task_id), {"mode": "auto"})

    async def test_forged_scope_user_conversation_or_bot_is_rejected(self):
        self.grant()
        for field, value in (("requester_user_id", 2), ("scope_key", "onebot-v11:group:99"),
                             ("conversation_id", "group:1:user:2")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.revise(packet=replace(self.fresh, **{field: value}))
        for field, value in (("user_id", 2), ("group_id", 99), ("self_id", 456)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.revise(event=self.actor_event.model_copy(update={field: value}))
        self.assertEqual(self.store.control(self.task.task_id)["revision"], 1)
        self.assertEqual(self.store.get(self.task.task_id).status, "completed")

    async def test_revocation_and_regrant_never_reactivate_old_execution(self):
        self.grant()
        self.revise()
        self.store.require_execution_actor(self.task.task_id, 3)
        self.grant(False)
        with self.assertRaises((ValueError, PermissionError)):
            self.store.require_execution_actor(self.task.task_id, 3)
        self.grant()
        with self.assertRaisesRegex(PermissionError, "replaced"):
            self.store.require_execution_actor(self.task.task_id, 3)
        with self.assertRaises(PermissionError):
            self.store.require_execution_actor(self.task.task_id, 2)

    async def test_resume_restores_collaborator_context_not_original_personal_memory(self):
        self.grant()
        self.revise()

        async def resumed(task, *, context, **kwargs):
            self.assertEqual(context, self.fresh)
            self.assertEqual(active_model_policy.get(), {"mode": "auto"})
            return "resumed"

        token = active_collaboration_guard.set(self.guard())
        try:
            with patch.object(self.coordinator, "_resume_task", new=AsyncMock(side_effect=resumed)):
                result = await self.coordinator.resume(self.task.task_id, scope_key=self.scope, requester_user_id=3,
                    selected_profile=self.catalog.default, tools=[], execute_tool=AsyncMock())
        finally:
            active_collaboration_guard.reset(token)
        self.assertEqual(result, "resumed")
        with self.assertRaises(ValueError):
            await self.coordinator.resume(self.task.task_id, scope_key=self.scope, requester_user_id=2,
                selected_profile=self.catalog.default, tools=[], execute_tool=AsyncMock())

    async def test_direct_resume_cannot_bypass_runtime_membership_guard(self):
        self.grant()
        self.revise()
        with self.assertRaisesRegex(PermissionError, "background job"):
            await self.coordinator.resume(self.task.task_id, scope_key=self.scope, requester_user_id=3,
                selected_profile=self.catalog.default, tools=[], execute_tool=AsyncMock())
        self.assertEqual(self.store.get(self.task.task_id).status, "queued")

    async def test_owner_next_revision_removes_collaborator_context_but_keeps_history_actor(self):
        self.grant()
        self.revise()
        self.store.set_task_state(self.task.task_id, "completed")
        self.coordinator.revise(self.task.task_id, scope_key=self.scope, requester_user_id=2,
            instruction="owner revision", step_keys=["disk"],
            expected_version=self.store.control(self.task.task_id)["version"])
        self.assertNotIn("execution", self.store.control(self.task.task_id)["dispatch"])
        self.assertEqual(self.store.revision_actor(self.task.task_id), 2)
        self.assertEqual(self.store.revision_actor(self.task.task_id, 2), 3)
        self.assertEqual(self.store.revision_actor(self.task.task_id, 1), 2)

    async def test_unknown_membership_pauses_and_leaving_cancels_before_model_call(self):
        self.grant()
        self.revise()
        self.dispatcher.enqueue(self.task.task_id)
        job = self.jobs.claim_due("worker")[0]
        self.bot.get_group_member_list.side_effect = OSError("offline")
        with patch("src.plugins.ai_chat.agent.background.get_bot", return_value=self.bot):
            with self.assertRaises(JobDeferred):
                await self.dispatcher.execute(job)
            self.assertEqual(self.store.get(self.task.task_id).status, "interrupted")
            self.assertEqual(self.outbox.recent(), [])
            self.bot.get_group_member_list.side_effect = None
            self.bot.get_group_member_list.return_value = [{"user_id": 2}]
            result = await self.dispatcher.execute(job)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(len(self.outbox.recent()), 1)
        self.services.tools._ask_ai.assert_not_awaited()
        self.assertIsNone(active_collaboration_guard.get())

    async def test_background_uses_revision_actor_and_original_group_outbox(self):
        self.grant()
        self.revise()

        async def execute(bot, event, objective, **kwargs):
            self.assertEqual(event.user_id, 3)
            self.assertIsNone(kwargs["selected_model_override"])
            self.assertIsNotNone(active_collaboration_guard.get())
            await guard_collaboration()
            self.store.set_task_state(self.task.task_id, "completed", result={"answer": "revision done"})

        self.services.tools._ask_ai.side_effect = execute
        self.dispatcher.enqueue(self.task.task_id)
        job = self.jobs.claim_due("worker")[0]
        with patch("src.plugins.ai_chat.agent.background.get_bot", return_value=self.bot), \
                patch.object(self.outbox, "enqueue", wraps=self.outbox.enqueue) as enqueue:
            result = await self.dispatcher.execute(job)
            self.assertEqual(enqueue.call_args.kwargs["reply_to_native_message_id"], str(self.event.message_id))
            self.assertEqual(enqueue.call_args.kwargs["target_scope"].key, self.scope)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.store.get(self.task.task_id).requester_user_id, 2)
        self.assertIsNone(active_collaboration_guard.get())

    async def test_tool_boundary_rechecks_revocation_even_with_cached_roster(self):
        self.grant()
        self.revise()
        guard = self.guard()
        await guard.check()
        await guard.check()
        self.assertEqual(self.bot.get_group_member_list.await_count, 1)
        self.grant(False)
        token = active_collaboration_guard.set(guard)
        try:
            with self.assertRaises(CollaborationRevoked):
                await guard_collaboration()
        finally:
            active_collaboration_guard.reset(token)

    async def test_guard_fails_closed_for_invalid_roster_or_absent_creator(self):
        self.grant()
        self.revise()
        self.bot.get_group_member_list.return_value = {"members": []}
        with self.assertRaises(CollaborationUnavailable):
            await self.guard().check()
        self.bot.get_group_member_list.return_value = [{"user_id": 3}]
        with self.assertRaises(CollaborationRevoked):
            await self.guard().check()

    async def test_server_authorization_binds_actual_executor_not_creator(self):
        self.grant()
        self.revise()
        self.services.context.mobile_authorization = SimpleNamespace(bot_selector=lambda: "123", executors={})
        authorization = ServerTaskAuthorization(self.services.context)
        task = self.store.get(self.task.task_id)
        scope = {"kind": "task", "id": task.task_id, "scope": self.scope, "qq_id": "3",
                 "bot_id": "123", "revision": 2, "objective": task.objective}
        authorization.validate(scope)
        with self.assertRaises(SecurityError):
            authorization.validate({**scope, "qq_id": "2"})
        self.grant(False)
        with self.assertRaises(SecurityError):
            authorization.validate(scope)

    async def test_background_approval_rechecks_membership_outside_task_guard(self):
        self.grant()
        self.revise()
        self.services.context.mobile_authorization = SimpleNamespace(bot_selector=lambda: "123", executors={})
        authorization = ServerTaskAuthorization(self.services.context)
        scope = {"kind": "task", "id": self.task.task_id, "scope": self.scope, "qq_id": "3",
                 "bot_id": "123", "revision": 2, "objective": self.store.get(self.task.task_id).objective}
        with patch("src.plugins.ai_chat.server_task_authorization.get_bot", return_value=self.bot):
            await authorization.validate_execution(scope)
            self.bot.get_group_member_list.return_value = [{"user_id": 2}]
            with self.assertRaisesRegex(SecurityError, "离群"):
                await authorization.validate_execution(scope)
            self.bot.get_group_member_list.side_effect = OSError("offline")
            with self.assertRaisesRegex(SecurityError, "无法核实"):
                await authorization.validate_execution(scope)

    async def test_external_submission_and_restart_poll_use_collaborator_not_owner(self):
        self.grant()
        self.revise()
        tracker = ExternalCalls(self.store, self.task.task_id, self.run.run_id)
        tracker.call_id, tracker.tool_name = "collaborative-call", "ops_call"
        tracker.arguments = {"operation": "exec.run", "host_id": "node-a"}
        perform = AsyncMock(return_value={"operation": {"operation_id": "op_shared", "status": "running"}})
        with self.assertRaises(PermissionError):
            await tracker.request("POST", "/v1/ops/call", tracker.arguments,
                                  actor="qq:2", origin=self.scope, perform=perform)
        perform.assert_not_awaited()
        await tracker.request("POST", "/v1/ops/call", tracker.arguments,
                              actor="qq:3", origin=self.scope, perform=perform)

        async def response(*args, **kwargs):
            self.assertEqual(server_task.get()["qq_id"], "3")
            self.assertEqual(kwargs["actor"], "qq:3")
            return {"operation_id": "op_shared", "status": "succeeded"}

        self.client._request.side_effect = response
        self.assertTrue(await poll_external(self.store, self.store.get(self.task.task_id), self.client))
        self.client._request.assert_awaited_once()
        self.assertIsNone(server_task.get())

    async def test_revisable_list_is_task_and_group_scoped_and_revocation_removes_it(self):
        fields = {"scope_key": self.scope, "requester_user_id": 3}
        self.assertEqual(self.store.revisable_tasks(**fields), [])
        self.grant()
        self.assertEqual([t.task_id for t in self.store.revisable_tasks(**fields)], [self.task.task_id])
        self.assertEqual(self.store.revisable_tasks(**{**fields, "scope_key": "onebot-v11:group:2"}), [])
        self.assertEqual(self.store.revisable_tasks(**{**fields, "requester_user_id": 4}), [])
        self.grant(False)
        self.assertEqual(self.store.revisable_tasks(**fields), [])

    async def test_command_grants_feedback_merge_and_revocation(self):
        self.assertIn("已添加", await self.command("添加 ", mention=3))
        self.assertIn("已记录", await self.command("意见 增加磁盘对比", event=self.actor_event))
        with patch("src.plugins.ai_chat.collaboration_handlers.tool_enabled", return_value=True):
            self.assertIn("第 2 版已排队", await self.command("汇总", event=self.actor_event))
        self.assertEqual(self.store.revision_actor(self.task.task_id), 3)
        view = self.store.collaboration(self.task.task_id, scope_key=self.scope, requester_user_id=2)
        self.assertEqual(view["feedback"][0]["status"], "consumed")
        self.assertIn("增加磁盘对比", self.store.get(self.task.task_id).objective)
        self.assertIn("已移除", await self.command("移除 ", mention=3))
        with self.assertRaises(CollaborationRevoked):
            await self.guard().check()

    async def test_command_denies_outsider_private_disabled_group_and_collaborator_grant(self):
        self.assertIn("authorized", await self.command("查看", event=self.actor_event))
        self.grant()
        self.assertIn("只有任务发起人", await self.command("添加 ", mention=4, event=self.actor_event))
        private = PrivateMessageEvent(time=self.event.time, self_id=123, post_type="message", message_type="private",
            sub_type="friend", message_id=44, user_id=2, message="test", raw_message="test", font=0, sender={"user_id": 2})
        self.assertIn("原来的 QQ 群", await self.command("查看", event=private))
        self.services.group_enabled = lambda _: False
        self.assertIn("群已停用", await self.command("查看"))

    async def test_departed_feedback_author_blocks_merge_without_consuming_comments(self):
        self.grant()
        await self.command("意见 second disk", event=self.actor_event)
        self.bot.get_group_member_list.return_value = [{"user_id": 2}]
        self.assertIn("已离群成员", await self.command("汇总"))
        self.assertEqual(self.store.control(self.task.task_id)["revision"], 1)
        self.assertEqual(self.store.collaboration(self.task.task_id, scope_key=self.scope,
                         requester_user_id=2)["feedback"][0]["status"], "pending")

    async def test_roster_network_error_never_authorizes_or_mutates(self):
        self.bot.get_group_member_list.side_effect = OSError("offline")
        self.assertIn("暂时无法核实", await self.command("添加 ", mention=3))
        self.assertEqual(self.store.control(self.task.task_id)["version"], 1)
        with self.assertRaises(JobDeferred):
            await collaboration_access(self.services, self.bot, self.event, self.task.task_id)

    async def test_malformed_persisted_envelope_cannot_fall_back_to_owner(self):
        self.grant()
        control = self.revise()
        for field, value in (("actor_user_id", 2), ("revision", 1)):
            invalid = copy.deepcopy(control)
            invalid["dispatch"]["execution"][field] = value
            with patch.object(self.store, "control", return_value=invalid), self.assertRaises(ValueError):
                execution_event(self.store, self.store.get(self.task.task_id), invalid)

    async def test_model_overrides_cannot_silently_replace_collaborator_auto_policy(self):
        self.grant()
        self.revise()
        with self.assertRaisesRegex(ValueError, "协作者"):
            self.coordinator.configure_models(self.task.task_id, {"mode": "locked", "profile": "qwen-local"},
                self.store.control(self.task.task_id)["version"])
        self.assertEqual(self.store.execution_model_policy(self.task.task_id), {"mode": "auto"})

    async def test_admin_projection_is_safe_and_console_cannot_impersonate_creator(self):
        import httpx
        from fastapi import FastAPI
        from src.plugins.ai_chat.admin import AdminServices
        from tests.admin_session_fixture import ApprovedClient, register_admin

        self.grant()
        self.revise()
        self.store.set_task_state(self.task.task_id, "completed")
        app = FastAPI()
        register_admin(app, AdminServices(version="test", started_at=1, subagent_store=self.store,
            subagent_coordinator=self.coordinator, model_catalog=self.catalog), test_case=self)
        async with ApprovedClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            headers = {"X-Test-Login": "admin", "If-Match": '"0"'}
            path = f"/bot-admin/api/subagents/{self.task.task_id}"
            response = await client.get(path, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertEqual(payload["collaboration"]["creator_user_id"], 2)
            self.assertEqual(payload["collaboration"]["execution_user_id"], 3)
            self.assertEqual(payload["collaboration"]["members"][0]["native_user_id"], "3")
            self.assertFalse(payload["revision_allowed"])
            self.assertNotIn("context_packet", payload["collaboration"])
            self.assertNotIn("dispatch", payload["control"])
            before = self.store.control(self.task.task_id)
            response = await client.post(path + "/revise", headers=headers,
                json={"expected_version": before["version"], "instruction": "run as someone else", "step_keys": ["disk"]})
            self.assertEqual(response.status_code, 403, response.text)
            self.assertEqual(self.store.control(self.task.task_id), before)
