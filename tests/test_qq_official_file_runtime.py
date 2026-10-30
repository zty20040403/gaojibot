"""Real source/capacity/manifest storage with the existing owned guest contract.

No platform network, credentials, real VM, alternate model loop or ad-hoc DDL.
"""
import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official import execution
from src.plugins.ai_chat.adapters.qq_official.file_runtime import OfficialFileRuntime, GENERATION_TOOLS
from src.plugins.ai_chat.adapters.qq_official.subagent_runtime import submit, job_kind
from src.plugins.ai_chat.agent.artifact_delivery import (
    ArtifactDeliveryError, canonical_artifact_filename, delivery_state,
)
from src.plugins.ai_chat.agent.contracts import ContextPacket
from src.plugins.ai_chat.agent.control import LeaseLost
from src.plugins.ai_chat.agent.workspaces import ArtifactCaptureError
from src.plugins.ai_chat.application.owned_sandbox import ArtifactAvailability, GenerationError
from src.plugins.ai_chat.model_catalog import ModelCatalog, ModelProfile
from src.plugins.ai_chat.platform_types import InboundMessage, PlatformFeatures
from src.plugins.ai_chat.subagents import SubAgentCoordinator, SubAgentStore, StepOutcome, TaskStep
from src.plugins.ai_chat.tool_invocation import active_tool_call_id
from src.plugins.ai_chat.tools import neutral_registry_for
from src.plugins.ai_chat.tools.state import ToolContext, TurnState
from tests.qq_official_file_fixture import FileFixture
from tests.test_owned_sandbox import FakeManager


class RuntimeFixture(FileFixture):
    def setUp(self):
        super().setUp()
        self.calls = DatabaseCalls()
        self.addCleanup(lambda: asyncio.run(self.calls.close()))
        self.manager = FakeManager()
        self.route_enabled = True
        self.platform = SimpleNamespace(account=self.account, features=PlatformFeatures(),
            execution=SimpleNamespace(token=self.token, handle=self.handle, job=self.job))
        self.inbound = InboundMessage(self.endpoint, self.actor, None, "TEST_INPUT", "test", "",
            self.message.body, True, None, self.now, None)
        self.runtime = self.build()

    def build(self, **kwargs):
        options = dict(account=self.account, token=self.token, artifact_store=self.files,
            database_calls=self.calls, manager=self.manager, admission=self.admission,
            route_availability=self.route, enabled=True)
        options.update(kwargs)
        return OfficialFileRuntime(**options)

    async def route(self, binding):
        await binding.assert_current()
        return ArtifactAvailability(self.route_enabled, 1024*1024, "link")

    async def foreground(self):
        return await self.runtime.foreground(self.inbound, self.platform)

    async def produce(self, session, *, filename="report.pdf"):
        sid = json.loads(await session.executor.execute("sandbox_create", {"runtime": "python"}))["sandbox_id"]
        self.assertTrue(json.loads(await session.executor.execute("sandbox_write_file",
            {"sandbox_id":sid, "path":"input.md", "content":"TEST immutable document"}))["ok"])
        self.assertTrue(json.loads(await session.executor.execute("sandbox_exec",
            {"sandbox_id":sid, "command":"generate-pdf"}))["ok"])
        return sid, {"handle":sid+":/workspace/result.pdf", "name":filename}

    async def send(self, session, sid):
        call = active_tool_call_id.set("TEST_FILE_CALL")
        try:
            return json.loads(await session.executor.execute("send_file_from_sandbox",
                {"sandbox_id":sid, "path":"result.pdf", "filename":"report.pdf"}))
        finally:
            active_tool_call_id.reset(call)


