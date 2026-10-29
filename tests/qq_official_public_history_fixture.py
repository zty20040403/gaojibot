"""Formal Alembic tables and explicit library guard fixtures, without test methods."""


from src.plugins.ai_chat.adapters.qq_official.migration_history import (
    HistoryUnavailable, activate_public_history, prepare_public_history, read_public_history,
)
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.context_store import ContextStore
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.long_term_memory import LongTermMemoryStore
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey
from tests.qq_official_outbox_fixture import OutboxFixture


class PublicHistoryFixture(OutboxFixture):
    def setUp(self):
        super().setUp()
        with self.tx() as cursor:
            # A test-only host route/approval implementation. Production must use
            # root's actual migration journal, not copy this synthetic table.
            cursor.execute("""CREATE TABLE IF NOT EXISTS draft_history_approvals (
                migration_id text PRIMARY KEY, route_id text, epoch bigint,
                source_scope text, target_endpoint text, phase text,
                approved_digest text, source_allowed boolean)""")
        self.source = ConversationScope("onebot-v11", "group", "700")
        self.ledger = MessageLedger(self.database)
        self.context = ContextStore(self.database)
        self.memory = LongTermMemoryStore(self.database)
        self.addCleanup(self.ledger.close)
        self.addCleanup(self.context.close)
        self.addCleanup(self.memory._state._connection.close)
        self.first = self.source_message("same-native", "PUBLIC FIRST")
        self.second = self.source_message("legacy-two", "PUBLIC SECOND")
        self.target_message = self.ledger.record_message(
            self.endpoint.scope, native_message_id="same-native", sender_native_user_id=self.actor.native_user_id,
            sender_display="Original official author", body=MessageBody((TextNode(0, "TARGET ONLY"),)),
            occurred_at=self.now, actor=self.actor, source_endpoint=self.endpoint,
        )
        with self.tx() as cursor:
            source_endpoint=EndpointKey(AccountKey('onebot-v11','TEST_HISTORY_BOT','test'),'group','700')
            cursor.execute("""INSERT INTO admin_accounts
                (account_id,username,password_hash,role,qq_id,enabled,version,created_at)
                VALUES ('history_admin','history_admin','TEST_HASH','admin','9',1,1,?)""",(self.now,))
            cursor.execute("""INSERT INTO migration_runtime_baselines
                (account_key,runtime_id,revision,backend_pid,advisory_key,deployment_digest,expires_at,ready_at,state)
                VALUES (?,'history_runtime',1,pg_backend_pid(),0,?,?,?,'ready')""",
                (source_endpoint.account.key,'d'*64,self.now+1000,self.now))
            cursor.execute("""INSERT INTO migration_routes
                (route_id,source_scope_key,source_endpoint_key,target_scope_key,target_endpoint_key,
                 source_account_key,target_account_key,target_generation,target_endpoint_epoch,epoch,phase,
                 active_endpoint_key,baseline_runtime_id,baseline_revision)
                VALUES ('route',?,?,?,?,?,?,1,1,7,'planned',?,'history_runtime',1)""",
                (self.source.key,source_endpoint.key,self.endpoint.scope.key,self.endpoint.key,
                 source_endpoint.account.key,self.account.key,source_endpoint.key))
            cursor.execute("""INSERT INTO migration_journals
                (migration_id,route_id,preview_digest,preview_json,creator_id,creator_version,deadline,created_at)
                VALUES ('migration','route',?,'{}','history_admin',1,?,?)""",('d'*64,self.now+1000,self.now))
            cursor.execute("""INSERT INTO draft_history_approvals VALUES
                ('migration','route',7,?,?,'planned','',TRUE)""", (self.source.key, self.endpoint.key))

    def source_message(self, native, text, *, kind="chat"):
        return self.ledger.record_message(self.source, native_message_id=native,
            sender_native_user_id="42", sender_display="Original legacy author",
            body=MessageBody((TextNode(0, text),)), occurred_at=self.now - 10,
            message_kind=kind)

    def guard(self, cursor, check):
        assert_epoch(cursor, self.token)
        row = cursor.execute("SELECT * FROM draft_history_approvals WHERE migration_id=? FOR UPDATE",
                             (check.migration_id,)).fetchone()
        if row is None or (row["route_id"], row["epoch"], row["source_scope"], row["target_endpoint"]) != (
                check.route_id, check.route_epoch, check.source_scope_key, check.target_endpoint_key):
            raise HistoryUnavailable("route_rejected")
        endpoint = cursor.execute("SELECT * FROM qq_official_endpoints WHERE endpoint_key=? FOR SHARE",
                                  (check.target_endpoint_key,)).fetchone()
        if (endpoint is None or not row["source_allowed"] or endpoint["binding_state"] != "active"
                or endpoint["membership_state"] != "present"
                or endpoint["generation"] != check.target_endpoint_generation
                or endpoint["route_epoch"] != check.target_endpoint_route_epoch):
            raise HistoryUnavailable("endpoint_rejected")
        if check.target_scope_key != EndpointKey.parse(check.target_endpoint_key).scope.key:
            raise HistoryUnavailable("route_rejected")
        if check.purpose in {"activate", "read"} and (
                row["phase"] != "completed" or row["approved_digest"] != check.manifest_digest):
            raise HistoryUnavailable("approval_rejected")

    def prepare(self, *, chapters=(), notes=()):
        with self.tx() as cursor:
            return prepare_public_history(cursor, guard=self.guard, migration_id="migration",
                route_id="route", route_epoch=7, source_scope=self.source, target_endpoint=self.endpoint,
                endpoint_generation=1, endpoint_route_epoch=1, chapter_ids=chapters,
                public_note_ids=notes, now=self.now)

    def activate(self, preview):
        with self.tx() as cursor:
            cursor.execute("UPDATE draft_history_approvals SET phase='completed',approved_digest=?",
                           (preview.manifest_digest,))
            activate_public_history(cursor, guard=self.guard, grant_id=preview.grant_id,
                                    approved_manifest_digest=preview.manifest_digest)

    def read(self, preview, **kwargs):
        with self.tx() as cursor:
            return read_public_history(cursor, guard=self.guard, grant_id=preview.grant_id,
                target_endpoint=kwargs.get("endpoint", self.endpoint), route_epoch=kwargs.get("epoch", 7),
                limit=kwargs.get("limit", 100))

    def chapter(self):
        candidate = self.context.capture_candidate(self.ledger, self.source, settled=True)
        self.assertIsNotNone(candidate)
        return self.context.publish_generated(candidate, ("Old full", "Old medium", "Old short"))

    def note(self, *, source_id=None, scope="group:700", kind="group", text="PUBLIC NOTE"):
        return self.memory.add(scope, kind, text, creator_user_id=42, source_message_id=source_id)[0]
