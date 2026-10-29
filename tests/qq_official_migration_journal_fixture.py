import hashlib
import secrets
from unittest.mock import patch

import psycopg

from src.plugins.ai_chat.adapters.qq_official.migration_journal import Journal, PARKED_AT
from src.plugins.ai_chat.adapters.qq_official.migration_types import (
    BaselineEvidence, ControlEvidence, ExportEvidence, MigrationDenied,
    MigrationItemProof, VerificationEvidence, digest, encoded,
)
from src.plugins.ai_chat.adapters.qq_official.migration_routes import RouteService
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey
from src.plugins.ai_chat.adapters.qq_official.plans import enqueue_plan
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from src.plugins.ai_chat.turn_journal import TurnJournal
from tests.qq_official_outbox_fixture import OutboxFixture
from tests import test_qq_official_receipts as receipt_fixture
from tests.qq_official_migration_mapping_fixture import install_mapping,rotate_mapping



class HostPorts:
    """Test host implementations; use real sessions/leases/domain rows below."""
    def __init__(self, test):
        self.test = test
        self.approved = set()
        self.export_revision = 1
        self.classification = "unsent"
        self.schedule = None

    def control(self, cursor, authorization, *, action, preview_digest, now):
        if authorization is not self.test.authorization or (action == "freeze" and preview_digest not in self.approved):
            raise MigrationDenied("test_host_review_required")
        return ControlEvidence("admin", 1, "a"*64)

    def baseline(self, cursor, source, *, account, now):
        if source is not self.test.baseline_source or account != self.test.source.account:
            raise MigrationDenied("test_host_baseline_required")
        return self.test.baseline

    def export(self, cursor, source_endpoint, source_scope, target_endpoint, kind, source_ref, now):
        if (source_endpoint != self.test.source or source_scope.key != self.test.source_scope.key
                or target_endpoint != self.test.endpoint):
            raise MigrationDenied("test_export_namespace")
        payload = {"type": "reminder", "text": "APPROVED CONTENT", "revision": self.export_revision}
        return ExportEvidence(kind, source_ref, self.export_revision, encoded(payload), self.classification,
            self.test.source_actor, digest([kind, source_ref, self.export_revision, self.classification, payload]),
            self.test.now+100, self.test.now+300, schedule_json=encoded(self.schedule) if self.schedule else None)

    def mapping(self, cursor, source_endpoint, source_scope, source_actor, target_endpoint, now):
        if (source_endpoint != self.test.source or source_scope.key != self.test.source_scope.key
                or source_actor != self.test.source_actor or target_endpoint != self.test.endpoint):
            raise MigrationDenied("test_mapping_namespace")
        return self.test.mapping_store.lookup(cursor,source_endpoint,source_scope,source_actor,target_endpoint,now)

    def effect(self, cursor, source, *, source_endpoint, source_scope, endpoint, operation, now):
        if source is not self.test.effect_source or source_endpoint != self.test.source or source_scope.key != self.test.source_scope.key:
            raise MigrationDenied("test_effect_source_required")
        if endpoint.account != self.test.source.account:
            raise MigrationDenied("test_effect_target")

    def verification(self, cursor, source, *, migration_id, target_endpoint, nonce, now):
        value = self.test.verifications.get(source)
        if value is None or value.nonce != nonce or target_endpoint != self.test.endpoint:
            raise MigrationDenied("test_verification_required")
        return value

    def verification_request(self, cursor, source, **kwargs):
        if source is not self.test.verification_request_source or kwargs["actor"] != self.test.actor:
            raise MigrationDenied("test_verification_request_required")

    def verification_plan(self, cursor, proof, plan_id, *, now):
        if self.test.verification_plans.get(plan_id) != (proof.migration_id, proof.kind, proof.request_id):
            raise MigrationDenied("test_verification_plan_required")

    def revoke(self, cursor, **kwargs):
        cursor.execute("UPDATE draft_history_marker SET active=FALSE")

    def bound_job(self, cursor, item, job, *, operation, now, due_at=None):
        item_id = item.item_id if isinstance(item, MigrationItemProof) else item["item_id"]
        job_id = job.job_id if hasattr(job, "job_id") else job["job_id"]
        row = cursor.execute("SELECT * FROM draft_continuation_bindings WHERE item_id=? AND job_id=?",
                             (item_id, job_id)).fetchone()
        if row is None or (due_at is not None and due_at != row["due_at"]):
            raise MigrationDenied("test_domain_binding_required")

    def bound_plan(self, cursor, item, plan_id, *, now):
        item_id = item.item_id if isinstance(item, MigrationItemProof) else item["item_id"]
        row = cursor.execute("SELECT 1 FROM draft_continuation_bindings WHERE item_id=? AND plan_id=?",
                             (item_id, plan_id)).fetchone()
        if row is None:
            raise MigrationDenied("test_domain_plan_required")

    def existing_artifact(self, cursor, source, *, endpoint, artifact_ref, now):
        if source is not self.test.artifact_source or endpoint != self.test.endpoint or artifact_ref != "exact-artifact":
            raise MigrationDenied("test_existing_artifact_required")