class ForegroundRuntimeTests(RuntimeFixture):
    def test_default_off_missing_route_and_unavailable_route_hide_catalog(self):
        async def run():
            for runtime in (self.build(enabled=False), self.build(route_availability=None)):
                self.assertIsNone(await runtime.foreground(None, None))
            self.route_enabled = False
            self.assertIsNone(await self.foreground())
        asyncio.run(run())
        self.assertEqual(self.manager.calls, [])
        self.assertEqual(self.query("SELECT * FROM qq_official_generation_capacity"), [])

    def test_actual_neutral_catalog_executor_registers_only_and_cleanup_releases(self):
        async def run():
            session = await self.foreground()
            context = ToolContext(SimpleNamespace(context=SimpleNamespace(db_call=self.calls)),
                inbound=self.inbound, platform=self.platform)
            registry = neutral_registry_for(context)
            self.assertFalse(GENERATION_TOOLS & {x["function"]["name"] for x in registry.definitions(context)})
            context = session.context(context)
            self.assertTrue(GENERATION_TOOLS <= {x["function"]["name"] for x in registry.definitions(context)})
            turn = TurnState(context)
            sid = json.loads(await registry.execute(turn, "sandbox_create", {}))["sandbox_id"]
            await registry.execute(turn, "sandbox_write_file", {"sandbox_id":sid, "path":"input.md", "content":"TEST"})
            await registry.execute(turn, "sandbox_exec", {"sandbox_id":sid, "command":"generate-pdf"})
            result = await self.send(session, sid)
            self.assertTrue(result["stored"])
            self.assertIs(result["message_accepted"], False)
            rows = self.query("SELECT * FROM qq_official_file_artifacts")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["source_execution_id"], self.handle.execution_id)
            self.assertEqual(rows[0]["endpoint_key"], self.endpoint.key)
            self.assertEqual(rows[0]["requester_actor_key"], self.actor.key)
            original = self.manager.items[sid]["files"]["result.pdf"]
            self.assertEqual(self.files.read_verified(rows[0]["artifact_id"],
                sha256=rows[0]["sha256"], byte_size=rows[0]["byte_size"]), original)
            self.assertEqual(self.query("SELECT * FROM deliveries"), [])
            self.assertEqual(await session.close(), {"released":1, "retained":0})
            self.assertFalse(GENERATION_TOOLS & {x["function"]["name"] for x in registry.definitions(context)})
        asyncio.run(run())
        self.assertEqual(self.query("SELECT * FROM qq_official_generation_capacity WHERE state='reserved'"), [])

    def test_actor_native_and_history_scope_mismatch_rejected_before_io(self):
        async def run():
            for inbound in (replace(self.inbound, actor=replace(self.actor, native_user_id="TEST_OTHER")),
                            replace(self.inbound, native_message_id="TEST_OTHER")):
                with self.assertRaises(LeaseLost):
                    await self.runtime.foreground(inbound, self.platform)
            session = await self.foreground()
            with self.assertRaises(GenerationError):
                session.context(ToolContext(SimpleNamespace(), inbound=replace(self.inbound,
                    native_message_id="TEST_OTHER"), platform=self.platform))
        asyncio.run(run())
        self.assertEqual(self.manager.calls, [])

    def test_unissued_handle_cannot_enable_generation(self):
        self.platform.execution.handle = replace(self.handle)
        async def run():
            with self.assertRaises(Exception):
                await self.foreground()
        asyncio.run(run())
        self.assertEqual(self.manager.calls, [])

    def test_pending_deadline_uses_source_creation_not_factory_time(self):
        async def run():
            first = await self.foreground()
            self.now += 20
            second = await self.foreground()
            self.assertEqual(first.source.pending_deadline_at, second.source.pending_deadline_at)
        asyncio.run(run())

    def test_route_revoked_before_export_cannot_register(self):
        async def run():
            session = await self.foreground()
            sid, _ = await self.produce(session)
            self.route_enabled = False
            self.assertFalse((await self.send(session, sid))["ok"])
            self.assertEqual(self.query("SELECT * FROM qq_official_file_artifacts"), [])
            await session.close()
        asyncio.run(run())

    def test_same_tool_invocation_reuses_one_manifest_and_existing_job(self):
        async def run():
            session = await self.foreground()
            sid, _ = await self.produce(session)
            first = await self.send(session, sid)
            second = await self.send(session, sid)
            self.assertEqual(first["artifact_ref"], second["artifact_ref"])
            self.assertEqual(len(self.query("SELECT * FROM qq_official_file_artifacts")), 1)
            self.assertEqual(len(self.query("SELECT * FROM qq_official_file_deliveries")), 1)
            await session.close()
        asyncio.run(run())

    def test_unknown_guest_mutation_preserves_capacity_and_no_replay(self):
        async def run():
            session = await self.foreground()
            self.manager.fail = "create"
            result = json.loads(await session.executor.execute("sandbox_create", {}))
            self.assertFalse(result["ok"])
            self.assertNotIn("TEST_SECRET", json.dumps(result))
            self.assertEqual(len(self.manager.items), 1)
            await session.close()
            self.assertEqual(len(self.manager.items), 1)
        asyncio.run(run())
        self.assertEqual(len(self.query("SELECT * FROM qq_official_generation_capacity WHERE state='reserved'")), 1)

    def test_lost_source_close_never_destroys_or_refunds(self):
        async def run():
            session = await self.foreground()
            await self.produce(session)
            self.admitted = False
            self.assertEqual(await session.close(), {"released":0, "retained":1})
        asyncio.run(run())
        self.assertFalse(any(x[0] == "destroy" for x in self.manager.calls))
        self.assertEqual(len(self.query("SELECT * FROM qq_official_generation_capacity WHERE state='reserved'")), 1)

    def test_loss_after_durable_registration_preserves_evidence_without_claiming_send(self):
        register = self.files.register
        def lose_after(*args, **kwargs):
            value = register(*args, **kwargs)
            self.admitted = False
            return value
        self.files.register = lose_after
        async def run():
            session = await self.foreground()
            sid, _ = await self.produce(session)
            with self.assertRaises(LeaseLost):
                await self.send(session, sid)
            self.assertEqual(len(self.query("SELECT * FROM qq_official_file_artifacts")), 1)
            self.assertEqual(self.query("SELECT * FROM deliveries"), [])
            self.assertEqual(await session.close(), {"released":0, "retained":1})
        asyncio.run(run())
        self.assertEqual(len(self.query("SELECT * FROM qq_official_generation_capacity WHERE state='reserved'")), 1)


