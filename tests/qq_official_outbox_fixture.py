"""Isolated PostgreSQL fixtures with a real account lease and fake identities."""
from contextlib import contextmanager
import secrets
import unittest

from tests.postgres_fixture import postgres_for_test
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey
from src.plugins.ai_chat.adapters.qq_official.lease import DedicatedAccountLease


class OutboxFixture(unittest.TestCase):
    def setUp(self):
        self.database = postgres_for_test(self)
        self.now = 1_800_000_000
        self.account = AccountKey("qq-official", "TEST_OUTBOX_" + secrets.token_hex(4), "test")
        self.endpoint = EndpointKey(self.account, "group", "TEST_GROUP")
        self.actor = ActorKey(self.account, "group_member", "TEST_GROUP", "TEST_MEMBER")
        self.provision(self.endpoint)
        self.account_lease = self.lease()
        self.token = self.account_lease.acquire()
        self.assertIsNotNone(self.token)

    @contextmanager
    def tx(self):
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            yield cursor
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def query(self, sql, args=()):
        with self.tx() as cursor:
            cursor.execute(sql, args)
            return cursor.fetchall() if sql.lstrip().upper().startswith("SELECT") else []

    def provision(self, endpoint):
        with self.tx() as cursor:
            cursor.execute("""INSERT INTO qq_official_accounts
                (account_key,enabled,gateway_state,created_at,updated_at)
                VALUES (?,TRUE,'idle',?,?) ON CONFLICT DO NOTHING""",
                (endpoint.account.key, self.now, self.now))
            cursor.execute("""INSERT INTO qq_official_endpoints
                (endpoint_key,account_key,kind,native_conversation_id,canonical_scope_key,
                 binding_state,membership_state,proactive_state,created_at,updated_at)
                VALUES (?,?,?,?,?,'active','present','denied',?,?)""",
                (endpoint.key, endpoint.account.key, endpoint.kind, endpoint.native_conversation_id,
                 endpoint.scope.key, self.now, self.now))

    def lease(self, account=None):
        lease = DedicatedAccountLease(self.database.dsn, account or self.account,
            schema=self.database.schema, clock=lambda: self.now)
        self.addCleanup(lease.close)
        return lease

    def window(self, *, endpoint=None, source="TEST_SOURCE", expires=None, max_replies=5):
        endpoint = endpoint or self.endpoint
        window = secrets.token_hex(16)
        with self.tx() as cursor:
            cursor.execute("""INSERT INTO qq_official_reply_windows
                (window_id,account_key,endpoint_key,endpoint_generation,route_epoch,
                 credential_kind,credential_value,issued_at,received_at,expires_at,
                 max_replies,state,created_at,updated_at)
                VALUES (?,?,?,1,1,'msg_id',?,?,?,?,?,'active',?,?)""",
                (window, endpoint.account.key, endpoint.key, source, self.now, self.now,
                 expires or self.now + 295, max_replies, self.now, self.now))
        return window
