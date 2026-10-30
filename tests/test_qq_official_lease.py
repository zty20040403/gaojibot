"""Real local PostgreSQL checks for the dedicated account fence and ingress DDL."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from alembic.config import Config
import psycopg
from psycopg import sql

from src.bot_storage.database import DatabaseError
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey
from tests.postgres_fixture import postgres_for_test

from src.plugins.ai_chat.adapters.qq_official.lease import (
    DedicatedAccountLease, LeaseLost, LeaseUnavailable, advisory_key_for, assert_epoch,
)


def migrate(database, method, revision):
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    with patch.dict(os.environ, AI_POSTGRES_DSN=database.dsn, AI_POSTGRES_SCHEMA=database.schema):
        method(config, revision)


class _LeaseFixture(unittest.TestCase):
    def setUp(self):
        self.database = postgres_for_test(self)
        self.account = AccountKey("qq-official", "TEST_APP", "test")
        self.provision(self.account)

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

    def provision(self, account, *, enabled=True, state="idle"):
        with self.tx() as cursor:
            cursor.execute("""INSERT INTO qq_official_accounts
                (account_key,enabled,gateway_state,created_at,updated_at) VALUES (?,?,?,?,?)""",
                (account.key, enabled, state, 100, 100))

    def lease(self, account=None, **kwargs):
        lease = DedicatedAccountLease(self.database.dsn, account or self.account,
                                      schema=self.database.schema, clock=lambda: 101, **kwargs)
        self.addCleanup(lease.close)
        return lease


class LeaseTests(_LeaseFixture):
    def test_competition_release_and_epoch_increment_preserve_checkpoint(self):
        with self.tx() as cursor:
            cursor.execute("UPDATE qq_official_accounts SET session_id=?,durable_seq=7 WHERE account_key=?",
                           ("TEST_SESSION", self.account.key))
        first, second = self.lease(), self.lease()
        token = first.acquire()
        self.assertEqual(token.epoch, 1)
        self.assertIsNone(second.acquire())
        self.assertIs(first.acquire(), token)
        for _ in range(3):
            self.assertTrue(first.probe())
        with self.tx() as cursor:
            assert_epoch(cursor, token)
        first.close()
        with self.tx() as cursor, self.assertRaises(LeaseLost):
            assert_epoch(cursor, token)
        replacement = second.acquire()
        self.assertEqual(replacement.epoch, 2)
        with self.tx() as cursor:
            row = cursor.execute("SELECT session_id,durable_seq FROM qq_official_accounts WHERE account_key=?",
                                 (self.account.key,)).fetchone()
        self.assertEqual((row["session_id"], row["durable_seq"]), ("TEST_SESSION", 7))

    def test_account_and_environment_have_separate_locks(self):
        accounts = (self.account, replace(self.account, app_id="TEST_OTHER"), replace(self.account, environment="sandbox"))
        for account in accounts[1:]:
            self.provision(account)
        tokens = [self.lease(account).acquire() for account in accounts]
        self.assertEqual(len({token.advisory_key for token in tokens}), 3)
        for token in tokens:
            with self.tx() as cursor:
                assert_epoch(cursor, token)

    def test_same_account_in_another_schema_still_competes(self):
        other_schema = self.database.schema + "_other"

        def clean():
            with psycopg.connect(self.database.dsn) as connection:
                connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(other_schema)))

        self.addCleanup(clean)
        with psycopg.connect(self.database.dsn) as connection:
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(other_schema)))
            connection.execute(sql.SQL("CREATE TABLE {}.qq_official_accounts (LIKE {}.qq_official_accounts INCLUDING ALL)")
                               .format(sql.Identifier(other_schema), sql.Identifier(self.database.schema)))
            connection.execute(sql.SQL("""INSERT INTO {}.qq_official_accounts
                (account_key,enabled,gateway_state,created_at,updated_at) VALUES (%s,TRUE,'idle',100,100)""")
                .format(sql.Identifier(other_schema)), (self.account.key,))
        first = self.lease()
        second = DedicatedAccountLease(self.database.dsn, self.account, schema=other_schema)
        self.addCleanup(second.close)
        first.acquire()
        self.assertIsNone(second.acquire())
        first.close()
        self.assertIsNotNone(second.acquire())

    def test_disabled_missing_and_paused_accounts_cannot_acquire(self):
        for state, enabled in (("disabled", False), ("paused", True)):
            account = replace(self.account, app_id="TEST_" + state)
            self.provision(account, enabled=enabled, state=state)
            with self.assertRaisesRegex(LeaseUnavailable, "not enabled"):
                self.lease(account).acquire()
        with self.assertRaisesRegex(LeaseUnavailable, "not enabled"):
            self.lease(replace(self.account, app_id="TEST_ABSENT")).acquire()
        with self.tx() as cursor:
            rows = cursor.execute("SELECT fencing_epoch,holder_id FROM qq_official_accounts").fetchall()
            self.assertTrue(all(row["fencing_epoch"] == 0 and row["holder_id"] is None for row in rows))

    def test_copied_token_cannot_become_a_live_fence(self):
        token = self.lease().acquire()
        cursor = Mock()
        with self.assertRaises(LeaseLost):
            assert_epoch(cursor, replace(token))
        cursor.execute.assert_not_called()
        self.assertNotIn("TEST_APP", repr(token))
        self.assertNotIn(token.holder_id, repr(token))

    def test_backend_death_is_rejected_before_takeover_or_probe(self):
        first = self.lease()
        token = first.acquire()
        with psycopg.connect(self.database.dsn, autocommit=True) as admin:
            stopped = admin.execute("SELECT pg_terminate_backend(%s,1000)", (token.backend_pid,)).fetchone()[0]
        self.assertTrue(stopped)
        with self.tx() as cursor, self.assertRaises(LeaseLost):
            assert_epoch(cursor, token)
        self.assertFalse(first.probe())
        self.assertTrue(first.lost)
        with self.assertRaises(LeaseLost):
            first.acquire()
        replacement = self.lease().acquire()
        self.assertGreater(replacement.epoch, token.epoch)
        with self.tx() as cursor:
            assert_epoch(cursor, replacement)

    def test_explicit_unlock_is_loss_not_transparent_reacquisition(self):
        lease = self.lease()
        token = lease.acquire()
        lease._connection.execute("SELECT pg_advisory_unlock(%s)", (token.advisory_key,))
        with self.tx() as cursor, self.assertRaises(LeaseLost):
            assert_epoch(cursor, token)
        self.assertFalse(lease.probe())
        with self.assertRaises(LeaseLost):
            lease.acquire()

    def test_pause_invalidates_current_epoch_and_survives_close(self):
        lease = self.lease()
        token = lease.acquire()
        with self.tx() as cursor:
            cursor.execute("UPDATE qq_official_accounts SET gateway_state='paused',pause_reason='auth' WHERE account_key=?",
                           (self.account.key,))
        with self.tx() as cursor, self.assertRaises(LeaseLost):
            assert_epoch(cursor, token)
        lease.close()
        with self.tx() as cursor:
            row = cursor.execute("SELECT gateway_state,holder_id FROM qq_official_accounts WHERE account_key=?",
                                 (self.account.key,)).fetchone()
        self.assertEqual(row["gateway_state"], "paused")
        self.assertIsNone(row["holder_id"])
        with self.assertRaises(LeaseUnavailable):
            self.lease().acquire()

    def test_short_fenced_transaction_serializes_takeover_after_backend_death(self):
        lease = self.lease()
        token = lease.acquire()
        with self.tx() as cursor:
            assert_epoch(cursor, token)
            with psycopg.connect(self.database.dsn, autocommit=True) as contender:
                self.assertTrue(contender.execute("SELECT pg_terminate_backend(%s,1000)", (token.backend_pid,)).fetchone()[0])
                # No timed sleep: NOWAIT proves the old transaction retains the
                # account-row lock even though its separate session lock died.
                with self.assertRaises(psycopg.errors.LockNotAvailable):
                    contender.execute(sql.SQL("SELECT account_key FROM {}.qq_official_accounts WHERE account_key=%s FOR UPDATE NOWAIT")
                                      .format(sql.Identifier(self.database.schema)), (self.account.key,))
        replacement = self.lease().acquire()
        self.assertEqual(replacement.epoch, token.epoch + 1)

    def test_connection_error_and_fence_errors_are_sanitized(self):
        leak = "password=SECRET_CREDENTIAL account=TEST_PRIVATE"
        with self.assertRaises(LeaseUnavailable) as error:
            self.lease(connect=Mock(side_effect=psycopg.OperationalError(leak))).acquire()
        self.assertNotIn(leak, str(error.exception))
        token = self.lease().acquire()
        cursor = Mock()
        cursor.execute.side_effect = RuntimeError(leak)
        with self.assertRaises(LeaseLost) as error:
            assert_epoch(cursor, token)
        self.assertNotIn(leak, str(error.exception))
        self.assertTrue(error.exception.__suppress_context__)

    def test_full_account_suffix_affects_lock_and_invalid_inputs_reject(self):
        accounts = [replace(self.account, app_id="TEST_" + "a" * 310 + suffix) for suffix in ("A", "B")]
        self.assertNotEqual(advisory_key_for(accounts[0]), advisory_key_for(accounts[1]))
        for account in (replace(self.account, platform="onebot-v11"), replace(self.account, app_id="x" * 1024)):
            with self.assertRaises(LeaseUnavailable):
                advisory_key_for(account)


class LeaseMigrationTests(_LeaseFixture):
    # A separate fixture class permits empty migration round-trips without
    # touching the runtime lease tests' schema or any fixed full-suite schema.
    def test_ddl_rejects_cross_account_endpoint_and_empty_ids(self):
        endpoint = EndpointKey(self.account, "group", "TEST_GROUP")
        other = replace(self.account, app_id="TEST_OTHER")
        self.provision(other)
        with self.tx() as cursor:
            cursor.execute("""INSERT INTO qq_official_endpoints
                (endpoint_key,account_key,kind,native_conversation_id,canonical_scope_key,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?)""", (endpoint.key, self.account.key, "group", "TEST_GROUP", endpoint.scope.key, 100, 100))
        for account, native_id in ((other, "TEST_MSG"), (self.account, "")):
            with self.assertRaises(DatabaseError) as error, self.tx() as cursor:
                cursor.execute("""INSERT INTO qq_official_messages
                    (message_id,account_key,endpoint_key,native_message_id,direction,created_at)
                    VALUES (?,?,?,?,?,?)""", ("test-message", account.key, endpoint.key, native_id, "inbound", 100))
            self.assertIsInstance(error.exception.__cause__,
                psycopg.errors.ForeignKeyViolation if account == other else psycopg.errors.CheckViolation)

    def test_multiple_replies_can_share_reference_and_index_ambiguity_is_recordable(self):
        endpoint = EndpointKey(self.account, "group", "TEST_GROUP")
        with self.tx() as cursor:
            cursor.execute("""INSERT INTO qq_official_endpoints
                (endpoint_key,account_key,kind,native_conversation_id,canonical_scope_key,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?)""", (endpoint.key, self.account.key, "group", "TEST_GROUP", endpoint.scope.key, 100, 100))
            for msg in ("TEST_ONE", "TEST_TWO"):
                cursor.execute("""INSERT INTO qq_official_messages
                    (message_id,account_key,endpoint_key,native_message_id,direction,msg_idx,ref_msg_idx,created_at)
                    VALUES (?,?,?,?,?,?,?,?)""", (msg, self.account.key, endpoint.key, msg, "inbound", "same", "target", 100))
            cursor.execute("""INSERT INTO qq_official_message_indexes
                (endpoint_key,account_key,index_kind,index_value,native_message_id,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?)""", (endpoint.key, self.account.key, "msg_idx", "same", "TEST_ONE", 100, 100))
            cursor.execute("UPDATE qq_official_message_indexes SET ambiguous=TRUE WHERE endpoint_key=?", (endpoint.key,))
            self.assertEqual(cursor.execute("SELECT COUNT(*) AS n FROM qq_official_messages").fetchone()["n"], 2)
            self.assertTrue(cursor.execute("SELECT ambiguous FROM qq_official_message_indexes").fetchone()["ambiguous"])



if __name__ == "__main__":
    unittest.main()