class CoordinatorRuntimeTests(RuntimeFixture):
    def setUp(self):
        super().setUp()
        self.tasks = SubAgentStore(self.database)
        self.addCleanup(self.tasks.close)
        profile = ModelProfile(name="test", provider="test", protocol="openai-chat", model="test",
            base_url="http://127.0.0.1", api_key_required=False)
        self.coordinator = SubAgentCoordinator(self.tasks,
            ModelCatalog({"test":profile}, default_profile="test"), logger=Mock())
        packet = ContextPacket(self.endpoint.scope.key, "TEST_FILES", None,
            self.message.canonical_message_id, "生成并发送 PDF 文件", endpoint=self.endpoint, requester_actor=self.actor)
        with self.tx() as cursor:
            self.task = submit(cursor, token=self.token, source_api=execution, source_handle=self.handle,
                source_job=self.job, tool_call_id="TEST_FILES_TASK", tool_name="run_subagents",
                packet=packet, decision=None, profile="test", now=self.now, execution_deadline=self.now+1200,
                result_wait_seconds=600, admission=self.admission, coordinator=self.coordinator, jobs=self.jobs)
            cursor.execute("UPDATE subagent_tasks SET plan_json=? WHERE task_id=?", (
                json.dumps({"contract":{"delivery_required":True}}), self.task.task_id))
        self.tasks.set_task_state(self.task.task_id, "running")
        self.task = self.tasks.get(self.task.task_id)
        self.task_job = self.jobs.claim_due("TEST_FILES_WORKER", kinds=(job_kind(self.account),), now=self.now)[0]
        self.step = TaskStep("document", "document", "TEST document", "PDF")
        self.run = self.tasks.create_run(self.task.task_id, self.step, allowed_tools=tuple(GENERATION_TOOLS),
            model_profile="test", now=self.now)

    async def session(self):
        return await self.runtime.subagent(self.task_job, self.task)

    async def captured(self, session, filename="report.pdf"):
        sid, raw = await self.produce(session, filename=filename)
        values = await session.hooks.workspaces.capture(self.task.task_id,
            [{**raw, "snapshot":"f"*64, "draft":False, "url":"https://TEST.invalid/private"}])
        self.assertNotIn("url", values[0])
        self.assertNotIn("draft", values[0])
        self.tasks.add_artifacts(self.task.task_id, self.run.run_id, values, now=self.now)
        self.tasks.finish_run(self.run.run_id, "succeeded", result={"artifacts":values}, now=self.now)
        return sid, values[0]

    async def deliver(self, session, item, *, draft=False):
        outcome = StepOutcome(self.step, self.tasks.runs(self.task.task_id)[0], {"artifacts":[item]}, None)
        check = await session.hooks.workspaces.validate(self.task.task_id, item)
        validation = {"status":"failed" if draft else "passed",
            "artifacts":[{"artifact_key":item["snapshot"], "status":"failed" if draft else "passed", "reason":"TEST incomplete"}],
            "checks":[{"artifact_key":item["snapshot"], **check}]}
        async def legacy(*_):
            raise AssertionError("legacy transport must not execute")
        return await self.coordinator._deliver_requested_artifacts(self.task, {self.step.key:outcome},
            execute_tool=legacy, delivered_artifacts=set(), progress=None,
            hooks=session.hooks, validation=validation)

    def test_capture_independent_validation_then_actual_coordinator_registration(self):
        async def run():
            session = await self.session()
            sid, item = await self.captured(session)
            expected = self.manager.items[sid]["files"]["result.pdf"]
            self.manager.items[sid]["files"]["result.pdf"] = b"TEST guest tampered after capture"
            self.assertEqual(self.query("SELECT * FROM qq_official_file_artifacts"), [])
            deliveries = await self.deliver(session, item)
            self.assertEqual(len(deliveries), 1)
            self.assertFalse(deliveries[0]["message_accepted"])
            rows = self.query("SELECT * FROM qq_official_file_artifacts")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["sha256"], hashlib.sha256(expected).hexdigest())
            self.assertFalse(rows[0]["draft"])
            self.assertEqual(rows[0]["source_task_id"], self.task.task_id)
            self.assertEqual(self.tasks.deliveries(self.task.task_id)[0]["payload"]["state"], "stored")
            await session.close()
        asyncio.run(run())

    def test_draft_is_registered_once_after_verdict_with_exact_actual_name(self):
        async def run():
            session = await self.session()
            _, item = await self.captured(session, filename="测"*56+".pdf")
            deliveries = await self.deliver(session, item, draft=True)
            row = self.query("SELECT * FROM qq_official_file_artifacts")[0]
            self.assertTrue(row["draft"])
            self.assertTrue(row["filename"].startswith("草稿-"))
            self.assertLessEqual(len(row["filename"].encode()), 180)
            self.assertEqual(deliveries[0]["filename"], row["filename"])
            self.assertEqual(len(self.query("SELECT * FROM qq_official_file_artifacts")), 1)
            await session.close()
        asyncio.run(run())

    def test_restart_restores_only_recorded_capture_and_does_not_reexport(self):
        async def run():
            first = await self.session()
            _, item = await self.captured(first)
            await first.close()
            before = len([x for x in self.manager.calls if x[0] == "export"])
            second = await self.session()
            result = await self.deliver(second, item)
            self.assertEqual(len(result), 1)
            self.assertEqual(len([x for x in self.manager.calls if x[0] == "export"]), before)
            self.assertFalse(result[0]["message_accepted"])
            await second.close()
        asyncio.run(run())

    def test_forged_hash_handle_size_and_unrecorded_capture_cannot_read_host_blob(self):
        async def run():
            first = await self.session()
            _, item = await self.captured(first)
            await first.close()
            second = await self.session()
            for changed in ({**item, "snapshot":"f"*64}, {**item, "size":item["size"]+1},
                            {**item, "handle":"s999999:/workspace/result.pdf"}, {**item, "name":"foreign.pdf"}):
                with self.assertRaises(GenerationError):
                    await second.hooks.workspaces.validate(self.task.task_id, changed)
            with self.tx() as cursor:
                cursor.execute("DELETE FROM subagent_artifacts WHERE task_id=?", (self.task.task_id,))
            with self.assertRaises(GenerationError):
                await second.hooks.workspaces.validate(self.task.task_id, item)
        asyncio.run(run())
        self.assertEqual(self.query("SELECT * FROM qq_official_file_artifacts"), [])

    def test_foreign_sandbox_capture_rejected_without_export_or_registration(self):
        async def run():
            session = await self.session()
            with self.assertRaises(ArtifactCaptureError):
                await session.hooks.workspaces.capture(self.task.task_id,
                    [{"handle":"s999999:/workspace/test.pdf", "name":"test.pdf"}])
            self.assertEqual(self.manager.calls, [])
            with self.assertRaises(LeaseLost):
                await self.runtime.subagent(self.job, self.task)
        asyncio.run(run())

    def test_source_revocation_between_capture_and_registration_leaves_no_artifact(self):
        async def run():
            session = await self.session()
            _, item = await self.captured(session)
            self.admitted = False
            with self.assertRaises(LeaseLost):
                await self.deliver(session, item)
            await session.close()
        asyncio.run(run())
        self.assertEqual(self.query("SELECT * FROM qq_official_file_artifacts"), [])
        self.assertEqual(len(self.query("SELECT * FROM qq_official_generation_capacity WHERE state='reserved'")), 1)

    def test_authorized_upstream_import_reads_snapshot_not_mutated_guest(self):
        async def install(owner, sid, path, content):
            self.manager.own(owner, sid)["files"][path] = bytes(content)
            self.manager.event("readonly", owner, sid)
        self.manager.install_readonly_file = install
        async def run():
            session = await self.session()
            sid, item = await self.captured(session)
            self.manager.items[sid]["files"]["result.pdf"] = b"TEST mutated guest"
            target = json.loads(await session.executor.execute("sandbox_create", {}))["sandbox_id"]
            args = {"step_id":"document", "artifact_index":0, "sandbox_id":target}
            with self.assertRaises(GenerationError):
                await session.workspaces.import_artifact(self.task.task_id, {}, args)
            result = json.loads(await session.workspaces.import_artifact(self.task.task_id,
                {"document":{"artifacts":[item]}}, args))
            actual = self.manager.items[target]["files"][result["path"].removeprefix("/workspace/")]
            self.assertTrue(result["read_only"])
            self.assertEqual(hashlib.sha256(actual).hexdigest(), item["snapshot"])
            with self.assertRaises(GenerationError):
                await session.workspaces.import_artifact(self.task.task_id,
                    {"document":{"artifacts":[item]}}, {**args, "sandbox_id":"s999999"})
            await session.close()
        asyncio.run(run())

    def test_historical_projection_without_runtime_uses_real_file_state_and_reason(self):
        async def run():
            session = await self.session()
            _, item = await self.captured(session)
            await self.deliver(session, item, draft=True)
            await session.close()
        asyncio.run(run())
        self.tasks.set_task_state(self.task.task_id, "completed", result={
            "answer":"TEST old unsupported model sentence", "report_narrative":"",
            "validation":{"acceptance":{"status":"failed", "task_outcome":{
                "status":"failed", "criteria":[{"status":"failed", "description":"TEST actual criterion",
                    "reason":"TEST real reason"}]}}}})
        self.runtime.enabled = False
        self.assertIsNone(getattr(self.tasks, "official_file_projection", None))
        for state in ("publishing", "publication_unknown"):
            self.query("UPDATE qq_official_file_deliveries SET state=?", (state,))
            result = self.tasks.projected_result(self.task.task_id)
            self.assertEqual(result["deliveries"][0]["state"], state)
            self.assertEqual(result["deliveries"][0]["draft_reason"], "TEST incomplete")
            self.assertFalse(result["deliveries"][0]["message_accepted"])
            self.assertNotIn("TEST old unsupported", result["answer"])
            self.assertIn("TEST actual criterion", result["answer"])
        self.assertEqual(self.tasks.get(self.task.task_id).status, "completed")


