from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

import httpx

from src.cluster_control.adapters.ops import OpsClient
from src.cluster_control.execution_storage import ClusterExecutionStore
from src.cluster_control.ops_management import OpsManagementService
from tests.test_host_operations import EXEC_DEFINITION


@unittest.skipUnless(os.getenv("TEST_OPS_POSTGRES_DSN"), "isolated PostgreSQL test not configured")
class HostOperationPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from alembic import command
        from alembic.config import Config
        from src.bot_storage.database import PostgresDatabase
        cls.dsn = os.environ["TEST_OPS_POSTGRES_DSN"]
        cls.schema = "gaoji_host_ops_test_" + uuid.uuid4().hex[:12]
        cls.temp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, AI_POSTGRES_DSN=cls.dsn, AI_POSTGRES_SCHEMA=cls.schema):
            command.upgrade(Config("alembic.ini"), "head")
        cls.db = PostgresDatabase(cls.dsn, schema=cls.schema, min_size=1, max_size=8)
        cls.store = ClusterExecutionStore(cls.db, Path(cls.temp.name))

    @classmethod
    def tearDownClass(cls):
        import psycopg
        from psycopg import sql
        cls.db.close()
        with psycopg.connect(cls.dsn) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))
        cls.temp.cleanup()

    def setUp(self):
        with closing(self.db.store_connection()) as connection, closing(connection.cursor()) as cursor:
            cursor.execute("TRUNCATE fleet_operations CASCADE")
            connection.commit()
        token = Path(self.temp.name) / "token"
        token.write_text("test-only-" + "a" * 40)
        client = OpsClient("http://ops.test", token, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"version": 2, "operations": [EXEC_DEFINITION]})))
        self.manager = OpsManagementService(client, self.store, hosts=("h310",), actors=("admin:kenneth",),
                                             host_helpers={"h310": "/helper"})
        self.addCleanup(lambda: asyncio.run(self.manager.close()))

    def prepare(self, *, key="postgres-operation"):
        result = asyncio.run(self.manager.call("exec.run", {"host": "h310", "profile": "operator",
            "command": {"argv": ["true"]}}, actor="admin:kenneth", origin="test", idempotency_key=key))
        return result["operation"]

    def approve(self, record):
        return self.store.approve_operation(record["operation_id"], actor_id="admin:kenneth",
            expected_hash=record["contract_hash"], expected_version=1, expires_at=int(time.time()) + 300)

    def expire_lease(self, record):
        with closing(self.db.store_connection()) as connection, closing(connection.cursor()) as cursor:
            cursor.execute("UPDATE fleet_operations SET lease_expires_at=0 WHERE operation_id=?", (record["operation_id"],))
            connection.commit()

    def test_parallel_claim_only_one_owner(self):
        record = self.prepare()
        self.approve(record)
        with ThreadPoolExecutor(max_workers=8) as executor:
            claims = list(executor.map(self.store.claim_managed_operation, [f"worker-{i}" for i in range(8)]))
        self.assertEqual(sum(claim is not None for claim in claims), 1)
        self.assertEqual(self.store.get_operation(record["operation_id"])["attempt"], 1)

    def test_durable_checkpoint_survives_takeover_and_rejects_old_owner(self):
        record = self.prepare()
        self.approve(record)
        first = self.store.claim_managed_operation("old")
        saved = {"phase": "submitting", "submission_started": True, "submitted_at": int(time.time())}
        self.store.checkpoint_managed_operation(record["operation_id"], owner="old", fence=first["fence"], result=saved)
        self.expire_lease(record)
        second = self.store.claim_managed_operation("new")
        self.assertIsNotNone(second)
        self.assertEqual(second["result"], saved)
        self.assertGreater(second["fence"], first["fence"])
        with self.assertRaises(PermissionError):
            self.store.checkpoint_managed_operation(record["operation_id"], owner="old", fence=first["fence"], result=saved)
        self.assertFalse(self.store.finish_managed_operation(record["operation_id"], owner="old", fence=first["fence"],
            status="succeeded", result={}, backend_id=None, error=""))
        self.assertTrue(self.store.finish_managed_operation(record["operation_id"], owner="new", fence=second["fence"],
            status="reconciling", result=saved, backend_id=None, error=""))

    def test_claim_before_submission_is_safely_recoverable(self):
        record = self.prepare()
        self.approve(record)
        self.store.claim_managed_operation("old")
        self.expire_lease(record)
        recovered = self.store.claim_managed_operation("new")
        self.assertEqual(recovered["status"], "running")
        self.assertFalse(recovered["result"].get("submission_started"))

    def test_cancel_blocks_checkpoint(self):
        record = self.prepare()
        self.approve(record)
        claim = self.store.claim_managed_operation("worker")
        self.store.cancel_operation(record["operation_id"], actor_id="admin:kenneth")
        with self.assertRaises(PermissionError):
            self.store.checkpoint_managed_operation(record["operation_id"], owner="worker", fence=claim["fence"],
                result={"phase": "submitting", "submission_started": True})
        self.assertEqual(self.store.get_operation(record["operation_id"])["result"], {})

    def test_expired_approval_cannot_be_claimed(self):
        record = self.prepare()
        approved = self.approve(record)
        with closing(self.db.store_connection()) as connection, closing(connection.cursor()) as cursor:
            cursor.execute("UPDATE fleet_approvals SET expires_at=0 WHERE approval_id=?", (approved["approval_ref"],))
            connection.commit()
        self.assertIsNone(self.store.claim_managed_operation("worker"))
        self.assertEqual(self.store.get_operation(record["operation_id"])["error_code"], "approval_expired")

    def test_consumed_approval_must_still_be_valid_before_first_submission(self):
        for index, mutation in enumerate(("expires_at=0", "contract_hash='changed'", "resource_version=99")):
            with self.subTest(mutation=mutation):
                record = self.prepare(key=f"approval-recheck-{index}")
                approved = self.approve(record)
                claim = self.store.claim_managed_operation("worker")
                with closing(self.db.store_connection()) as connection, closing(connection.cursor()) as cursor:
                    cursor.execute(f"UPDATE fleet_approvals SET {mutation} WHERE approval_id=?", (approved["approval_ref"],))
                    connection.commit()
                with self.assertRaisesRegex(PermissionError, "approval expired or changed"):
                    self.store.checkpoint_managed_operation(record["operation_id"], owner="worker", fence=claim["fence"],
                        result={"phase": "submitting", "submission_started": True})
                self.assertEqual(self.store.get_operation(record["operation_id"])["result"], {})

    def test_phase_evidence_is_replayable_and_cancel_is_not_lost(self):
        record = self.prepare()
        self.approve(record)
        claim = self.store.claim_managed_operation("worker")
        self.store.checkpoint_managed_operation(record["operation_id"], owner="worker", fence=claim["fence"],
            result={"phase": "submitting", "submission_started": True})
        self.store.cancel_operation(record["operation_id"], actor_id="admin:kenneth")
        self.assertTrue(self.store.finish_managed_operation(record["operation_id"], owner="worker", fence=claim["fence"],
            status="running", result={"phase": "waiting_for_reboot"}, backend_id="job-1", error=""))
        saved = self.store.get_operation(record["operation_id"])
        self.assertEqual(saved["status"], "cancelling")
        phases = [event["payload"].get("phase") for event in saved["events"]]
        self.assertIn("submitting", phases)
        self.assertIn("waiting_for_reboot", phases)

    def test_same_key_is_atomic_and_scope_isolated(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: self.prepare(), range(4)))
        self.assertEqual(len({row["operation_id"] for row in results}), 1)
        self.assertIsNone(self.store.find_operation("admin:kenneth", "another-group", "postgres-operation"))


if __name__ == "__main__":
    unittest.main()
