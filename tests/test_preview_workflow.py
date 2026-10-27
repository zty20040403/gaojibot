from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import tempfile
import time
import unittest
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from src.cluster_control.execution_service import ClusterExecutionService
from src.cluster_control.execution_storage import ClusterExecutionStore
from src.cluster_control.resource_policy import ResourcePolicyStore
from src.cluster_worker.api import create_app
from src.cluster_worker.service import ClusterWorker
from tests.postgres_fixture import postgres_for_test
from tests import test_cluster_execution as worker_fixture


def site_zip(entries=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, value in entries or [("index.html", "<h1>Preview</h1>")]:
            archive.writestr(name, value)
    return output.getvalue()


class PreviewWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = postgres_for_test(self)
        self.store = ClusterExecutionStore(self.database, self.root)
        self.policies = ResourcePolicyStore(self.database)
        self.service = ClusterExecutionService(self.store,
            inventory=({"host_id": "preview-host", "compute": True},), diagnostic_targets=(),
            worker_hosts={"preview-worker": "preview-host"}, worker_owners={"preview-worker": "qq:2"},
            resource_policies=self.policies)
        self.service.heartbeat("preview-worker", {"boot_id": uuid.uuid4().hex,
            "protocol_version": 1, "availability": "available", "capabilities": ["preview.static"],
            "runtime": {"system": "linux", "machine": "x86_64"},
            "capacity": {"cpu_millis": 2000, "memory_bytes": 2 * 1024**3, "gpu_slots": 0},
            "public_base_url": "http://preview.test:8092"})
        self.artifact = self.upload()
        self.raw = {"kind": "preview.static", "payload": {
            "artifact_id": self.artifact["artifact_id"], "ttl_seconds": 3600},
            "constraints": {"worker_id": "preview-worker", "cpu_millis": 100,
                            "memory_bytes": 32 * 1024**2}, "idempotency_key": "preview-test-1"}

    def upload(self):
        return self.store.store_artifact(actor_id="qq:2", origin_scope="group:test", name="site.zip",
            media_type="application/zip", content_base64=base64.b64encode(site_zip()).decode())

    def submit(self, raw=None, **identity):
        return self.service.submit_job(raw or self.raw,
            **{"actor_id": "qq:2", "origin_scope": "group:test", **identity})

    def execute_sql(self, sql, *args):
        connection = self.database.store_connection()
        try:
            cursor = connection.execute(sql, args)
            rows = cursor.fetchall() if sql.startswith("SELECT") else []
            connection.commit()
            return rows
        finally:
            connection.close()

    def complete(self, job, url=None):
        return self.service.complete_job(job["job_id"], worker_id="preview-worker", fence=job["fence"],
            ok=True, result={"public_url": url or self.url(job)}, error_code="")

    @staticmethod
    def url(job):
        return f"http://preview.test:8092/previews/{job['payload']['preview_id']}/"

    def test_replay_preserves_id_deadline_and_expiry_and_survives_concurrent_submissions(self):
        first = self.submit()
        with patch("src.cluster_control.execution_service.time.time", return_value=time.time() + 45):
            replay = self.submit()
        self.assertEqual(replay["job_id"], first["job_id"])
        self.assertEqual(replay["payload"], first["payload"])
        self.assertEqual(replay["deadline_at"], first["deadline_at"])
        self.raw["idempotency_key"] = "concurrent-preview"
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.submit(), range(4)))
        self.assertEqual(len({item["job_id"] for item in results}), 1)
        self.assertEqual(len(self.store.previews()), 2)
        self.assertEqual(self.execute_sql("SELECT COUNT(*) AS n FROM fleet_job_events")[0]["n"], 2)

    def test_replay_rejects_different_artifact_ttl_constraints_and_explicit_deadline(self):
        first = self.submit()
        other = self.upload()
        changes = [("payload", "artifact_id", other["artifact_id"]),
                   ("payload", "ttl_seconds", 600), ("constraints", "cpu_millis", 200)]
        for section, name, value in changes:
            raw = copy.deepcopy(self.raw)
            raw[section][name] = value
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "different job"):
                self.submit(raw)
        with self.assertRaisesRegex(ValueError, "different job"):
            self.submit({**self.raw, "deadline_at": first["deadline_at"] + 1})
        self.assertEqual(self.submit({**self.raw, "deadline_at": first["deadline_at"]})["job_id"], first["job_id"])

    def test_route_failure_rolls_back_job_event_and_route_together(self):
        original = self.store._ensure_preview

        def fail(cursor, job):
            original(cursor, job)
            raise RuntimeError("crash before commit")

        with patch.object(self.store, "_ensure_preview", side_effect=fail), self.assertRaises(RuntimeError):
            self.submit()
        for table in ("fleet_previews", "fleet_worker_jobs", "fleet_job_events"):
            self.assertEqual(self.execute_sql(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"], 0)
        self.assertTrue(self.submit()["preview_id"])

    def test_upgrade_replay_preserves_old_execution_policy_and_nonterminal_orphan(self):
        original = self.submit()
        policy = {**original["constraints"], "safe_rerun": False, "checkpointable": False}
        self.execute_sql("UPDATE fleet_worker_jobs SET constraints_json=?, status='verifying' WHERE job_id=?",
                         json.dumps(policy), original["job_id"])
        self.execute_sql("DELETE FROM fleet_previews")
        replay = self.submit()
        self.assertEqual(replay["job_id"], original["job_id"])
        self.assertEqual(replay["constraints"], policy)
        self.assertEqual(replay["status"], "verifying")
        self.assertEqual(self.store.preview(original["preview_id"])["state"], "queued")

    def test_orphan_recovery_does_not_resurrect_expired_or_unverified_routes(self):
        first = self.submit()
        claimed = self.service.claim_job("preview-worker")
        self.assertEqual(claimed["job_id"], first["job_id"])
        self.complete(claimed)
        self.execute_sql("DELETE FROM fleet_previews")
        self.submit()
        self.assertEqual(self.store.preview(first["preview_id"])["state"], "active")
        self.execute_sql("DELETE FROM fleet_previews")
        with patch("src.cluster_control.execution_service.time.time", return_value=first["payload"]["expires_at"] + 1):
            self.submit()
        self.assertEqual(self.store.preview(first["preview_id"])["state"], "expired")
        self.execute_sql("DELETE FROM fleet_previews")
        self.execute_sql("UPDATE fleet_worker_jobs SET result_json=? WHERE job_id=?",
                         json.dumps({"public_url": "http://wrong.test/previews/other/"}), first["job_id"])
        self.submit()
        self.assertEqual(self.store.preview(first["preview_id"])["state"], "failed")

    def test_preview_requires_exact_scoped_artifact_and_job_url(self):
        for identity in ({"actor_id": "qq:3"}, {"origin_scope": "group:other"}):
            with self.subTest(identity=identity), self.assertRaises(PermissionError):
                self.submit(**identity)
        submitted = self.submit()
        with self.assertRaises(PermissionError):
            self.service.job_status(submitted["job_id"], actor_id="qq:2", origin_scope="group:other")
        job = self.service.claim_job("preview-worker")
        for url in ("http://evil.test/previews/" + job["payload"]["preview_id"] + "/",
                    "http://preview.test:8092/previews/preview_" + "f" * 32 + "/",
                    self.url(job) + "?secret=1"):
            with self.subTest(url=url), self.assertRaises(PermissionError):
                self.complete(job, url)
        self.assertEqual(self.complete(job)["status"], "succeeded")
        self.assertEqual(self.store.preview(submitted["preview_id"])["health_status"], "published")
        self.assertEqual(self.complete(job)["status"], "succeeded")

    def test_expired_receipt_does_not_activate_route(self):
        self.submit()
        job = self.service.claim_job("preview-worker")
        with patch("src.cluster_control.execution_service.time.time", return_value=job["payload"]["expires_at"]):
            result = self.complete(job)
        self.assertEqual(result["error_code"], "preview_expired")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.store.preview(job["payload"]["preview_id"])["public_url"], "")

    def test_crashed_worker_is_reclaimed_with_same_preview_and_expiry(self):
        self.submit()
        first = self.store.claim_job("preview-worker", lease_seconds=15)
        with patch("src.cluster_control.execution_storage.time.time", return_value=time.time() + 16):
            second = self.store.claim_job("preview-worker", lease_seconds=15)
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(first["payload"], second["payload"])
        self.assertEqual(second["fence"], first["fence"] + 1)
        with self.assertRaisesRegex(PermissionError, "stale worker receipt"):
            self.complete(first)
        self.assertEqual(self.complete(second)["status"], "succeeded")


class PreviewWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.worker = ClusterWorker(worker_fixture.WorkerSafetyTests._settings(self.root))
        self.addAsyncCleanup(self.worker.close)
        self.artifact = self.root / "site.zip"
        self.artifact.write_bytes(site_zip())
        self.payload = {"preview_id": "preview_" + "a" * 32, "expires_at": int(time.time()) + 3600}

    async def test_duplicate_publications_reconcile_without_staging_collision(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.worker._publish_preview(self.artifact, self.payload), range(4)))
        self.assertEqual(len({r["public_url"] for r in results}), 1)
        self.assertEqual(sum(not r.get("reconciled", False) for r in results), 1)
        self.assertEqual(len(list(self.worker.previews.iterdir())), 1)
        for changes in ({"expires_at": self.payload["expires_at"] + 1},):
            with self.assertRaises(ValueError):
                self.worker._publish_preview(self.artifact, {**self.payload, **changes})
        self.artifact.write_bytes(site_zip([("index.html", "different")]))
        with self.assertRaises(ValueError):
            self.worker._publish_preview(self.artifact, self.payload)

    async def test_invalid_lifetime_identity_and_archive_paths_leave_no_publication(self):
        for change in ({"preview_id": "../escape"}, {"expires_at": True},
                       {"expires_at": int(time.time())}, {"expires_at": int(time.time()) + 604900}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.worker._publish_preview(self.artifact, {**self.payload, **change})
        bad_names = ["../out", "/absolute", "a\\file", ".env", "a/.git/config", "a/./file", "a/\x01file"]
        special = zipfile.ZipInfo("symlink")
        special.create_system = 3
        special.external_attr = 0o120777 << 16
        duplicate = [("index.html", "one"), ("folder//file", "one"), ("folder/file", "two")]
        for entries in [[("index.html", "ok"), (name, "bad")] for name in [*bad_names, special]] + [duplicate]:
            self.artifact.write_bytes(site_zip(entries))
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                self.worker._publish_preview(self.artifact, self.payload)
            self.assertEqual(list(self.worker.previews.iterdir()), [])

    async def test_saved_url_from_another_worker_is_not_used_without_local_artifact(self):
        state = {"result": {"public_url": "http://old.test/previews/old/"}}
        checkpoint = {"phase": "completed", "format_version": 1, "executor_version": "worker-v2",
            "state": state, "state_hash": hashlib.sha256(json.dumps(state, ensure_ascii=False,
                separators=(",", ":"), sort_keys=True).encode()).hexdigest()}
        self.worker._download = AsyncMock(return_value=(self.artifact, "application/zip"))
        result = await self.worker._execute({"job_id": "job_" + "a" * 32, "fence": 2,
            "kind": "preview.static", "payload": {**self.payload, "artifact_id": "artifact_" + "b" * 32},
            "resume_checkpoint": checkpoint})
        self.worker._download.assert_awaited_once()
        self.assertEqual(result["public_url"], "http://h610.test:8092/previews/" + self.payload["preview_id"] + "/")
        self.assertIsNotNone(self.worker.preview_file(self.payload["preview_id"], "index.html"))

    async def test_staging_left_by_crash_is_reclaimed_but_live_or_unowned_directories_are_not(self):
        now = int(time.time())
        expired = self.worker.previews / f".{self.payload['preview_id']}.{now - 1}.teststage"
        live = self.worker.previews / f".{self.payload['preview_id']}.{now + 60}.teststage"
        unowned = self.worker.previews / "unrelated"
        for path in (expired, live, unowned):
            path.mkdir()
            (path / "index.html").write_text("staged")
        self.worker._publish_preview(self.artifact, self.payload)
        self.worker._clean_expired_previews()
        self.assertFalse(expired.exists())
        self.assertTrue(live.exists())
        self.assertTrue(unowned.exists())
        self.assertIsNotNone(self.worker.preview_file(self.payload["preview_id"], "index.html"))

    async def test_archive_file_count_limit_and_oversized_declared_member(self):
        self.artifact.write_bytes(site_zip([(f"item{i}", "") for i in range(5001)]))
        with self.assertRaisesRegex(ValueError, "file count"):
            self.worker._publish_preview(self.artifact, self.payload)
        self.artifact.write_bytes(site_zip())
        member = zipfile.ZipInfo("index.html")
        member.file_size = 100 * 1024**2 + 1
        with patch.object(zipfile.ZipFile, "infolist", return_value=[member]), self.assertRaisesRegex(ValueError, "100 MiB"):
            self.worker._publish_preview(self.artifact, self.payload)
        self.assertEqual(list(self.worker.previews.iterdir()), [])

    async def test_preview_http_is_opaque_no_cookie_and_expiry_is_immediate(self):
        self.artifact.write_bytes(site_zip([("index.html", "<h1>Game</h1>"), ("app.js", "export default 1")]))
        self.worker._publish_preview(self.artifact, self.payload)
        app = create_app(self.worker)
        route = "/previews/" + self.payload["preview_id"] + "/"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker.test") as client:
            response = await client.get(route)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertNotIn("set-cookie", response.headers)
            self.assertIn("sandbox allow-scripts;", response.headers["Content-Security-Policy"])
            self.assertNotIn("allow-same-origin", response.headers["Content-Security-Policy"])
            response = await client.get(route + "app.js", headers={"Origin": "null"})
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")
            for path in (".gaoji-preview.json", "%2e%2e%2ftoken", "missing"):
                self.assertEqual((await client.get(route + path)).status_code, 404)
            with patch("src.cluster_worker.service.time.time", return_value=self.payload["expires_at"]):
                self.assertEqual((await client.get(route)).status_code, 404)
                self.worker._clean_expired_previews()
                self.assertEqual(list(self.worker.previews.iterdir()), [])
