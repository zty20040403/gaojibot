"""Real SQL constraints for the official turn migration, without a model runtime."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import secrets
from unittest.mock import patch

import psycopg
from alembic import command
from alembic.config import Config

from tests.qq_official_outbox_fixture import OutboxFixture
from src.bot_storage.database import DatabaseError
from src.plugins.ai_chat.adapters.qq_official.ingest import turn_dispatch_key, turn_dispatch_lane
from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey
from src.plugins.ai_chat.storage.jobs import DurableJobStore

from src.plugins.ai_chat.adapters.qq_official import turn_source as execution
from tests import qq_official_turn_fixture as fixtures


class UnifiedMigrationTests(OutboxFixture):
    def setUp(self):
        super().setUp()
        fixtures.install_migration(self)
        self.ledger, self.jobs = MessageLedger(self.database), DurableJobStore(self.database)
        self.addCleanup(self.ledger.close)
        self.addCleanup(self.jobs.close)

    @contextmanager
    def rejected(self, cause):
        with self.assertRaises(DatabaseError) as caught:
            yield
        self.assertIsInstance(caught.exception.__cause__, cause)

    def migration(self, direction):
        # The disposable schema starts at the latest head. Follow Alembic's
        # dependency order so newer file FKs are removed before testing 0064's
        # downgrade, without weakening its refusal to discard real evidence.
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
        revision = ("0063_qq_official_outbox" if direction == "downgrade"
                    else "0064_qq_official_turn_execution")
        with patch.dict(os.environ, {"AI_POSTGRES_SCHEMA": self.database.schema,
                                     "AI_POSTGRES_DSN": self.database.dsn}):
            getattr(command, direction)(config, revision)

    def insert(self, table, values):
        with self.tx() as cursor:
            return cursor.execute("INSERT INTO " + table + " (" + ",".join(values) + ") VALUES (" + ",".join("?" for _ in values) + ") RETURNING *", tuple(values.values())).fetchone()

    def task(self, *, source=None, **override):
        values = dict(trace_id=secrets.token_hex(16), scope_key=self.endpoint.scope.key,
            conversation_id="TEST_CONVERSATION", requester_user_id=37, trigger_message_id=None,
            objective="TEST objective", status="received", priority=100, max_parallelism=1,
            max_steps=2, plan_json="{}", result_json="{}", last_error="", cancel_requested=False,
            created_at=self.now, updated_at=self.now, finished_at=None)
        if source is not None:
            values.update(requester_user_id=None, account_key=self.account.key, endpoint_key=self.endpoint.key,
                requester_actor_key=self.actor.key, trigger_message_id=source.canonical_message_id)
        values.update(override)
        return self.insert("subagent_tasks", values)

    def source(self):
        native, inbox = "TEST_SOURCE_" + secrets.token_hex(4), secrets.token_hex(16)
        message = self.ledger.record_message(self.endpoint.scope, native_message_id=native,
            sender_native_user_id=self.actor.native_user_id, sender_display="test", body=MessageBody((TextNode(0, "TEST task request"),)),
            actor=self.actor, source_endpoint=self.endpoint, occurred_at=self.now)
        self.insert("qq_official_inbound_events", dict(inbox_id=inbox, account_key=self.account.key,
            endpoint_key=self.endpoint.key, endpoint_generation=1, route_epoch=1,
            event_type="GROUP_AT_MESSAGE_CREATE", native_message_id=native, status="processed",
            echo_state="not_echo", trigger_requested=True, canonical_message_id=message.canonical_message_id,
            received_at=self.now, created_at=self.now, updated_at=self.now, turn_text="TEST task request", turn_text_version=1))
        kind = account_job_kind("turn", self.account.key)
        with self.tx() as cursor:
            job, _ = self.jobs.enqueue(kind=kind, payload={"inbox_id": inbox},
                scope_key=turn_dispatch_lane(self.account.key, self.endpoint.key),
                idempotency_key=turn_dispatch_key(self.account.key, self.endpoint.key, inbox),
                now=self.now, transaction_cursor=cursor)
            cursor.execute("UPDATE qq_official_inbound_events SET dispatch_job_id=? WHERE inbox_id=?", (job.job_id, inbox))
        job = self.jobs.claim_due("TEST_SCHEMA_WORKER", kinds=(kind,), now=self.now)[0]
        with self.tx() as cursor:
            return execution.claim_execution(cursor, token=self.token, job=job, now=self.now,
                deadline_at=self.now + 600, admission=lambda *_: True).handle

    def manifest(self, source, task, **override):
        job, _ = self.jobs.enqueue(kind="TEST_SUBAGENT_SCHEMA", payload={"task_id": task["task_id"]},
            scope_key="TEST_BACKGROUND", idempotency_key=secrets.token_hex(16), now=self.now)
        values = dict(task_id=task["task_id"], task_revision=1, account_key=self.account.key,
            endpoint_key=self.endpoint.key, requester_actor_key=self.actor.key,
            source_execution_id=source.execution_id, source_canonical_message_id=source.canonical_message_id,
            source_native_message_id=source.source_native_message_id, submission_key=secrets.token_hex(32),
            submission_digest=secrets.token_hex(32), job_id=job.job_id, endpoint_generation=1, route_epoch=1,
            tool_profile="official-text-v1", execution_deadline_at=self.now+900, result_wait_seconds=600,
            created_at=self.now, updated_at=self.now)
        values.update(override)
        return self.insert("qq_official_subagent_dispatches", values)


    def test_task_identity_union_rejects_partial_and_mixed_authority(self):
        source = self.source()
        valid = self.task(source=source)
        self.assertIsNone(valid["requester_user_id"])
        for override in ({"requester_user_id": 0}, {"requester_actor_key": None}, {"trigger_message_id": None}, {"project_id": 1}):
            with self.subTest(fields=tuple(override)), self.rejected(psycopg.errors.ForeignKeyViolation if 'project_id' in override else psycopg.errors.CheckViolation):
                self.task(source=source, **override)

    def test_task_endpoint_account_cannot_cross_namespaces(self):
        source = self.source()
        other = AccountKey("qq-official", "TEST_OTHER_SCHEMA_ACCOUNT", "test")
        other_endpoint = EndpointKey(other, "group", "TEST_GROUP")
        self.provision(other_endpoint)
        with self.rejected(psycopg.errors.ForeignKeyViolation):
            self.task(source=source, endpoint_key=other_endpoint.key)

    def test_manifest_binds_parent_native_canonical_and_task_trigger(self):
        source = self.source()
        task = self.task(source=source)
        with self.rejected(psycopg.errors.ForeignKeyViolation):
            self.manifest(source, task, source_native_message_id="TEST_WRONG_NATIVE")
        other_message = self.ledger.record_message(self.endpoint.scope, native_message_id="TEST_OTHER_SOURCE",
            sender_native_user_id=self.actor.native_user_id, sender_display="test", body=MessageBody(),
            actor=self.actor, source_endpoint=self.endpoint, occurred_at=self.now)
        wrong_task = self.task(source=source, trigger_message_id=other_message.canonical_message_id)
        with self.rejected(psycopg.errors.ForeignKeyViolation):
            self.manifest(source, wrong_task)
        valid = self.manifest(source, task)
        self.assertEqual(valid["source_native_message_id"], source.source_native_message_id)

    def test_manifest_submission_identity_is_unique_and_revision_is_one(self):
        source = self.source()
        task = self.task(source=source)
        with self.rejected(psycopg.errors.CheckViolation):
            self.manifest(source, task, task_revision=2)
        first = self.manifest(source, task)
        other_task = self.task(source=source)
        with self.rejected(psycopg.errors.UniqueViolation):
            self.manifest(source, other_task, submission_key=first["submission_key"])

    def test_result_tuple_and_original_wait_bound_are_atomic(self):
        source = self.source()
        task = self.task(source=source)
        self.manifest(source, task)
        with self.rejected(psycopg.errors.CheckViolation):
            self.query("UPDATE qq_official_subagent_dispatches SET result_body_json='{}'")
        with self.rejected(psycopg.errors.CheckViolation):
            self.query("""UPDATE qq_official_subagent_dispatches SET result_body_json='{}',result_digest=?,
                result_task_status='completed',result_at=?,result_expires_at=?""", ("a"*64, self.now, self.now+601))
        self.query("""UPDATE qq_official_subagent_dispatches SET result_body_json='{}',result_digest=?,
            result_task_status='completed',result_at=?,result_expires_at=?""", ("a"*64, self.now, self.now+600))
        self.assertEqual(self.query("SELECT result_expires_at FROM qq_official_subagent_dispatches")[0][0], self.now+600)
