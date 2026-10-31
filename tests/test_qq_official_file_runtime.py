from src.plugins.ai_chat.adapters.qq_official.image_formats import canonical_artifact_filename, ImageFormatError as ArtifactDeliveryError
"""Real source/capacity/manifest storage with the existing owned guest contract.

No platform network, credentials, real VM, alternate model loop or ad-hoc DDL.
"""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official.file_runtime import OfficialFileRuntime, GENERATION_TOOLS
from src.plugins.ai_chat.execution_control import LeaseLost
from src.plugins.ai_chat.application.owned_sandbox import ArtifactAvailability, GenerationError
from src.plugins.ai_chat.platform_types import InboundMessage, PlatformFeatures
from src.plugins.ai_chat.tool_invocation import active_tool_call_id
from src.plugins.ai_chat.tools import neutral_registry_for
from src.plugins.ai_chat.tools.state import ToolContext
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
            sid, _ = await self.produce(session)
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
            await session.close()
            self.assertEqual(len(session.executor.retained_resources), 1)
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
        self.assertEqual(self.query("SELECT * FROM qq_official_generation_capacity"), [])

    def test_lost_source_close_never_destroys_or_refunds(self):
        async def run():
            session = await self.foreground()
            await self.produce(session)
            self.admitted = False
            await session.close()
            self.assertEqual(len(session.executor.retained_resources), 1)
        asyncio.run(run())
        self.assertFalse(any(x[0] == "destroy" for x in self.manager.calls))
        self.assertEqual(self.query("SELECT * FROM qq_official_generation_capacity"), [])

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
            await session.close()
            self.assertEqual(len(session.executor.retained_resources), 1)
        asyncio.run(run())
        self.assertEqual(self.query("SELECT * FROM qq_official_generation_capacity"), [])





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
