"""Real PG checks of formal 0065 upgrade/downgrade and exact SQL constraints.

This test invokes the migration through Alembic Operations.context inside the
disposable repository fixture without changing its declared Alembic revision.
All identifiers and message content are synthetic. No platform I/O.
"""
import importlib.util
import os
import secrets
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import psycopg
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from src.bot_storage.database import DatabaseError
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.message_ir import MessageBody, TextNode, body_to_json
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey
from tests.qq_official_file_fixture import ExecutionFixture
from tests.test_qq_official_plans import PlanTests
from tests.qq_official_receipt_fixture import ReceiptFixture

ROOT = Path(__file__).resolve().parents[1]
TABLES = ("qq_official_file_artifacts", "qq_official_file_deliveries", "qq_official_file_publications",
          "qq_official_generation_capacity", "qq_official_file_stage_attempts",
          "qq_official_file_credentials", "qq_official_file_message_plans")
REDACTED = body_to_json(MessageBody((TextNode(0, "[受保护附件：文件内容及访问凭据已从会话上下文隐去。]"),)))


class FileMigrationTests(ExecutionFixture):
    def setUp(self):
        super().setUp()
        # Remove newer descendants through Alembic's dependency order first.
        # M6's direct Operations assertions below then remain isolated to 0065.
        config = Config()
        config.set_main_option("script_location", str(ROOT / "migrations"))
        with patch.dict(os.environ, {"AI_POSTGRES_SCHEMA": self.database.schema,
                                     "AI_POSTGRES_DSN": self.database.dsn}):
            command.downgrade(config, "0065_qq_official_files")
        spec = importlib.util.spec_from_file_location("test_m6_migration", ROOT / "migrations/versions/0065_qq_official_files.py")
        self.migration_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.migration_module)
        # The fixture truncates data between tests; remove the previous test's
        # 0065 schema so each test begins with 0064 physical structures; the
        # declared revision remains unchanged by direct Operations calls.
        if self.query("SELECT to_regclass(?)", (self.database.schema + "." + TABLES[0],))[0][0] is not None:
            self.migration("downgrade")
        self.deliveries = DeliveryStore(self.database, recover_interrupted=False)
        self.addCleanup(self.deliveries.close)
        self.pepper = b"TEST_MIGRATION_PRIVATE_PEPPER_0123456789"

    def migration(self, direction):
        engine = sa.create_engine("postgresql+psycopg://", creator=lambda: psycopg.connect(self.database.dsn), poolclass=sa.pool.NullPool)
        try:
            with (engine.begin() as connection,
                  patch.dict(os.environ, {"AI_POSTGRES_SCHEMA": self.database.schema}),
                  Operations.context(MigrationContext.configure(connection))):
                getattr(self.migration_module, direction)()
        finally:
            engine.dispose()

    @contextmanager
    def rejected(self, cause):
        with self.assertRaises(DatabaseError) as caught:
            yield
        self.assertIsInstance(caught.exception.__cause__, cause)

    def insert(self, table, values):
        with self.tx() as cursor:
            return cursor.execute("INSERT INTO " + table + " (" + ",".join(values) + ") VALUES ("
                                  + ",".join("?" for _ in values) + ") RETURNING *", tuple(values.values())).fetchone()

    def clone(self, table, row, *, omit=(), **updates):
        columns = self.query("SELECT column_name FROM information_schema.columns WHERE table_schema=? AND table_name=? AND is_generated='NEVER'",
                             (self.database.schema, table))
        values = {item["column_name"]: row[item["column_name"]] for item in columns if item["column_name"] not in omit}
        values.update(updates)
        return self.insert(table, values)

    def constraints(self):
        return [tuple(row) for row in self.query("""SELECT c.relname,k.conname,pg_get_constraintdef(k.oid)
            FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=? AND c.relname IN ('deliveries','delivery_plans','delivery_attempts')
            ORDER BY c.relname,k.conname""", (self.database.schema,))]

    def legacy(self):
        scope = ConversationScope("onebot", "group", "37")
        delivery, _ = self.deliveries.enqueue(idempotency_key="TEST_LEGACY", source_scope_key=scope.key,
            target_scope=scope, body=MessageBody((TextNode(0, "TEST legacy reply"),)), now=self.now)
        return self.deliveries.begin_direct_attempt(delivery.delivery_id, now=self.now)

    challenge = ReceiptFixture.challenge

    def normal_attempt(self, **kwargs):
        return ReceiptFixture.attempt(self, **kwargs)

    def normal_plan(self):
        return PlanTests.enqueue(self, logical_key=secrets.token_hex(16), purpose="requested_artifact")

    def protected(self, *, plan_updates=None, delivery_updates=None):
        base = self.normal_plan()
        plan = self.query("SELECT * FROM delivery_plans WHERE plan_id=?", (base.plan_id,))[0]
        delivery = self.query("SELECT * FROM deliveries WHERE delivery_id=?", (base.delivery_ids[0],))[0]
        new_plan = self.clone("delivery_plans", plan, plan_id=secrets.token_hex(16), idempotency_key="qqp1:" + secrets.token_hex(32),
                              payload_mode="protected", **(plan_updates or {}))
        new_delivery = self.clone("deliveries", delivery, omit=("delivery_id",), plan_id=new_plan["plan_id"],
            idempotency_key=secrets.token_hex(32), payload_mode="protected", body_json=REDACTED,
            **(delivery_updates or {}))
        return new_plan, new_delivery

    def protected_attempt(self, delivery, **updates):
        normal = self.normal_attempt()
        original = self.query("SELECT * FROM delivery_attempts WHERE attempt_id=?", (normal.attempt_id,))[0]
        return self.clone("delivery_attempts", original, omit=("attempt_id",), delivery_id=delivery["delivery_id"],
            payload_mode="protected", window_id=self.window(), wire_storage_digest="a" * 64, **updates)

    def artifact(self, **updates):
        if not hasattr(self, "handle"):
            self.handle = self.claim().handle
        values = {"artifact_id": secrets.token_hex(16), "request_key": secrets.token_hex(32), "account_key": self.account.key,
            "endpoint_key": self.endpoint.key, "requester_actor_key": self.actor.key, "source_execution_id": self.handle.execution_id,
            "source_canonical_message_id": self.handle.canonical_message_id, "source_native_message_id": self.handle.source_native_message_id,
            "source_scope_key": self.endpoint.scope.key, "source_job_id": self.job.job_id,
            "source_job_lease_generation": self.job.lease_generation, "source_account_epoch": self.token.epoch,
            "endpoint_generation": 1, "route_epoch": 1, "sha256": "a" * 64, "byte_size": 17, "mime": "application/pdf", "filename": "test.pdf",
            "draft": False, "draft_reason": "", "created_at": self.now, "pending_deadline_at": self.now + 86400, "retain_until": self.now + 3600}
        values.update(updates)
        return self.insert(TABLES[0], values)

    def publication(self, artifact, **updates):
        values = {"publication_ref": secrets.token_hex(16), "credential_ref": secrets.token_hex(16), "request_key": secrets.token_hex(32),
            "artifact_id": artifact["artifact_id"], "token_digest": secrets.token_hex(32), "wire_commitment": "b" * 64,
            "origin": "https://TEST-download.example/files", "created_at": self.now, "expires_at": self.now + 100}
        values.update(updates)
        return self.insert(TABLES[2], values)

    def stage(self, artifact, **updates):
        rows = self.query("SELECT job_id FROM qq_official_file_deliveries WHERE artifact_id=?", (artifact["artifact_id"],))
        if not rows:
            job, _ = self.jobs.enqueue(kind="TEST_FILE_MIGRATION", payload={"artifact_id": artifact["artifact_id"]},
                idempotency_key="TEST_FILE:" + artifact["artifact_id"], now=self.now)
            self.insert(TABLES[1], {"artifact_id": artifact["artifact_id"], "job_id": job.job_id,
                "state": "stored", "created_at": self.now, "updated_at": self.now})
            rows = self.query("SELECT job_id FROM qq_official_file_deliveries WHERE artifact_id=?", (artifact["artifact_id"],))
        values = {"stage_attempt_id": secrets.token_hex(16), "artifact_id": artifact["artifact_id"], "operation": "upload_small",
            "request_commitment": "c" * 64, "job_id": rows[0]["job_id"], "job_lease_generation": 1,
            "account_epoch": self.token.epoch, "state": "confirmed", "created_at": self.now, "expires_at": self.now + 100,
            "settled_at": self.now, "receipt_commitment": "d" * 64}
        values.update(updates)
        return self.insert(TABLES[4], values)

    def credential(self, artifact, *, publication=None, stage=None, **updates):
        values = {"credential_ref": publication["credential_ref"] if publication else secrets.token_hex(16),
            "artifact_id": artifact["artifact_id"], "route": "link" if publication else "native", "content_commitment": "e" * 64,
            "created_at": self.now, "expires_at": self.now + 100,
            "publication_ref": publication["publication_ref"] if publication else None,
            "native_stage_attempt_id": stage["stage_attempt_id"] if stage else None,
            "native_binding_digest": "f" * 64 if stage else None,
            "native_expires_at": self.now + 200 if stage else None,
            "native_md5": "a" * 32 if stage else None, "native_sha1": "b" * 40 if stage else None,
            "native_md5_10m": "c" * 32 if stage else None}
        values.update(updates)
        return self.insert(TABLES[5], values)

    def message_plan(self, artifact, credential, plan, delivery, **updates):
        values = {"plan_id": plan["plan_id"], "artifact_id": artifact["artifact_id"], "credential_ref": credential["credential_ref"],
                      "delivery_id": delivery["delivery_id"], "created_at": self.now,
                      "account_key": artifact["account_key"], "endpoint_key": artifact["endpoint_key"],
                      "requester_actor_key": artifact["requester_actor_key"]}
        values.update(updates)
        return self.insert(TABLES[6], values)

    def test_file_empty_upgrade_downgrade_preserves_exact_old_constraints_and_head(self):
        before = self.constraints()
        head = self.query("SELECT version_num FROM alembic_version")[0][0]
        self.assertEqual(head, "0065_qq_official_files")
        self.assertEqual(self.migration_module.down_revision, "0064_qq_official_turn_execution")
        self.migration("upgrade")
        for table in TABLES:
            self.assertIsNotNone(self.query("SELECT to_regclass(?)", (self.database.schema + "." + table,))[0][0])
        self.assertEqual(self.query("SELECT version_num FROM alembic_version")[0][0], head)
        self.migration("downgrade")
        self.assertEqual(self.constraints(), before)
        for table in TABLES:
            self.assertIsNone(self.query("SELECT to_regclass(?)", (self.database.schema + "." + table,))[0][0])
        self.assertFalse(self.query("SELECT column_name FROM information_schema.columns WHERE table_schema=? AND table_name='delivery_attempts' AND column_name='wire_storage_digest'", (self.database.schema,)))
        self.migration("upgrade")

    def test_file_legacy_onebot_and_otp_rows_roundtrip_without_private_content_change(self):
        legacy = self.legacy()
        endpoint = EndpointKey(self.account, "private", "TEST_PRIVATE")
        self.provision(endpoint)
        # Current M6 outbox now writes the new nullable column even for OTP.
        # Populate using that real API, then return to 0064 before taking the
        # old-row snapshot; no mock weakens the old SQL constraints.
        self.migration("upgrade")
        otp = self.normal_attempt(endpoint=endpoint, ephemeral=True)
        self.migration("downgrade")
        before_delivery = [dict(row) for row in self.query("SELECT * FROM deliveries ORDER BY delivery_id")]
        before_attempt = [dict(row) for row in self.query("SELECT * FROM delivery_attempts ORDER BY attempt_id")]
        self.migration("upgrade")
        self.assertIsNone(self.query("SELECT wire_storage_digest FROM delivery_attempts WHERE delivery_id=?", (legacy.delivery_id,))[0][0])
        self.assertIsNone(self.query("SELECT wire_storage_digest FROM delivery_attempts WHERE attempt_id=?", (otp.attempt_id,))[0][0])
        self.migration("downgrade")
        self.assertEqual([dict(row) for row in self.query("SELECT * FROM deliveries ORDER BY delivery_id")], before_delivery)
        self.assertEqual([dict(row) for row in self.query("SELECT * FROM delivery_attempts ORDER BY attempt_id")], before_attempt)

    def test_file_otp_and_onebot_constraints_still_reject_mixed_or_persistent_secrets(self):
        legacy = self.legacy()
        endpoint = EndpointKey(self.account, "private", "TEST_PRIVATE")
        self.provision(endpoint)
        self.migration("upgrade")
        otp = self.normal_attempt(endpoint=endpoint, ephemeral=True)
        cases = (("UPDATE deliveries SET purpose='final' WHERE delivery_id=?", (legacy.delivery_id,)),
                 ("UPDATE deliveries SET body_json='TEST_OTP_SECRET' WHERE delivery_id=?", (otp.delivery_id,)),
                 ("UPDATE delivery_attempts SET wire_payload_json='TEST_OTP_SECRET' WHERE attempt_id=?", (otp.attempt_id,)),
                 ("UPDATE delivery_attempts SET attempt=2 WHERE attempt_id=?", (otp.attempt_id,)))
        for sql, args in cases:
            with self.subTest(sql=sql), self.rejected(psycopg.errors.CheckViolation):
                self.query(sql, args)

    def test_file_legacy_attempt_cannot_acquire_protected_wire_commitment(self):
        legacy = self.legacy()
        self.migration("upgrade")
        attempt_id = self.query("SELECT attempt_id FROM delivery_attempts WHERE delivery_id=?", (legacy.delivery_id,))[0][0]
        with self.rejected(psycopg.errors.CheckViolation):
            self.query("UPDATE delivery_attempts SET wire_storage_digest=? WHERE attempt_id=?", ("a" * 64, attempt_id))

    def test_file_protected_plan_delivery_and_attempt_valid_combination(self):
        self.migration("upgrade")
        plan, delivery = self.protected()
        attempt = self.protected_attempt(delivery)
        self.assertEqual((plan["payload_mode"], delivery["body_json"], attempt["wire_storage_digest"]), ("protected", REDACTED, "a" * 64))

    def test_file_protected_plan_is_requested_artifact_single_part_only(self):
        self.migration("upgrade")
        for changes in ({"purpose": "final"}, {"part_count": 2}, {"digest_key_id": "TEST_KEY"}):
            with self.subTest(fields=tuple(changes)), self.rejected(psycopg.errors.CheckViolation):
                self.protected(plan_updates=changes)

    def test_file_protected_delivery_forbids_raw_body_and_fallback(self):
        self.migration("upgrade")
        _, delivery = self.protected()
        cases = (("body_json", "TEST_RAW_SECRET"), ("part_index", 1), ("variant", "plain_fallback"), ("digest_key_id", "TEST_KEY"))
        for field, value in cases:
            with self.subTest(field=field), self.rejected(psycopg.errors.CheckViolation):
                self.query("UPDATE deliveries SET " + field + "=? WHERE delivery_id=?", (value, delivery["delivery_id"]))

    def test_file_protected_attempt_requires_digest_and_safe_mode(self):
        self.migration("upgrade")
        _, delivery = self.protected()
        attempt = self.protected_attempt(delivery)
        for field, value in (("wire_storage_digest", None), ("wire_storage_digest", "not_a_hash"), ("wire_payload_json", None), ("digest_scheme", "hmac_sha256_v1")):
            with self.subTest(field=field), self.rejected(psycopg.errors.CheckViolation):
                self.query("UPDATE delivery_attempts SET " + field + "=? WHERE attempt_id=?", (value, attempt["attempt_id"]))

    def test_file_artifact_foreign_keys_bind_exact_account_endpoint_actor_and_source(self):
        self.migration("upgrade")
        self.artifact()
        other_account = AccountKey("qq-official", "TEST_OTHER_ACCOUNT", "sandbox")
        other_endpoint = EndpointKey(other_account, "group", self.endpoint.native_conversation_id)
        self.provision(other_endpoint)
        for changes in ({"account_key": other_account.key}, {"endpoint_key": other_endpoint.key, "account_key": other_account.key},
                        {"requester_actor_key": "TEST_OTHER_ACTOR"}, {"source_native_message_id": "TEST_OTHER_SOURCE"},
                        {"source_canonical_message_id": self.handle.canonical_message_id + 1000}, {"source_job_id": 999999}):
            with self.subTest(fields=tuple(changes)), self.rejected(psycopg.errors.ForeignKeyViolation):
                self.artifact(**changes)

    def test_file_manifest_bounds_and_immutable_retention(self):
        self.migration("upgrade")
        for changes in ({"byte_size": 104857601}, {"mime": "text/plain"}, {"filename": "x" * 181},
                        {"pending_deadline_at": self.now + 86401}, {"retain_until": self.now + 86401}):
            with self.subTest(fields=tuple(changes)), self.rejected(psycopg.errors.CheckViolation):
                self.artifact(**changes)
        artifact = self.artifact()
        for field, value in (("filename", "changed.pdf"), ("retain_until", self.now + 3601), ("source_scope_key", "TEST_OTHER_SCOPE")):
            with self.subTest(field=field), self.rejected(psycopg.errors.RaiseException):
                self.query("UPDATE qq_official_file_artifacts SET " + field + "=? WHERE artifact_id=?", (value, artifact["artifact_id"]))

    def test_file_publication_and_link_credential_have_exact_artifact_and_ref_fks(self):
        self.migration("upgrade")
        first, second = self.artifact(), self.artifact()
        publication = self.publication(first)
        for changes in ({"artifact_id": second["artifact_id"]}, {"credential_ref": secrets.token_hex(16)}):
            with self.subTest(fields=tuple(changes)), self.rejected(psycopg.errors.ForeignKeyViolation):
                self.credential(first, publication=publication, **changes)
        valid = self.credential(first, publication=publication)
        self.assertEqual(valid["credential_ref"], publication["credential_ref"])
        with self.rejected(psycopg.errors.ForeignKeyViolation):
            self.publication(first, artifact_id="f" * 32)
        with self.rejected(psycopg.errors.CheckViolation):
            self.publication(second, expires_at=self.now + 3601)

    def test_file_native_credential_cannot_reuse_another_artifact_stage(self):
        self.migration("upgrade")
        first, second = self.artifact(), self.artifact()
        stage = self.stage(first)
        with self.rejected(psycopg.errors.ForeignKeyViolation):
            self.credential(second, stage=stage)
        self.credential(first, stage=stage)
        with self.rejected(psycopg.errors.UniqueViolation):
            self.credential(first, stage=stage)

    def test_file_native_original_expiry_and_checksums_cannot_be_mixed_with_links(self):
        self.migration("upgrade")
        artifact = self.artifact()
        stage = self.stage(artifact)
        for changes in ({"native_expires_at": None}, {"native_expires_at": self.now + 99},
                        {"native_md5": None}, {"native_sha1": "not_a_checksum"}, {"native_md5_10m": None}):
            with self.subTest(fields=tuple(changes)), self.rejected(psycopg.errors.CheckViolation):
                self.credential(artifact, stage=stage, **changes)
        credential = self.credential(artifact, stage=stage)
        self.assertGreater(credential["native_expires_at"], credential["expires_at"])
        publication = self.publication(artifact)
        for changes in ({"native_expires_at": self.now + 200}, {"native_binding_digest": "f" * 64}, {"native_md5": "a" * 32}):
            with self.subTest(fields=tuple(changes)), self.rejected(psycopg.errors.CheckViolation):
                self.credential(artifact, publication=publication, **changes)
        self.credential(artifact, publication=publication)

    def test_file_stage_part_and_receipt_state_constraints(self):
        self.migration("upgrade")
        artifact = self.artifact()
        for changes in ({"operation": "upload_put"}, {"operation": "upload_small", "part_index": 0},
                        {"state": "confirmed", "receipt_commitment": None}, {"state": "dispatched", "settled_at": self.now},
                        {"http_status": 99}, {"expires_at": self.now}, {"operation": "invented_stage"}):
            with self.subTest(fields=tuple(changes)), self.rejected(psycopg.errors.CheckViolation):
                self.stage(artifact, **changes)
        self.stage(artifact, operation="upload_put", part_index=0)

    def test_file_stage_cannot_borrow_producer_job_or_another_file_job(self):
        self.migration("upgrade")
        first, second = self.artifact(), self.artifact()
        first_stage, second_stage = self.stage(first), self.stage(second)
        self.assertNotEqual(first_stage["job_id"], self.job.job_id)
        for job_id in (self.job.job_id, second_stage["job_id"]):
            with self.subTest(job=job_id), self.rejected(psycopg.errors.ForeignKeyViolation):
                self.stage(first, job_id=job_id)

    def test_file_message_plan_requires_exact_credential_artifact_and_delivery_plan_pair(self):
        self.migration("upgrade")
        first, second = self.artifact(), self.artifact()
        credential = self.credential(first, stage=self.stage(first))
        plan, delivery = self.protected()
        other_plan, other_delivery = self.protected()
        with self.rejected(psycopg.errors.ForeignKeyViolation):
            self.message_plan(second, credential, plan, delivery)
        with self.rejected(psycopg.errors.ForeignKeyViolation):
            self.message_plan(first, credential, plan, other_delivery)
        row = self.message_plan(first, credential, plan, delivery)
        self.assertEqual(row["plan_id"], plan["plan_id"])
        with self.rejected(psycopg.errors.UniqueViolation):
            self.message_plan(first, credential, other_plan, delivery)

    def test_file_message_plan_cannot_associate_other_account_or_requester(self):
        self.migration("upgrade")
        artifact = self.artifact()
        credential = self.credential(artifact, stage=self.stage(artifact))
        other_account = AccountKey("qq-official", "TEST_OTHER_PLAN_ACCOUNT", "sandbox")
        other_endpoint = EndpointKey(other_account, "group", self.endpoint.native_conversation_id)
        self.provision(other_endpoint)
        other_token = self.lease(other_account).acquire()
        other_actor = ActorKey(other_account, "group_member", other_endpoint.native_conversation_id, self.actor.native_user_id)
        with patch.multiple(self, account=other_account, endpoint=other_endpoint, actor=other_actor, token=other_token):
            other_plan, other_delivery = self.protected()
        with self.rejected(psycopg.errors.ForeignKeyViolation):
            self.message_plan(artifact, credential, other_plan, other_delivery)
        with patch.object(self, "actor", ActorKey(self.account, "group_member", self.endpoint.native_conversation_id, "TEST_OTHER_REQUESTER")):
            other_plan, other_delivery = self.protected()
        with self.rejected(psycopg.errors.ForeignKeyViolation):
            self.message_plan(artifact, credential, other_plan, other_delivery)

    def test_file_message_plan_requires_protected_artifact_mode_not_normal_text(self):
        self.migration("upgrade")
        artifact = self.artifact()
        credential = self.credential(artifact, stage=self.stage(artifact))
        ordinary = self.normal_plan()
        plan = self.query("SELECT * FROM delivery_plans WHERE plan_id=?", (ordinary.plan_id,))[0]
        delivery = self.query("SELECT * FROM deliveries WHERE delivery_id=?", (ordinary.delivery_ids[0],))[0]
        with self.rejected(psycopg.errors.ForeignKeyViolation):
            self.message_plan(artifact, credential, plan, delivery)
        for changes in ({"payload_mode": "persistent"}, {"purpose": "final"}):
            with self.subTest(fields=tuple(changes)), self.rejected(psycopg.errors.CheckViolation):
                self.message_plan(artifact, credential, plan, delivery, **changes)

    def test_file_active_plan_and_credential_cannot_cross_artifacts(self):
        self.migration("upgrade")
        first, second = self.artifact(), self.artifact()
        credential = self.credential(first, stage=self.stage(first))
        plan, delivery = self.protected()
        self.message_plan(first, credential, plan, delivery)
        job, _ = self.jobs.enqueue(kind="TEST_FILE", payload={}, idempotency_key=secrets.token_hex(16), now=self.now)
        values = {"artifact_id": second["artifact_id"], "job_id": job.job_id, "state": "stored", "created_at": self.now, "updated_at": self.now}
        with self.rejected(psycopg.errors.ForeignKeyViolation):
            self.insert(TABLES[1], {**values, "active_plan_id": plan["plan_id"]})
        with self.rejected(psycopg.errors.ForeignKeyViolation):
            self.insert(TABLES[1], {**values, "credential_ref": credential["credential_ref"]})
        self.query("UPDATE qq_official_file_deliveries SET active_plan_id=?,credential_ref=?,state='handed_off' WHERE artifact_id=?",
                   (plan["plan_id"], credential["credential_ref"], first["artifact_id"]))

    def test_file_downgrade_refuses_artifact_evidence_before_any_drop(self):
        self.migration("upgrade")
        artifact = self.artifact()
        before = self.constraints()
        with self.assertRaisesRegex(RuntimeError, "file_evidence_requires_export"):
            self.migration("downgrade")
        self.assertEqual(self.constraints(), before)
        self.assertEqual(self.query("SELECT artifact_id FROM qq_official_file_artifacts")[0][0], artifact["artifact_id"])
        for table in TABLES:
            self.assertIsNotNone(self.query("SELECT to_regclass(?)", (self.database.schema + "." + table,))[0][0])

    def test_file_downgrade_refuses_protected_plan_even_without_any_file_rows(self):
        self.migration("upgrade")
        plan, _ = self.protected()
        with self.assertRaisesRegex(RuntimeError, "file_evidence_requires_export"):
            self.migration("downgrade")
        self.assertEqual(self.query("SELECT payload_mode FROM delivery_plans WHERE plan_id=?", (plan["plan_id"],))[0][0], "protected")
        self.assertIsNotNone(self.query("SELECT to_regclass(?)", (self.database.schema + "." + TABLES[0],))[0][0])

    def test_file_downgrade_refuses_released_capacity_evidence_without_artifact(self):
        self.migration("upgrade")
        handle = self.claim().handle
        self.insert(TABLES[3], {"reservation_id": secrets.token_hex(16), "account_key": self.account.key, "endpoint_key": self.endpoint.key,
            "requester_actor_key": self.actor.key, "source_execution_id": handle.execution_id, "source_key": "TEST_CAPACITY",
            "owner": "qqo:v1:" + "a" * 64 + ":x:" + "b" * 32, "state": "released", "created_at": self.now,
            "expires_at": self.now + 100, "released_at": self.now, "release_evidence": "owned_destroy"})
        with self.assertRaisesRegex(RuntimeError, "file_evidence_requires_export"):
            self.migration("downgrade")
        self.assertEqual(self.query("SELECT state FROM qq_official_generation_capacity")[0][0], "released")


    def test_file_publication_intent_is_all_or_none_and_fixed_to_link(self):
        self.migration('upgrade')
        artifact = self.artifact()
        self.stage(artifact)
        for route, key, started, expiry in ((None, 'a'*64, self.now, self.now+60),
                ('native', 'a'*64, self.now, self.now+60),
                ('link', 'a'*64, None, self.now+60),
                ('link', None, self.now, self.now+60),
                ('link', 'a'*64, self.now, self.now+3601)):
            with self.rejected(psycopg.errors.CheckViolation):
                with self.tx() as cursor:
                    cursor.execute('UPDATE qq_official_file_deliveries SET route=?,publication_request_key=?,publication_started_at=?,publication_expires_at=? WHERE artifact_id=?',
                        (route,key,started,expiry,artifact['artifact_id']))
        with self.tx() as cursor:
            cursor.execute("UPDATE qq_official_file_deliveries SET state='publishing',route='link',publication_request_key=?,publication_started_at=?,publication_expires_at=? WHERE artifact_id=?",
                ('a'*64,self.now,self.now+3600,artifact['artifact_id']))

    def test_file_task_profile_is_additive_and_prevents_evidence_downgrade(self):
        from unittest.mock import Mock
        from src.plugins.ai_chat.adapters.qq_official import turn_source as execution, subagent_runtime
        from src.plugins.ai_chat.agent.contracts import ContextPacket
        from src.plugins.ai_chat.model_catalog import ModelCatalog, ModelProfile
        from src.plugins.ai_chat.subagents import SubAgentStore, SubAgentCoordinator
        self.migration('upgrade')
        handle = self.claim().handle
        self.begin(handle)
        store = SubAgentStore(self.database)
        self.addCleanup(store.close)
        profile = ModelProfile(name='test',provider='test',protocol='openai-chat',model='test',base_url='http://127.0.0.1',api_key_required=False)
        coordinator = SubAgentCoordinator(store,ModelCatalog({'test':profile},default_profile='test'),logger=Mock())
        packet = ContextPacket(self.endpoint.scope.key,'TEST_FILE_PROFILE',None,self.message.canonical_message_id,
            'TEST PDF',endpoint=self.endpoint,requester_actor=self.actor)
        with self.tx() as cursor:
            subagent_runtime.submit(cursor,token=self.token,source_api=execution,source_handle=handle,
                source_job=self.job,tool_call_id='TEST_FILE_PROFILE',tool_name='run_subagents',packet=packet,
                decision=None,profile='test',now=self.now,execution_deadline=self.now+600,result_wait_seconds=600,
                admission=self.admission,coordinator=coordinator,jobs=self.jobs,tool_profile='official-files-v1')
        self.assertEqual(self.query('SELECT * FROM qq_official_file_artifacts'),[])
        with self.assertRaisesRegex(RuntimeError,'file_evidence_requires_export'):
            self.migration('downgrade')
        self.assertEqual(self.query('SELECT tool_profile FROM qq_official_subagent_dispatches')[0][0],'official-files-v1')


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(FileMigrationTests(name) for name in dir(FileMigrationTests) if name.startswith("test_file_"))


if __name__ == "__main__":
    unittest.main()
