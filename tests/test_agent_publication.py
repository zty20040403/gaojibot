from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import nonebot

os.environ.setdefault("AI_ALLOW_LEGACY_SQLITE", "true")
os.environ.setdefault("AI_SUBAGENTS_ENABLED", "false")
nonebot.init()

from src.plugins.ai_chat.agent import AgentResult, ContextPacket, DEFAULT_AGENT_REGISTRY
from src.plugins.ai_chat.agent.sessions import cluster_artifact_refs, read_upstream_result, upstream_index
from src.plugins.ai_chat.agent.workspaces import StepWorkspaces
from src.plugins.ai_chat.ai_tools import CLUSTER_ARTIFACT_UPLOAD_TOOL, CLUSTER_JOB_SUBMIT_TOOL, SANDBOX_CREATE_TOOL
from src.plugins.ai_chat.model_catalog import ModelCatalog, ModelProfile
from src.plugins.ai_chat.subagents import AgentExecutionHooks, SubAgentCoordinator, SubAgentStore, TaskStep
from src.plugins.ai_chat.sandbox import DockerSandboxManager, SandboxError
from src.plugins.ai_chat.application.chat_orchestrator import ChatFailure


ARTIFACT_ID = "artifact_" + "a" * 32


class PublicationContextTests(unittest.TestCase):
    def test_legacy_handoff_id_is_visible_even_with_large_context(self):
        upstream = {"validate": {"status": "success", "summary": "checked " * 1000,
            "handoff": ["Publish the validated site with artifact_id=" + ARTIFACT_ID]}}
        packet = ContextPacket("group:1", "group:1:user:2", 2, 3, "publish",
            supporting_context="context " * 3000)
        rendered = packet.for_agent(DEFAULT_AGENT_REGISTRY.worker("operator"), upstream=upstream).rendered_context
        index = json.loads(rendered.split("[上游结构化结果索引]\n")[1])
        self.assertEqual(index["validate"]["cluster_artifacts"], [
            {"artifact_id": ARTIFACT_ID, "section": "handoff", "offset": 0}])
        receipt = json.loads(read_upstream_result(upstream,
            {"step_id": "validate", "section": "cluster_artifacts"}))
        self.assertEqual(receipt["data"], index["validate"]["cluster_artifacts"])

    def test_structured_artifact_id_survives_result_normalization(self):
        result = AgentResult.from_payload({"status": "success", "summary": "validated",
            "artifacts": [{"handle": "s123abc:/workspace/site.zip", "name": "site.zip",
                "snapshot": "b" * 64, "artifact_id": ARTIFACT_ID}],
            "handoff": ["Publish " + ARTIFACT_ID]}).as_payload()
        self.assertEqual(cluster_artifact_refs(result), [{"artifact_id": ARTIFACT_ID,
            "section": "artifacts", "offset": 0, "name": "site.zip"}])

    def test_top_level_receipt_is_accessible_after_metadata_normalization(self):
        result = AgentResult.from_payload({"status": "success", "summary": "uploaded",
            "artifact_id": ARTIFACT_ID}).as_payload()
        upstream = {"build": result}
        self.assertEqual(cluster_artifact_refs(result)[0]["section"], "metadata")
        receipt = json.loads(read_upstream_result(upstream, {"step_id": "build", "section": "metadata"}))
        self.assertEqual(receipt["data"], [{"artifact_id": ARTIFACT_ID}])

    def test_history_and_malformed_ids_are_not_promoted_to_publishable_handles(self):
        result = {"previous_evidence": [{"artifact_id": ARTIFACT_ID}],
            "metadata": {"previous_result": {"artifact_id": ARTIFACT_ID}},
            "handoff": ["artifact_short", ARTIFACT_ID + "0", "x" + ARTIFACT_ID],
            "artifacts": [{"artifact_id": "not-an-artifact"}]}
        self.assertEqual(cluster_artifact_refs(result), [])

    def test_reference_index_is_bounded_and_full_list_is_scoped_and_pageable(self):
        ids = [f"artifact_{index:032x}" for index in range(9)]
        upstream = {"validate": {"artifacts": [{"artifact_id": identifier} for identifier in ids]}}
        index = json.loads(upstream_index(upstream))["validate"]
        self.assertEqual(len(index["cluster_artifacts"]), 5)
        self.assertEqual(index["sections"]["cluster_artifacts"], 9)
        tail = json.loads(read_upstream_result(upstream,
            {"step_id": "validate", "section": "cluster_artifacts", "offset": 5}))
        self.assertEqual([item["artifact_id"] for item in tail["data"]], ids[5:])
        denied = json.loads(read_upstream_result(upstream,
            {"step_id": "other-task", "section": "cluster_artifacts"}))
        self.assertFalse(denied["ok"])

    def test_operator_only_gains_isolated_upload_prerequisites(self):
        allowed = DEFAULT_AGENT_REGISTRY.worker("operator").allowed_tools
        self.assertTrue({"sandbox_create", "sandbox_list", "cluster_artifact_upload", "cluster_job_submit"} <= allowed)
        self.assertFalse({"sandbox_exec", "sandbox_write_file", "sandbox_read_file", "sandbox_destroy",
            "sandbox_host_exec", "operation_approve", "ssh"} & allowed)


class VmWorkspaceValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_quiesce_and_validate_use_separate_verifier_owner(self):
        manager = SimpleNamespace(
            backend="vm",
            list=AsyncMock(return_value=[{
                "sandbox_id": "s123abc", "purpose": "task", "status": "Up (VM)",
            }]),
            stop_owned=AsyncMock(),
            create=AsyncMock(return_value={"sandbox_id": "s456abc"}),
            write_file=AsyncMock(),
            exec=AsyncMock(return_value=SimpleNamespace(returncode=0, stdout="", stderr="")),
            destroy=AsyncMock(),
        )
        executor = SimpleNamespace(owner="group:1", base_owner="group:1", sandbox_manager=manager)
        with tempfile.TemporaryDirectory() as directory:
            workspaces = StepWorkspaces(Path(directory), executor)
            completed = {"build": SimpleNamespace(step=SimpleNamespace(key="build"))}
            await workspaces.quiesce_for_validation(7, completed)
            manager.stop_owned.assert_awaited_once_with("group:1:task#7/build", "s123abc")

            digest = workspaces._persist(7, b"sample image")
            result = await workspaces.validate(7, {"snapshot": digest, "name": "image.png"})
            self.assertTrue(result["ok"])
            manager.create.assert_awaited_once_with("group:1:task#7/verifier", "python")
            args, kwargs = manager.exec.await_args
            self.assertEqual(args[0], "group:1:task#7/verifier")
            self.assertTrue(args[2].startswith("python3 -c"))
            self.assertEqual(kwargs["packages"], ["python3-pil"])
            manager.destroy.assert_awaited_once_with("group:1:task#7/verifier", "s456abc")


class PublicationWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SubAgentStore(Path(self.tmp.name) / "agents.sqlite3")
        self.addCleanup(self.store.close)
        self.profile = ModelProfile(name="test", model="test", provider="test", protocol="openai-chat",
            base_url="http://127.0.0.1", api_key_required=False)
        catalog = ModelCatalog({"test": self.profile}, default_profile="test")
        self.coordinator = SubAgentCoordinator(self.store, catalog, logger=Mock())
        self.packet = ContextPacket("group:1", "group:1:user:2", 2, 3, "publish validated static site")
        self.task = self.store.create_task(scope_key=self.packet.scope_key, conversation_id=self.packet.conversation_id,
            requester_user_id=2, trigger_message_id=3, objective=self.packet.objective, max_parallelism=1, max_steps=1)
        self.step = TaskStep("publish", "operator", "publish validated static site", "preview URL", dependencies=("validate",))
        definitions = [CLUSTER_ARTIFACT_UPLOAD_TOOL, CLUSTER_JOB_SUBMIT_TOOL, SANDBOX_CREATE_TOOL]
        self.tools = {tool["function"]["name"]: tool for tool in definitions}
        self.run = self.store.create_run(self.task.task_id, self.step, allowed_tools=list(self.tools), model_profile="test")
        self.manager = Mock(create=AsyncMock(return_value={"sandbox_id": "s456abc"}), list=AsyncMock(return_value=[]),
            install_readonly_file=AsyncMock(), read_file=AsyncMock(return_value=b"validated site"))
        self.executor = SimpleNamespace(owner=f"{self.packet.conversation_id}:task#{self.task.task_id}/publish",
            sandbox_manager=self.manager)
        self.workspaces = StepWorkspaces(Path(self.tmp.name), self.executor)
        self.client = Mock(upload_artifact=AsyncMock(return_value={"artifact_id": ARTIFACT_ID}),
            submit_job=AsyncMock(return_value={"status": "succeeded", "url": "https://example.invalid/preview"}))

    async def execute_tool(self, name, arguments):
        if name == "sandbox_create":
            result = {"sandbox": await self.manager.create(self.executor.owner, "python")}
        elif name == "cluster_artifact_upload":
            content = await self.manager.read_file(self.executor.owner, arguments["sandbox_id"], arguments["path"],
                max_bytes=25 * 1024 * 1024)
            result = await self.client.upload_artifact(name=arguments["name"], content=content,
                actor="qq:2", origin=self.packet.scope_key)
        elif name == "cluster_job_submit":
            result = await self.client.submit_job(arguments, actor="qq:2", origin=self.packet.scope_key)
        else:
            self.fail("Unexpected tool " + name)
        return json.dumps({"ok": True, **result})

    async def run_publish(self, upstream, model):
        with patch("src.plugins.ai_chat.subagents.ask_deepseek_with_tools", new=model):
            return await self.coordinator._run_step(self.task, self.step, self.run, context=self.packet,
                upstream=upstream, selected_profile=self.profile, tools_by_name=self.tools,
                execute_tool=self.execute_tool, hooks=AgentExecutionHooks(workspaces=self.workspaces))

    async def test_publish_reuses_upstream_upload_without_creating_sandbox(self):
        async def model(user_text, history, tools, execute_tool, **kwargs):
            self.assertIn(ARTIFACT_ID, user_text)
            result = json.loads(await execute_tool("read_agent_result",
                {"step_id": "validate", "section": "cluster_artifacts"}))
            await execute_tool("cluster_job_submit", {"kind": "preview.static",
                "artifact_id": result["data"][0]["artifact_id"], "idempotency_key": "publish-existing"})
            return json.dumps({"status": "success", "summary": "published"})

        outcome = await self.run_publish({"validate": {"status": "success", "summary": "validated",
            "handoff": ["Existing upload: " + ARTIFACT_ID]}}, model)
        self.assertTrue(outcome.succeeded)
        self.client.upload_artifact.assert_not_awaited()
        self.manager.create.assert_not_awaited()
        self.assertEqual(self.client.submit_job.await_args.args[0]["artifact_id"], ARTIFACT_ID)

    async def test_publish_imports_authorized_snapshot_before_controlled_upload(self):
        digest = self.workspaces._persist(self.task.task_id, b"validated site")
        async def model(user_text, history, tools, execute_tool, **kwargs):
            names = {tool["function"]["name"] for tool in tools}
            self.assertTrue({"sandbox_create", "import_agent_artifact", "cluster_artifact_upload", "cluster_job_submit"} <= names)
            sandbox = json.loads(await execute_tool("sandbox_create", {"runtime": "python"}))["sandbox"]["sandbox_id"]
            imported = json.loads(await execute_tool("import_agent_artifact",
                {"step_id": "validate", "artifact_index": 0, "sandbox_id": sandbox}))
            uploaded = json.loads(await execute_tool("cluster_artifact_upload",
                {"sandbox_id": sandbox, "path": imported["path"], "name": "site.zip", "media_type": "application/zip"}))
            await execute_tool("cluster_job_submit", {"kind": "preview.static",
                "artifact_id": uploaded["artifact_id"], "idempotency_key": "publish-imported"})
            return json.dumps({"status": "success", "summary": "published"})

        outcome = await self.run_publish({"validate": {"status": "success", "summary": "validated",
            "artifacts": [{"handle": "s123abc:/workspace/site.zip", "snapshot": digest, "name": "site.zip"}]}}, model)
        self.assertTrue(outcome.succeeded, outcome.error)
        self.manager.install_readonly_file.assert_awaited_once_with(self.executor.owner, "s456abc",
            f"upstream/{digest}/site.zip", b"validated site")
        self.manager.read_file.assert_awaited_once_with(self.executor.owner, "s456abc",
            f"/workspace/upstream/{digest}/site.zip", max_bytes=25 * 1024 * 1024)
        self.client.upload_artifact.assert_awaited_once()
        self.client.submit_job.assert_awaited_once()

    async def test_resumed_publisher_gets_current_id_even_with_frozen_context(self):
        self.store.save_run_context(self.task.task_id, self.run.run_id,
            self.packet.for_agent(DEFAULT_AGENT_REGISTRY.worker("operator"), upstream={}))
        self.store.save_agent_session(self.task.task_id, self.run.run_id,
            [{"role": "user", "content": "publish"}, {"role": "assistant", "content": "Missing artifact ID"}],
            scope_key=self.packet.scope_key, requester_user_id=2, model_profile="test", expected_version=0)
        async def model(user_text, history, tools, execute_tool, **kwargs):
            self.assertTrue(history)
            self.assertIn(ARTIFACT_ID, user_text)
            self.assertIn("cluster_artifacts", user_text)
            await execute_tool("cluster_job_submit", {"kind": "preview.static",
                "artifact_id": ARTIFACT_ID, "idempotency_key": "publish-resumed"})
            return json.dumps({"status": "success", "summary": "published"})
        outcome = await self.run_publish({"validate": {"status": "success", "summary": "validated",
            "handoff": ["Reuse " + ARTIFACT_ID]}}, model)
        self.assertTrue(outcome.succeeded, outcome.error)
        self.client.upload_artifact.assert_not_awaited()
        self.client.submit_job.assert_awaited_once()

    async def test_publish_cannot_import_a_snapshot_outside_direct_dependencies(self):
        with self.assertRaises(ValueError):
            await self.workspaces.import_artifact(self.task.task_id, {"validate": {"artifacts": []}},
                {"step_id": "another-task", "artifact_index": 0, "sandbox_id": "s456abc"})
        self.manager.install_readonly_file.assert_not_awaited()


class PublicationUploadToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_accepts_imported_workspace_path_without_widening_path_access(self):
        import src.plugins.ai_chat as ai_chat
        from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message

        client = Mock(authorization=None, upload_artifact=AsyncMock(return_value={"artifact_id": ARTIFACT_ID}))
        manager = Mock()
        async def read_file(owner, sandbox_id, path, *, max_bytes):
            self.assertEqual(owner, "publisher-owner")
            self.assertEqual(sandbox_id, "s456abc")
            self.assertEqual(max_bytes, 25 * 1024 * 1024)
            DockerSandboxManager._workspace_path(manager, path)
            return b"validated site"
        manager.read_file = AsyncMock(side_effect=read_file)
        executor = Mock(owner="publisher-owner", sandbox_manager=manager,
            retain_task_sandboxes=AsyncMock(return_value={"retained": (), "stopped": (), "failed": ()}))
        requested = {"sandbox_id": "s456abc", "path": "/workspace/upstream/digest/site.zip",
            "name": "site.zip", "media_type": "application/zip"}
        results = []
        async def model(user_text, history, tools, execute_tool, **kwargs):
            results.append(json.loads(await execute_tool("cluster_artifact_upload", requested)))
            return "uploaded"
        event = GroupMessageEvent(time=1, self_id=999, post_type="message", sub_type="normal",
            user_id=2, message_type="group", message_id=3, message=Message("publish"),
            original_message=Message("publish"), raw_message="publish", font=0,
            sender={"user_id": 2, "nickname": "tester", "role": "member"}, group_id=1)
        with (
            patch.object(ai_chat.app_context, "fleet_client", client),
            patch.object(ai_chat.app_context, "subagent_coordinator", None),
            patch.object(ai_chat.app_context, "message_ledger", None),
            patch.object(ai_chat.app_context, "pin_store", None),
            patch.object(ai_chat.app_context, "source_store", None),
            patch.object(ai_chat.handlers.tools, "_fleet_tools_allowed", return_value=True),
            patch.object(ai_chat.handlers.commands, "_current_long_term_memory", return_value=""),
            patch.object(ai_chat.app_context.memory, "append_turn"),
            patch("src.plugins.ai_chat.tool_executor.AgentToolExecutor", return_value=executor),
            patch("src.plugins.ai_chat.tool_executor.ask_deepseek_with_tools", new=model),
        ):
            await ai_chat.handlers.tools._ask_ai(AsyncMock(), event, "publish", available_image_sources=[])
            self.assertEqual(results[-1]["artifact_id"], ARTIFACT_ID)
            self.assertEqual(manager.read_file.await_args.args[2], "upstream/digest/site.zip")
            self.assertEqual(client.upload_artifact.await_args.kwargs["actor"], "qq:2")
            self.assertEqual(client.upload_artifact.await_args.kwargs["origin"], "onebot-v11:group:1")
            client.upload_artifact.reset_mock()
            for path in ("/etc/passwd", "/workspace/../secret", "../secret"):
                requested["path"] = path
                with self.assertRaises(ChatFailure):
                    await ai_chat.handlers.tools._ask_ai(AsyncMock(), event, "publish", available_image_sources=[])
            manager.read_file.side_effect = SandboxError("Sandbox belongs to another step")
            requested["path"] = "upstream/digest/site.zip"
            with self.assertRaises(ChatFailure):
                await ai_chat.handlers.tools._ask_ai(AsyncMock(), event, "publish", available_image_sources=[])
            client.upload_artifact.assert_not_awaited()
