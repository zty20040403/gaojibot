"""Real PostgreSQL and authenticated HTTP; no bot, model or runner requests."""

from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
import nonebot
from fastapi import FastAPI

nonebot.init()

from src.bot_security.passwords import hash_password
from src.plugins.ai_chat.admin import AdminServices, _changed_database_resources
from src.plugins.ai_chat.admin_control import AdminControlStore, AdminVersionConflict
from src.plugins.ai_chat.project_admin import ProjectAdmin
from src.plugins.ai_chat.projects import ProjectConflict
from tests.admin_session_fixture import PASSWORD, register_admin
from tests import test_project_store as fixture


class ProjectAdminTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.ProjectStoreTests.setUp
    owned = fixture.ProjectStoreTests.owned
    new_store = fixture.ProjectStoreTests.new_store
    create = fixture.ProjectStoreTests.create
    snapshot = fixture.ProjectStoreTests.snapshot

    async def asyncSetUp(self):
        self.pid = self.create()
        self.control = AdminControlStore(self.db)
        self.admin = ProjectAdmin(self.store, self.control)
        self.runtime = SimpleNamespace(store=self.store, ready=True, closed=False)
        self.app = FastAPI()
        register_admin(self.app, AdminServices(version="test", started_at=1, database=self.db,
            project_runtime=self.runtime, settings=SimpleNamespace(admin_origin="https://test")), test_case=self)
        security = self.app.state.mobile.store
        account = security.account_for_qq("123456789")
        security.apply_account_change(account, {"action": "create", "username": "viewer",
            "password_hash": hash_password(PASSWORD), "role": "member", "qq_id": None})
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
            base_url="https://test", headers={"Origin": "https://test"})
        self.addAsyncCleanup(self.client.aclose)

    async def login(self, username="kenneth"):
        response = await self.client.post("/bot-admin/api/v1/auth/login", json={"username": username, "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        self.client.headers["X-CSRF-Token"] = self.client.cookies["gaoji_csrf"]

    async def change(self, action="pin", body=None, version=0, method="PUT", pid=None, **headers):
        return await self.client.request(method, f"/bot-admin/api/v1/projects/{pid or self.pid}/{action}",
            json=body or {"expected_version": 1, "pinned": True}, headers={"If-Match": f'"{version}"', **headers})

    def materialized(self, *, sampled_at=None, used=4096):
        self.sql.execute("""UPDATE projects SET workspace_generation=1,lifecycle_fence=3,sampled_at=?,
            used_bytes=?,rebuildable_bytes=1024,status='stopped' WHERE project_id=?""", (sampled_at, used, self.pid))
        self.sql.execute("""INSERT INTO project_usage(project_id,generation,fence,checked_at,complete,sample,error)
            VALUES (?,1,3,?,TRUE,?::jsonb,'')""",
            (self.pid, self.now, json.dumps({"quota_bytes": 2 * 1024**3, "rebuildable_bytes": 1024})))

    async def test_anonymous_and_member_are_rejected_on_all_aliases(self):
        for prefix in ("/bot-admin/api", "/bot-admin/api/v1"):
            self.assertEqual((await self.client.get(prefix + "/projects")).status_code, 401)
        await self.login("viewer")
        for path in ("/projects", f"/projects/{self.pid}"):
            self.assertEqual((await self.client.get("/bot-admin/api/v1" + path)).status_code, 403)
        self.assertEqual((await self.change()).status_code, 403)

    async def test_real_login_pin_version_guard_and_trusted_audit_actor(self):
        await self.login()
        response = await self.change(**{"X-Admin-Actor": "forged"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["resource_version"], 1)
        self.assertTrue(self.store.get(self.access, self.pid)["pinned"])
        self.assertEqual((await self.change()).status_code, 409)
        self.assertEqual((await self.change(version=1)).status_code, 409)
        self.assertEqual(self.control.version("projects"), 1)
        records = self.control.audit()
        self.assertEqual(len(records), 1)
        self.assertNotIn("forged", records[0]["actor"])
        self.assertEqual(records[0]["action"], "project.pin")
        self.assertEqual(self.app.state.auth_messages, [])

    async def test_csrf_origin_and_missing_resource_version(self):
        await self.login()
        path = f"/bot-admin/api/v1/projects/{self.pid}/pin"
        body = {"expected_version": 1, "pinned": True}
        self.assertEqual((await self.client.put(path, json=body)).status_code, 428)
        self.assertEqual((await self.change(**{"X-CSRF-Token": "invalid"})).status_code, 403)
        self.assertEqual((await self.change(Origin="https://foreign.invalid")).status_code, 403)
        self.assertFalse(self.store.get(self.access, self.pid)["pinned"])

    async def test_paging_search_details_do_not_expose_private_state(self):
        for index in range(4):
            self.store.create(self.access, f"Project {index}")
        self.snapshot(self.pid)
        self.sql.execute("INSERT INTO project_events(project_id,kind,payload,created_at) VALUES (?,'fixture',?::jsonb,?)",
                         (self.pid, json.dumps({"reservation": "private-token", "page": "private-browser"}), self.now))
        await self.login()
        listing = (await self.client.get("/bot-admin/api/v1/projects?offset=3&limit=2")).json()
        self.assertEqual((listing["total"], len(listing["items"])), (5, 2))
        self.assertEqual((await self.client.get("/bot-admin/api/v1/projects?query=Project%202")).json()["total"], 1)
        self.assertEqual((await self.client.get("/bot-admin/api/v1/projects?state=archived")).json()["total"], 0)
        detail = (await self.client.get(f"/bot-admin/api/v1/projects/{self.pid}?section=events")).json()
        serialized = json.dumps(detail)
        for hidden in ("private-token", "private-browser", "storage_path", "rebuild_command", "manifest_sha256"):
            self.assertNotIn(hidden, serialized)
        self.assertEqual(detail["scope_limits"]["project_count"], 5)
        self.assertGreater(detail["project"]["version_bytes"], 0)

    async def test_stale_usage_is_unknown_not_zero_and_does_not_claim_resize(self):
        self.materialized(sampled_at=self.now - 3600)
        await self.login()
        result = (await self.client.get(f"/bot-admin/api/v1/projects/{self.pid}")).json()["project"]
        self.assertFalse(result["usage_fresh"])
        self.assertIsNone(result["used_bytes"])
        self.assertEqual(result["last_known_used_bytes"], 4096)
        self.assertFalse(result["quota_editable"])
        response = await self.change("quota", {"expected_version": 1, "quota_bytes": 3 * 1024**3})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.get(self.access, self.pid)["quota_bytes"], 2 * 1024**3)

    async def test_fresh_usage_displays_measured_bytes(self):
        self.materialized(sampled_at=self.now)
        await self.login()
        value = (await self.client.get(f"/bot-admin/api/v1/projects/{self.pid}")).json()["project"]
        self.assertTrue(value["usage_fresh"])
        self.assertEqual((value["used_bytes"], value["rebuildable_bytes"]), (4096, 1024))

    async def test_initial_quota_and_scope_limits_apply_without_host_effects(self):
        await self.login()
        response = await self.change("quota", {"expected_version": 1, "quota_bytes": 3 * 1024**3})
        self.assertEqual(response.status_code, 200, response.text)
        response = await self.change("scope-limits", {"expected_version": 2, "scope_version": 1,
            "quota_bytes": 6 * 1024**3, "max_projects": 8}, version=1)
        self.assertEqual(response.status_code, 200, response.text)
        detail = self.admin.detail(self.pid)
        self.assertEqual(detail["scope_limits"]["version"], 2)
        self.assertEqual(detail["scope_limits"]["quota_bytes"], 6 * 1024**3)
        self.assertEqual(detail["project"]["version"], 3)
        self.assertEqual(self.app.state.auth_messages, [])

    async def test_invalid_limits_and_shrink_below_retained_or_unverified_work_rejected(self):
        await self.login()
        for value in (-1, 0, True, "123", 2**60, 67108865, 101 * 1024**3):
            self.assertEqual((await self.change("quota", {"expected_version": 1, "quota_bytes": value})).status_code, 422)
        self.assertEqual((await self.change("quota", {"expected_version": 1, "quota_bytes": 6 * 1024**3})).status_code, 409)
        self.materialized(sampled_at=self.now - 3600, used=1024**3)
        response = await self.change("scope-limits", {"expected_version": 1, "scope_version": 1,
            "quota_bytes": 2 * 1024**3, "max_projects": 5})
        self.assertEqual(response.status_code, 409)
        self.assertIn("核实", response.json()["detail"])
        self.assertEqual(self.control.version("projects"), 0)

    async def test_version_pin_and_scope_bound_preview_revocation(self):
        self.snapshot(self.pid)
        other = self.create()
        self.snapshot(other)
        token = "test-preview-" + "a" * 22
        self.sql.execute("""INSERT INTO project_previews(preview_id,project_id,version_no,root,expires_at,created_at)
            VALUES (?,?,1,'site',?,?)""", (token, other, self.now + 1000, self.now))
        await self.login()
        response = await self.change("versions/1/pin", {"expected_version": 2, "pinned": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(self.store.versions(self.access, self.pid)[0]["pinned"])
        wrong = await self.change(f"previews/{token}/revoke", {"expected_version": 3}, version=1, method="POST")
        self.assertEqual(wrong.status_code, 404)
        self.assertEqual(self.control.version("projects"), 1)
        response = await self.change(f"previews/{token}/revoke", {"expected_version": 2}, version=1, method="POST", pid=other)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNotNone(self.admin.detail(other, section="previews")["items"][0]["revoked_at"])

    async def test_not_ready_is_explicit_and_list_filters_are_bounded(self):
        await self.login()
        for suffix in ("?limit=101", "?offset=-1", "?state=imaginary", "/0", "/1?section=files"):
            self.assertEqual((await self.client.get("/bot-admin/api/v1/projects" + suffix)).status_code, 422)
        self.runtime.closed = True
        self.assertFalse((await self.client.get("/bot-admin/api/v1/projects")).json()["available"])
        self.assertEqual((await self.client.get(f"/bot-admin/api/v1/projects/{self.pid}")).status_code, 503)
        self.assertEqual((await self.change()).status_code, 503)

    def test_audit_failure_rolls_back_project_event_and_resource_revision(self):
        original = self.control.mutate_transaction
        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("fixture lost transaction before commit")
        with patch.object(self.control, "mutate_transaction", side_effect=fail):
            with self.assertRaises(OSError):
                self.admin.mutate(self.pid, action="pin", expected_version=1, resource_version=0,
                                  actor="fixture", values={"pinned": True})
        self.assertFalse(self.store.get(self.access, self.pid)["pinned"])
        self.assertEqual(self.control.version("projects"), 0)
        self.assertEqual(self.control.audit(), [])
        self.assertFalse(any(event["kind"] == "admin_pin" for event in self.store.events(self.access, self.pid)))

    def test_concurrent_connections_cannot_overwrite_same_revision(self):
        second = ProjectAdmin(self.new_store(), AdminControlStore(self.db))
        def mutate(admin):
            try:
                admin.mutate(self.pid, action="pin", expected_version=1, resource_version=0,
                             actor="fixture", values={"pinned": True})
                return "saved"
            except (AdminVersionConflict, ProjectConflict):
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(mutate, [self.admin, second]))
        self.assertCountEqual(results, ["saved", "conflict"])
        self.assertEqual(len(self.control.audit()), 1)

    def test_realtime_maps_each_project_metadata_source(self):
        for table in ("projects", "project_versions", "project_previews", "project_usage", "project_events",
                      "project_scope_limits", "project_lifecycle_runs", "project_io", "durable_jobs"):
            self.assertIn("projects", _changed_database_resources({table: (1, 0, 0)}, {table: (1, 1, 0)}))
