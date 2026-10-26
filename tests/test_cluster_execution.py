from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

import httpx
import nonebot

nonebot.init()

from src.cluster_control.api import create_app
from src.cluster_control.execution_contracts import OperationProposal, WorkerJobProposal
from src.cluster_control.execution_service import (
    ClusterExecutionService,
    WorkerAuthenticator,
)
from src.cluster_worker.config import WorkerSettings
from src.cluster_worker.service import ClusterWorker
from src.plugins.ai_chat.fleet_client import FleetControlClient


class _Fleet:
    metrics_registry = None

    async def close(self) -> None:
        return None


class _ExecutionStore:
    def __init__(self) -> None:
        self.operation: dict | None = None
        self.approval: dict | None = None

    def prepare_operation(self, record: dict) -> dict:
        self.operation = dict(record)
        return dict(record)

    def approve_operation(self, operation_id: str, **kwargs: object) -> dict:
        self.approval = {"operation_id": operation_id, **kwargs}
        return {"operation_id": operation_id, "status": "queued"}


class _Execution:
    def __init__(self) -> None:
        self.store = _ExecutionStore()

    def prepare_operation(self, raw: dict, *, actor_id: str, origin_scope: str) -> dict:
        return {"actor_id": actor_id, "origin_scope": origin_scope, **raw}


class _ScopedArtifactStore:
    def __init__(self, actor_id: str, origin_scope: str) -> None:
        self.actor_id = actor_id
        self.origin_scope = origin_scope
        self.submitted = False

    def artifact(self, _artifact_id: str) -> dict:
        return {"actor_id": self.actor_id, "origin_scope": self.origin_scope}

    def submit_job(self, record: dict) -> dict:
        self.submitted = True
        return record


class ClusterContractTests(unittest.TestCase):
    def test_operation_contract_accepts_only_exact_service_actions(self) -> None:
        proposal = OperationProposal.parse(
            {
                "host_id": "h610",
                "resource_ref": "gaoji.service",
                "operation": "service.restart",
                "idempotency_key": "restart-0001",
            },
            now=100,
        )
        self.assertEqual(proposal.resource_ref, "gaoji.service")
        with self.assertRaises(ValueError):
            OperationProposal.parse(
                {
                    "host_id": "h610",
                    "resource_ref": "*.service",
                    "operation": "shell.exec",
                    "idempotency_key": "restart-0002",
                },
                now=100,
            )

    def test_worker_job_rejects_unregistered_probe_urls(self) -> None:
        with self.assertRaises(ValueError):
            WorkerJobProposal.parse(
                {
                    "kind": "probe.http",
                    "payload": {"url": "http://metadata.internal"},
                    "idempotency_key": "probe-0001",
                }
            )

    def test_worker_job_cannot_use_another_scope_artifact(self) -> None:
        store = _ScopedArtifactStore("qq:other", "onebot-v11:group:999")
        service = ClusterExecutionService(
            store,  # type: ignore[arg-type]
            inventory=(), diagnostic_targets=(), worker_hosts={},
        )
        with self.assertRaises(PermissionError):
            service.submit_job(
                {
                    "kind": "artifact.inspect",
                    "payload": {"artifact_id": "artifact_" + "a" * 32},
                    "idempotency_key": "artifact-0001",
                },
                actor_id="qq:123",
                origin_scope="onebot-v11:group:456",
            )
        self.assertFalse(store.submitted)

    def test_guardian_operation_consumes_its_preapproval(self) -> None:
        store = _ExecutionStore()
        service = ClusterExecutionService(
            store,  # type: ignore[arg-type]
            inventory=(
                {
                    "host_id": "h610",
                    "operate": True,
                    "operable_units": ["nginx.service"],
                },
            ),
            diagnostic_targets=(),
            worker_hosts={},
            write_backend=type(
                "Backend",
                (),
                {
                    "available": True,
                    "backend_ref": "test",
                    "binding_version": 1,
                    "reason": "",
                },
            )(),
        )
        result = service.submit_guardian_operation(
            {
                "host_id": "h610",
                "resource_ref": "nginx.service",
                "operation": "service.restart",
                "deadline_at": int(time.time()) + 300,
                "idempotency_key": "guardian:test:1",
            },
            actor_id="admin:kenneth",
            origin_scope="admin-console",
        )
        self.assertEqual(result["status"], "queued")
        self.assertIsNotNone(store.approval)

    def test_unavailable_guardian_action_does_not_leave_a_stale_operation(self) -> None:
        store = _ExecutionStore()
        service = ClusterExecutionService(
            store,  # type: ignore[arg-type]
            inventory=(
                {
                    "host_id": "h610",
                    "operate": True,
                    "operable_units": ["nginx.service"],
                },
            ),
            diagnostic_targets=(),
            worker_hosts={},
        )
        result = service.submit_guardian_operation(
            {
                "host_id": "h610",
                "resource_ref": "nginx.service",
                "operation": "service.restart",
                "deadline_at": int(time.time()) + 300,
                "idempotency_key": "guardian:test:unavailable",
            },
            actor_id="admin:kenneth",
            origin_scope="admin-console",
        )
        self.assertEqual(result["capability_status"], "not_configured")
        self.assertIsNone(store.operation)


class SignedActorApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_signature_binds_actor_and_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token"
            token.write_text("x" * 48, encoding="ascii")
            execution = _Execution()
            app = create_app(
                _Fleet(), api_token_file=token, execution=execution  # type: ignore[arg-type]
            )
            client = FleetControlClient(
                "http://control.test", token,
                transport=httpx.ASGITransport(app=app),
            )
            try:
                result = await client._raw_request(
                    "POST", "/v1/operations/prepare",
                    {
                        "host_id": "h610",
                        "resource_ref": "gaoji.service",
                        "operation": "service.restart",
                        "idempotency_key": "restart-0001",
                    },
                    actor="qq:123", origin="onebot-v11:group:456",
                )
            finally:
                await client.close()
            self.assertEqual(result["actor_id"], "qq:123")
            self.assertEqual(result["origin_scope"], "onebot-v11:group:456")

    async def test_unsigned_prepare_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token"
            token.write_text("y" * 48, encoding="ascii")
            app = create_app(
                _Fleet(), api_token_file=token, execution=_Execution()  # type: ignore[arg-type]
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://control.test"
            ) as client:
                response = await client.post(
                    "/v1/operations/prepare",
                    headers={"Authorization": f"Bearer {'y' * 48}"},
                    json={
                        "host_id": "h610",
                        "resource_ref": "gaoji.service",
                        "operation": "service.restart",
                        "idempotency_key": "restart-0001",
                    },
                )
            self.assertEqual(response.status_code, 401)