class FilenameAndImportTests(unittest.TestCase):
    def test_names_are_idempotent_utf8_bounded_and_path_free(self):
        for draft in (True, False):
            for name in ("report.pdf", "测"*500+".zip", "草稿-报告.pdf", "program.exe", "notes.txt"):
                actual = canonical_artifact_filename(name, draft=draft)
                self.assertLessEqual(len(actual.encode()), 180)
                self.assertEqual(canonical_artifact_filename(actual, draft=draft), actual)
        for name in ("/etc/test.pdf", "../test.pdf", "test\\file.pdf", "test\x00.pdf", " test.pdf"):
            with self.assertRaises(ArtifactDeliveryError):
                canonical_artifact_filename(name, draft=True)

    def test_publication_unknown_is_not_pending_or_accepted(self):
        self.assertEqual(delivery_state([{"state":"publishing"}], required=True), "pending")
        self.assertEqual(delivery_state([{"state":"publication_unknown"}], required=True), "publication_unknown")

    def test_entire_factory_imports_without_nonebot(self):
        script = """
import sys, importlib.abc
class Deny(importlib.abc.MetaPathFinder):
 def find_spec(self, fullname, *args):
  if fullname == 'nonebot' or fullname.startswith('nonebot.'):
   raise AssertionError('SDK import')
sys.meta_path.insert(0, Deny())
from src.plugins.ai_chat.adapters.qq_official.file_runtime import OfficialFileRuntime
assert not any(k == 'nonebot' or k.startswith('nonebot.') for k in sys.modules)
"""
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "PYTHON_DOTENV_DISABLED":"1"}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