class JournalFixture(OutboxFixture):
    def setUp(self):
        super().setUp()
        with self.tx() as cursor:
            cursor.execute("CREATE TABLE IF NOT EXISTS draft_history_marker(active boolean)")
            cursor.execute("INSERT INTO draft_history_marker VALUES(TRUE)")
            cursor.execute("""CREATE TABLE IF NOT EXISTS draft_continuation_bindings (
                item_id text PRIMARY KEY,job_id bigint UNIQUE,due_at bigint,plan_id text)""")
            cursor.execute("INSERT INTO admin_accounts(account_id,username,password_hash,role,qq_id,enabled,version,created_at) "
                           "VALUES ('admin','test-admin','TEST_HASH','admin','9',1,1,?)", (self.now,))
            cursor.execute("INSERT INTO admin_sessions VALUES (?, 'admin',1,?,?,?)", ("a"*64, "b"*64, self.now+1000, self.now))
        self.authorization, self.baseline_source, self.effect_source, self.artifact_source = object(), object(), object(), object()
        self.source = EndpointKey(AccountKey("onebot-v11", str(secrets.randbelow(10**12)+10**12), "test"), "group", "700")
        self.source_actor = ActorKey(self.source.account, "user", "", "42")
        self.source_scope = ConversationScope("onebot-v11", "group", "700", bot_native_user_id=self.source.account.app_id)
        self.source_connection = psycopg.connect(self.database.dsn, autocommit=True)
        self.addCleanup(self.source_connection.close)
        key = int.from_bytes(hashlib.sha256(self.source.account.key.encode()).digest()[:8], "big", signed=True)
        self.source_connection.execute("SELECT pg_advisory_lock(%s)", (key,))
        pid = self.source_connection.execute("SELECT pg_backend_pid()").fetchone()[0]
        self.baseline = BaselineEvidence(self.source.account.key, secrets.token_hex(16), pid, key, "d"*64, self.now+1000)
        self.ports = HostPorts(self)
        self.routes = RouteService(tokens={self.account.key: self.token}, ports=self.ports)
        self.migration = Journal(self.routes)
        with self.tx() as cursor:
            self.routes.register_baseline(cursor, account=self.source.account, source=self.baseline_source, now=self.now)
        self.jobs = DurableJobStore(self.database)
        self.deliveries = DeliveryStore(self.database, recover_interrupted=False)
        self.ledger = MessageLedger(self.database)
        self.journal = TurnJournal(self.database)
        for store in (self.jobs, self.deliveries, self.ledger, self.journal):
            self.addCleanup(store.close)
        install_mapping(self)
        self.pepper = b"TEST_PEPPER_0123456789012345678901234"
        self.verifications = {}
        self.verification_request_source = object()
        self.verification_plans = {}


    def rotate_mapping(self):
        return rotate_mapping(self)

    def plan(self):
        with self.tx() as cursor:
            return self.migration.plan(cursor, source_endpoint=self.source, source_scope=self.source_scope, target_endpoint=self.endpoint,
                items=(("reminder", "1"),), authorization=self.authorization,
                baseline_source=self.baseline_source, deadline=self.now+500, now=self.now)


    def freeze(self, plan):
        self.ports.approved.add(plan.preview_digest)
        with self.tx() as cursor:
            return self.migration.freeze(cursor, migration_id=plan.migration_id, reviewed_digest=plan.preview_digest,
                                         authorization=self.authorization, now=self.now)


    def switch(self, plan):
        self.freeze(plan)
        with self.tx() as cursor:
            self.migration.begin_draining(cursor, migration_id=plan.migration_id, authorization=self.authorization, now=self.now)
            return self.migration.switch_target(cursor, migration_id=plan.migration_id, authorization=self.authorization, now=self.now)


    def complete(self, plan):
        with self.tx() as cursor:
            nonce = self.migration.begin_verification(cursor, migration_id=plan.migration_id, authorization=self.authorization, now=self.now)
        for kind in ("text", "file"):
            with self.tx() as cursor:
                proof = self.migration.issue_verification(cursor, migration_id=plan.migration_id,
                    kind=kind, actor=self.actor, source=self.verification_request_source, now=self.now)

            def register_plan(cursor, **kwargs):
                created = enqueue_plan(cursor, **kwargs)
                self.verification_plans[created.plan_id] = (proof.migration_id, proof.kind, proof.request_id)
                self.migration.bind_verification_plan(cursor, proof, plan_id=created.plan_id, now=self.now)
                return created

            with patch.object(receipt_fixture, "enqueue_plan", side_effect=register_plan):
                attempt = receipt_fixture.OfficialReceiptTests.attempt(self)
            receipt = receipt_fixture.OfficialReceiptTests.capture(self, attempt, {"id": "NATIVE_"+kind})
            self.assertEqual("committed", receipt_fixture.OfficialReceiptTests.finalize(self, receipt, journal=False).status)
            source = object()
            self.verifications[source] = VerificationEvidence(receipt, attempt.delivery_id, kind, nonce)
            with self.tx() as cursor:
                self.migration.record_verification(cursor, migration_id=plan.migration_id, source=source, now=self.now)
        with self.tx() as cursor:
            return self.migration.complete(cursor, migration_id=plan.migration_id, authorization=self.authorization, now=self.now)


    def route(self, endpoint=None, epoch=0, scope=None, operation="send"):
        endpoint = endpoint or self.source
        with self.tx() as cursor:
            return self.routes.assert_active_route(cursor, canonical_scope=scope or (self.source_scope if endpoint == self.source else endpoint.scope),
                endpoint=endpoint, route_epoch=epoch, operation=operation)


    def dispatch(self, *, endpoint=None, key="test-op"):
        with self.tx() as cursor:
            return self.routes.begin_dispatch(cursor, source_endpoint=self.source, source_scope=self.source_scope,
                physical_endpoint=endpoint or self.source, route_epoch=0, target_route_epoch=0,
                operation_ref=key, operation="send", source=self.effect_source, now=self.now)


    def prepare_job(self, plan):
        item_id = self.query("SELECT item_id FROM migration_items WHERE migration_id=?", (plan.migration_id,))[0][0]
        with self.tx() as cursor:
            proof = self.migration.issue_item(cursor, item_id=item_id, now=self.now)
            job, _ = self.jobs.enqueue(kind="migration.test", idempotency_key="migration:"+item_id,
                scope_key=self.endpoint.scope.key, payload={"item_id": item_id}, run_at=PARKED_AT,
                transaction_cursor=cursor, now=self.now)
            cursor.execute("INSERT INTO draft_continuation_bindings VALUES (?,?,?,NULL)", (item_id, job.job_id, self.now+10))
            self.migration.bind_item_job(cursor, proof, job_id=job.job_id, expected_kind=job.kind, now=self.now)
        return proof, job