class WorkerSafetyTests(unittest.TestCase):
    @staticmethod
    def _settings(root: Path) -> WorkerSettings:
        token = root / "token"
        token.write_text("z" * 48, encoding="ascii")
        return WorkerSettings(
            worker_id="h610-worker",
            token_file=token,
            control_url="http://127.0.0.1:8091",
            listen_host="127.0.0.1",
            listen_port=8092,
            public_base_url="http://h610.test:8092",
            state_dir=root / "state",
            cpu_millis=2000,
            memory_bytes=2 * 1024**3,
            gpu_slots=0,
            concurrency=2,
        )

    def test_preview_publish_is_idempotent_and_serves_only_its_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = ClusterWorker(self._settings(root))
            archive = root / "site.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("index.html", "<h1>gaoji</h1>")
                output.writestr("assets/app.css", "body{}")
            payload = {
                "preview_id": "preview_" + "a" * 32,
                "ttl_seconds": 3600,
                "expires_at": int(time.time()) + 3600,
            }
            first = worker._publish_preview(archive, payload)
            second = worker._publish_preview(archive, payload)
            self.assertFalse(first.get("reconciled", False))
            self.assertTrue(second["reconciled"])
            self.assertIsNotNone(worker.preview_file(payload["preview_id"], "index.html"))
            self.assertIsNone(worker.preview_file(payload["preview_id"], "../../token"))
            root = worker.previews / payload["preview_id"]
            (root / ".gaoji-preview.json").rename(root / ".kennethbot-preview.json")
            self.assertIsNotNone(worker.preview_file(payload["preview_id"], "index.html"))
            self.assertIsNone(worker.preview_file(payload["preview_id"], ".kennethbot-preview.json"))
            self.assertTrue(worker._publish_preview(archive, payload)["reconciled"])
            asyncio.run(worker.client.close())

    def test_preview_accepts_one_wrapper_directory_and_preserves_asset_routes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = ClusterWorker(self._settings(root))
            self.addCleanup(lambda: asyncio.run(worker.client.close()))
            archive = root / "site.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("server-status-snapshot/", "")
                output.writestr("server-status-snapshot/index.html", "<h1>Status</h1>")
                output.writestr("server-status-snapshot/assets/app.css", "body{}")
            payload = {"preview_id": "preview_" + "d" * 32, "expires_at": int(time.time()) + 3600}
            first = worker._publish_preview(archive, payload)
            self.assertEqual(worker.preview_file(payload["preview_id"], "index.html")[0].read_text(), "<h1>Status</h1>")
            self.assertEqual(worker.preview_file(payload["preview_id"], "assets/app.css")[0].read_text(), "body{}")
            self.assertIsNone(worker.preview_file(payload["preview_id"], "server-status-snapshot/index.html"))
            self.assertIsNone(worker.preview_file(payload["preview_id"], "../token"))
            self.assertEqual(worker._publish_preview(archive, payload), {"public_url": first["public_url"], "reconciled": True})

    def test_preview_does_not_guess_nested_or_multiple_project_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = ClusterWorker(self._settings(root))
            self.addCleanup(lambda: asyncio.run(worker.client.close()))
            archive = root / "ambiguous.zip"
            payload = {"preview_id": "preview_" + "e" * 32, "expires_at": int(time.time()) + 3600}
            for names in (("outer/inner/index.html",), ("one/index.html", "two/index.html"),
                          ("site/index.html", "README.txt"), ("site/index.html", "empty/")):
                with self.subTest(names=names):
                    with zipfile.ZipFile(archive, "w") as output:
                        for name in names:
                            output.writestr(name, "" if name.endswith("/") else "content")
                    with self.assertRaises(ValueError):
                        worker._publish_preview(archive, payload)
                    self.assertFalse((worker.previews / payload["preview_id"]).exists())
                    self.assertFalse((worker.previews / f".{payload['preview_id']}.tmp").exists())

    def test_preview_rejects_archive_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = ClusterWorker(self._settings(root))
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("../outside", "bad")
                output.writestr("index.html", "ok")
            with self.assertRaises(ValueError):
                worker._publish_preview(
                    archive,
                    {
                        "preview_id": "preview_" + "b" * 32,
                        "ttl_seconds": 3600,
                        "expires_at": int(time.time()) + 3600,
                    },
                )
            asyncio.run(worker.client.close())

    def test_preview_rejects_hidden_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = ClusterWorker(self._settings(root))
            archive = root / "hidden.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("index.html", "ok")
                output.writestr(".env", "SECRET=not-for-preview")
            with self.assertRaises(ValueError):
                worker._publish_preview(
                    archive,
                    {
                        "preview_id": "preview_" + "c" * 32,
                        "ttl_seconds": 3600,
                        "expires_at": int(time.time()) + 3600,
                    },
                )
            asyncio.run(worker.client.close())


class WorkerCredentialTests(unittest.TestCase):
    def test_credential_maps_to_configured_worker_not_request_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "worker"
            token.write_text("k" * 48, encoding="ascii")
            auth = WorkerAuthenticator({"h610-worker": token})
            self.assertEqual(auth.authenticate(f"Bearer {'k' * 48}"), "h610-worker")
            self.assertIsNone(auth.authenticate(f"Bearer {'x' * 48}"))


if __name__ == "__main__":
    unittest.main()
