"""Real admin/PG leases + host/guest journals; libvirt and devices are fixtures."""

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import json
import os
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from alembic import command
from alembic.config import Config
import httpx
import nonebot

nonebot.init()

from src.bot_storage import open_store_connection
from src.plugins.ai_chat.admin import AdminServices
from src.plugins.ai_chat.admin_control import AdminControlStore
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.project_admin import ProjectAdmin
from src.plugins.ai_chat.project_executor import ProjectLifecycleExecutor, ProjectBackendUnavailable
from src.plugins.ai_chat.project_lifecycle import KIND, ProjectLifecycle
from src.plugins.ai_chat.project_quota import QuotaAuthority
from src.plugins.ai_chat.projects import ProjectAccess, ProjectConflict, ProjectStore
from src.plugins.ai_chat.project_vm_executor import VmProjectBackend
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from src.sandbox_runtime.project_vm_runtime import VmProjectRuntime
from src.sandbox_runtime.project_vm_gate import GuestDiskError
from src.sandbox_runtime.errors import SandboxError
from tests.admin_session_fixture import PASSWORD, register_admin
from tests.postgres_fixture import postgres_for_test
from tests.test_project_vm_provision import RESERVATION
from tests.test_project_vm_resize import VmResizeFixture, MIB


class ProjectQuotaTests(VmResizeFixture, unittest.IsolatedAsyncioTestCase):
    owner = "onebot-v11:group:101"

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.manager.project_mounts.reserve("release", self.owner, 1,
            generation=1, fence=4, reservation=RESERVATION)
        self.db = postgres_for_test(self)
        self.projects = ProjectStore(self.db, self.root / "blobs")
        self.jobs = DurableJobStore(self.db, lease_seconds=60)
        ledger = MessageLedger(self.db)
        for store in (self.projects, self.jobs, ledger):
            self.addCleanup(store.close)
        principal = ledger.ensure_principal_identity("onebot-v11", "201", "A")
        self.access = ProjectAccess(ConversationScope("onebot-v11", "group", "101", "201", "301"), principal, member=True)
        self.pid = self.projects.create(self.access, "Quota", backend="vm", quota_bytes=64 * MIB)["project_id"]
        self.assertEqual(self.pid, 1)
        _, self.sql = open_store_connection(self.db)
        self.addCleanup(self.sql.close)
        self.sql.execute("""UPDATE projects SET workspace_generation=1,lifecycle_fence=5,status='active',sandbox_id=?
            WHERE project_id=?""", (self.record()["sandbox_id"], self.pid))
        self.control = AdminControlStore(self.db)
        self.compose()
        self.app = FastAPI()
        runtime = SimpleNamespace(store=self.projects, lifecycle=self.lifecycle,
            backends={"vm": self.backend}, ready=True, closed=False)
        register_admin(self.app, AdminServices(version="test", started_at=1, database=self.db,
            project_runtime=runtime, settings=SimpleNamespace(admin_origin="https://test")), test_case=self)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
            base_url="https://test", headers={"Origin": "https://test"})
        self.addAsyncCleanup(self.client.aclose)
        response = await self.client.post("/bot-admin/api/v1/auth/login", json={"username": "kenneth", "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        self.client.headers["X-CSRF-Token"] = self.client.cookies["gaoji_csrf"]
        self.effects.clear()

    def compose(self):
        self.lifecycle = ProjectLifecycle(self.projects, self.jobs)
        self.runtime = VmProjectRuntime(self.manager, self.store, host=self.host)
        self.manager.project_mounts = self.runtime.mounts
        self.backend = VmProjectBackend(self.runtime, files=None)
        self.resolve_access = Mock(side_effect=AssertionError("Do not impersonate a QQ member"))
        self.executor = ProjectLifecycleExecutor(self.lifecycle, {"vm": self.backend}, resolve_access=self.resolve_access)
        self.admin = ProjectAdmin(self.projects, self.control, lifecycle=self.lifecycle, backends={"vm": self.backend})

    async def virsh(self, *args, **kwargs):
        if args[0] == "shutdown":
            self.domains[args[1]]["state"] = "shut off"
            self.done("shutdown")
            return "shutdown"
        return await super().virsh(*args, **kwargs)

    def project(self):
        return self.projects.get(self.access, self.pid)

    async def request(self, target=96):
        response = await self.client.put(f"/bot-admin/api/v1/projects/{self.pid}/quota",
            json={"expected_version": self.project()["version"], "quota_bytes": target * MIB},
            headers={"If-Match": f'"{self.control.version("projects")}"', "X-Admin-Actor": "forged"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["change"]

    def claim(self, **kwargs):
        return self.jobs.claim_due("quota-worker", kinds=(KIND,), **kwargs)[0]

    async def test_http_to_real_host_guest_resize_then_atomic_publication(self):
        before = self.project()
        result = await self.request()
        self.assertEqual(result["status"], "pending")
        self.assertEqual(self.effects, [])
        self.assertEqual((self.project()["quota_bytes"], self.project()["version"]), (64 * MIB, before["version"] + 1))
        detail = self.admin.detail(self.pid)
        self.assertFalse(detail["project"]["quota_editable"])
        self.assertEqual(detail["operation"]["quota_bytes"], 96 * MIB)
        self.assertNotIn("reservation", json.dumps(detail))
        job = self.claim()
        authority = self.lifecycle.quota_authority(job)
        self.assertIsInstance(authority, QuotaAuthority)
        self.assertNotIn("forged", authority.actor)
        run = self.lifecycle.context(job)[1]
        self.assertIsNone(run["actor_principal_id"])
        completed = await self.executor(job)
        self.assertEqual(completed["quota_bytes"], 96 * MIB)
        self.assertEqual(self.guest_disk.geometry, 96 * MIB)
        project = self.project()
        self.assertEqual(project["quota_bytes"], self.record()["quota_bytes"])
        self.assertEqual(project["sandbox_id"], before["sandbox_id"])
        self.assertIsNone(project["lifecycle_job_id"])
        self.assertIsNone(project["sampled_at"])
        self.assertEqual(project["state_changed_at"], before["state_changed_at"])
        self.resolve_access.assert_not_called()

    async def test_lost_resize_receipt_worker_takeover_does_not_repeat_effect(self):
        await self.request()
        job = self.claim()
        original = self.backend.execute

        async def lose(job, step, access):
            result = await original(job, step, access)
            if step["operation"] == "resize_volume":
                raise RuntimeError("lost resize receipt")
            return result

        with patch.object(self.backend, "execute", side_effect=lose):
            with self.assertRaisesRegex(RuntimeError, "lost resize receipt"):
                await self.executor(job)
        self.assertEqual(self.project()["quota_bytes"], 64 * MIB)
        self.assertEqual(self.record()["quota_bytes"], 96 * MIB)
        self.sql.execute("UPDATE durable_jobs SET lease_until=? WHERE job_id=?", (int(time.time()) - 1, job.job_id))
        self.restart()
        self.compose()
        recovered = self.claim()
        self.assertGreater(recovered.lease_generation, job.lease_generation)
        with self.assertRaises(ProjectConflict):
            self.lifecycle.next_step(job)
        await self.executor(recovered)
        self.assertEqual(self.effects.count("blockresize"), 1)
        self.assertEqual(self.project()["quota_bytes"], 96 * MIB)

    async def test_online_grow_and_offline_shrink_keep_original_vm_and_state(self):
        await self.request()
        await self.executor(self.claim())
        self.effects.clear()
        await self.request(64)
        await self.executor(self.claim())
        self.assertLess(self.effects.index("guest-apply"), self.effects.index("blockresize"))
        self.assertEqual(self.project()["status"], "active")
        self.assertEqual(self.gate()._state()["mode"], "active")

    async def test_stopped_workspace_is_stopped_again_after_resize(self):
        domain = self.record()["workspace"]["domain_uuid"]
        self.domains[domain]["state"] = "shut off"
        self.sql.execute("UPDATE projects SET status='stopped' WHERE project_id=?", (self.pid,))
        await self.request()
        await self.executor(self.claim())
        self.assertEqual(self.domains[domain]["state"], "shut off")
        self.assertEqual(self.project()["status"], "stopped")

    async def test_revoked_grant_blocks_dispatch_and_cannot_be_replaced_by_member(self):
        await self.request()
        job = self.claim()
        with self.assertRaises(PermissionError):
            self.lifecycle.context(job, access=self.access)
        self.sql.execute("UPDATE project_quota_authorizations SET revoked_at=?", (int(time.time()),))
        with self.assertRaises(PermissionError):
            await self.executor(job)
        self.assertEqual(self.effects, [])
        self.assertEqual(self.project()["quota_bytes"], 64 * MIB)

    async def test_changed_bot_and_audit_fail_closed(self):
        await self.request()
        job = self.claim()
        self.sql.execute("UPDATE projects SET bot_native_user_id='other' WHERE project_id=?", (self.pid,))
        with self.assertRaises(PermissionError):
            self.lifecycle.context(job)
        self.sql.execute("UPDATE projects SET bot_native_user_id='301' WHERE project_id=?", (self.pid,))
        self.sql.execute("DELETE FROM admin_audit_log")
        with self.assertRaisesRegex(PermissionError, "audit"):
            self.lifecycle.context(job)

    async def test_changed_target_or_steps_not_authorized(self):
        await self.request()
        job = self.claim()
        original = self.lifecycle.context(job)[1]["plan"]
        for corrupt in ({**original, "resize": {"source_state": "active", "quota_bytes": 128 * MIB}},
                        {**original, "steps": original["steps"] + ["delete_volume"]}):
            self.sql.execute("UPDATE project_lifecycle_runs SET plan=?::jsonb WHERE job_id=?", (json.dumps(corrupt), job.job_id))
            with self.assertRaises(PermissionError):
                self.lifecycle.context(job)
        self.assertEqual(self.effects, [])

    async def test_no_database_completion_from_a_false_capacity_receipt(self):
        await self.request()
        job = self.claim()
        while True:
            step = self.lifecycle.next_step(job)
            receipt = await self.backend.execute(job, step, self.lifecycle.quota_authority(job))
            if step["operation"] == "resize_volume":
                receipt["quota_bytes"] = 128 * MIB
                with self.assertRaisesRegex(ProjectConflict, "capacity"):
                    self.lifecycle.acknowledge(job, step, receipt)
                break
            self.lifecycle.acknowledge(job, step, receipt)
        with self.assertRaises(ProjectConflict):
            self.lifecycle.finish(job)
        self.assertEqual(self.project()["quota_bytes"], 64 * MIB)

    async def test_duplicate_http_request_cannot_queue_second_resize(self):
        await self.request()
        second = await self.client.put(f"/bot-admin/api/v1/projects/{self.pid}/quota",
            json={"expected_version": 1, "quota_bytes": 96 * MIB}, headers={"If-Match": '"0"'})
        self.assertEqual(second.status_code, 409)
        self.assertEqual(self.sql.execute("SELECT count(*) FROM project_lifecycle_runs").fetchone()[0], 1)

    async def test_member_request_cannot_invoke_admin_action(self):
        with self.assertRaises(ValueError):
            self.lifecycle.request(replace(self.access, admin=True), self.pid, "resize",
                expected_version=1, request_key="forged")

    async def test_unavailable_backend_rolls_back_without_queue_or_audit(self):
        with patch.object(self.backend, "preflight", side_effect=ProjectBackendUnavailable("disabled")):
            with self.assertRaises(ProjectBackendUnavailable):
                self.admin.mutate(self.pid, action="quota", expected_version=1, resource_version=0,
                    actor="administrator", values={"quota_bytes": 96 * MIB})
        self.assertEqual(self.project()["version"], 1)
        self.assertEqual(self.control.version("projects"), 0)
        self.assertEqual(self.sql.execute("SELECT count(*) FROM durable_jobs").fetchone()[0], 0)

    async def test_failed_audit_rolls_back_job_authority_and_project_lock(self):
        def reject_json(value):
            raise RuntimeError("audit serialization failed")

        with patch("src.plugins.ai_chat.admin_control._encode_json", side_effect=reject_json):
            with self.assertRaisesRegex(RuntimeError, "audit serialization"):
                self.admin.mutate(self.pid, action="quota", expected_version=1, resource_version=0,
                    actor="administrator", values={"quota_bytes": 96 * MIB})
        for table in ("durable_jobs", "project_lifecycle_runs", "project_quota_authorizations", "admin_audit_log"):
            self.assertEqual(self.sql.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
        self.assertIsNone(self.project()["lifecycle_job_id"])
        self.assertEqual(self.project()["version"], 1)

    async def test_quota_smaller_than_physical_usage_keeps_lock_and_original_capacity(self):
        await self.request()
        await self.executor(self.claim())
        self.guest_disk.available = 0
        await self.request(64)
        job = self.claim()
        with self.assertRaisesRegex((SandboxError, GuestDiskError), "usage|used|shrink|space"):
            await self.executor(job)
        self.assertEqual(self.project()["quota_bytes"], 96 * MIB)
        self.assertEqual(self.record()["quota_bytes"], 96 * MIB)
        self.assertEqual(self.project()["lifecycle_job_id"], job.job_id)
        self.assertTrue(self.record()["maintenance"])

    async def test_authority_revoked_after_physical_effect_does_not_publish_success(self):
        await self.request()
        job = self.claim()
        original = self.backend.execute

        async def revoke_after_resize(job, step, access):
            result = await original(job, step, access)
            if step["operation"] == "resize_volume":
                self.sql.execute("UPDATE project_quota_authorizations SET revoked_at=?", (int(time.time()),))
            return result

        with patch.object(self.backend, "execute", side_effect=revoke_after_resize):
            with self.assertRaises(PermissionError):
                await self.executor(job)
        self.assertEqual(self.project()["quota_bytes"], 64 * MIB)
        self.assertEqual(self.record()["quota_bytes"], 96 * MIB)
        self.assertIsNotNone(self.project()["lifecycle_job_id"])
        self.assertEqual(self.gate()._state()["mode"], "quiesced")

    async def test_quota_journal_prevents_schema_downgrade(self):
        result = await self.request()
        with patch.dict(os.environ, {"AI_POSTGRES_DSN": os.environ["TEST_POSTGRES_DSN"],
                                     "AI_POSTGRES_SCHEMA": self.db.schema}):
            with self.assertRaisesRegex(RuntimeError, "quota lifecycle journals"):
                command.downgrade(Config("alembic.ini"), "0059_project_task_prepare")
        self.assertEqual(self.project()["lifecycle_job_id"], result["job_id"])
        self.assertEqual(self.sql.execute("SELECT count(*) FROM project_quota_authorizations").fetchone()[0], 1)

    async def test_concurrent_administrators_cannot_queue_conflicting_targets(self):
        from src.plugins.ai_chat.admin_control import AdminVersionConflict

        def request(quota):
            try:
                return self.admin.mutate(self.pid, action="quota", expected_version=1, resource_version=0,
                    actor="administrator", values={"quota_bytes": quota * MIB}).value
            except (AdminVersionConflict, ProjectConflict):
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(request, (96, 128)))
        accepted = [value for value in results if value is not None]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(self.sql.execute("SELECT count(*) FROM project_quota_authorizations").fetchone()[0], 1)
        self.assertEqual(self.project()["quota_bytes"], 64 * MIB)
        self.assertEqual((await self.executor(self.claim()))["quota_bytes"], accepted[0]["quota_bytes"])
