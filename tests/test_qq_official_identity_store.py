"""Owned PostgreSQL schemas and fake bot observations; no platform requests."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import unittest
from unittest.mock import Mock

import psycopg

from src.plugins.ai_chat.adapters.qq_official import identity_store
from src.plugins.ai_chat.adapters.qq_official.lease import DedicatedAccountLease, LeaseLost
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey
from tests.postgres_fixture import postgres_for_test

NOW = 1_800_000_000


def ready(native_id="TEST_BOT:原样/+", *, bot=True):
    return {"op": 0, "t": "READY", "s": 1, "d": {
        "session_id": "TEST_PRIVATE_SESSION", "user": {"id": native_id, "bot": bot,
        "username": "TEST_PRIVATE_NICK", "avatar": "https://example.invalid/PRIVATE_AVATAR"},
        "requester": "TEST_PRIVATE_REQUESTER", "extra": "TEST_RAW_PAYLOAD"}}


class IdentityStoreTests(unittest.TestCase):
    def setUp(self):
        self.database = postgres_for_test(self)
        self.account = AccountKey("qq-official", "TEST_APP:非数字/+", "test")
        self.provision(self.account)
        self.lease = self.make_lease(self.account)
        self.token = self.lease.acquire()

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

    def provision(self, account):
        with self.tx() as cursor:
            cursor.execute("""INSERT INTO qq_official_accounts
                (account_key,enabled,gateway_state,created_at,updated_at)
                VALUES (?,TRUE,'idle',?,?)""", (account.key, NOW - 10, NOW - 10))

    def make_lease(self, account):
        lease = DedicatedAccountLease(self.database.dsn, account, schema=self.database.schema, clock=lambda: NOW - 5)
        self.addCleanup(lease.close)
        return lease

    def row(self, account=None):
        with self.tx() as cursor:
            return dict(cursor.execute("SELECT * FROM qq_official_accounts WHERE account_key=?",
                                       ((account or self.account).key,)).fetchone())

    def observe_ready(self, payload=None, *, now=NOW, token=None):
        with self.tx() as cursor:
            return identity_store.observe_ready(cursor, token or self.token, ready() if payload is None else payload, now)

    def observe_self(self, payload=None, *, now=NOW, token=None):
        with self.tx() as cursor:
            return identity_store.observe_self_api(cursor, token or self.token,
                {"id": "TEST_BOT:原样/+", "bot": True} if payload is None else payload, now)

    def snapshot(self, account=None):
        with self.tx() as cursor:
            return identity_store.snapshot(cursor, account or self.account)

    def test_unknown_and_missing_snapshots_are_read_only(self):
        before = self.row()
        self.assertIsNone(self.snapshot())
        self.assertIsNone(self.snapshot(replace(self.account, app_id="TEST_ABSENT")))
        self.assertEqual(self.row(), before)

    def test_ready_unknown_to_verified_preserves_exact_bot_domain(self):
        result = self.observe_ready()
        self.assertEqual((result.status, result.revision, result.changed), ("verified", 1, True))
        author = self.snapshot()
        self.assertEqual(author.bot_actor, ActorKey(self.account, "bot", "", "TEST_BOT:原样/+"))
        self.assertEqual((author.source, author.revision), ("ready", 1))
        row = self.row()
        self.assertEqual(row["bot_identity_verified_at"], NOW)
        self.assertNotIn("TEST_BOT", repr(author))
        self.assertNotIn("TEST_APP", repr(author))

    def test_successful_self_api_is_an_independent_first_source(self):
        result = self.observe_self({"id": " 00123 ", "bot": True})
        self.assertEqual(result.status, "verified")
        self.assertEqual(self.snapshot().source, "self_api")
        self.assertEqual(self.snapshot().bot_actor.native_user_id, " 00123 ")




    def test_account_and_environment_same_native_strings_do_not_share_identity(self):
        accounts = [self.account, replace(self.account, app_id="TEST_OTHER_APP"),
                    replace(self.account, environment="sandbox")]
        tokens = [self.token]
        for account in accounts[1:]:
            self.provision(account)
            tokens.append(self.make_lease(account).acquire())
        for token in tokens:
            self.observe_ready(token=token)
        actors = [self.snapshot(account).bot_actor.key for account in accounts]
        self.assertEqual(len(set(actors)), 3)
        self.observe_self({"id": "TEST_OTHER_BOT", "bot": True}, token=tokens[1])
        self.assertIsNotNone(self.snapshot(accounts[0]))
        self.assertEqual(self.snapshot(accounts[1]).bot_actor.native_user_id,'TEST_OTHER_BOT')
        self.assertIsNotNone(self.snapshot(accounts[2]))



    def test_caller_rollback_owns_identity_transaction(self):
        with self.assertRaisesRegex(RuntimeError, "TEST rollback"):
            with self.tx() as cursor:
                identity_store.observe_ready(cursor, self.token, ready(), NOW)
                self.assertIsNotNone(identity_store.snapshot(cursor, self.account))
                self.assertIsNone(self.snapshot())  # no internal commit
                raise RuntimeError("TEST rollback")
        self.assertIsNone(self.snapshot())
        self.assertEqual(self.row()["bot_identity_revision"], 0)



    def test_closed_or_copied_fence_fails_before_payload_or_sql_access(self):
        cursor = Mock()
        with self.assertRaises(LeaseLost):
            identity_store.observe_ready(cursor, replace(self.token), object(), NOW)
        cursor.execute.assert_not_called()
        self.lease.close()
        with self.assertRaises(LeaseLost):
            identity_store.observe_self_api(cursor, self.token, object(), NOW)
        cursor.execute.assert_not_called()
        self.assertIsNone(self.snapshot())

    def test_dead_lease_backend_prevents_observation_write(self):
        with psycopg.connect(self.database.dsn, autocommit=True) as admin:
            self.assertTrue(admin.execute("SELECT pg_terminate_backend(%s,1000)", (self.token.backend_pid,)).fetchone()[0])
        with self.assertRaises(LeaseLost):
            self.observe_ready()
        self.assertIsNone(self.snapshot())







if __name__ == "__main__":
    unittest.main()

    def test_authenticated_ready_can_refresh_identity_without_sdk_proof(self):
        self.observe_ready()
        result=self.observe_ready({'op':0,'t':'READY','d':{'user':{'id':'TEST_NEW_BOT'}}})
        self.assertEqual(result.status,'verified')
        self.assertEqual(self.snapshot().bot_actor.native_user_id,'TEST_NEW_BOT')
