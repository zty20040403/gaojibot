"""Owned PostgreSQL schemas and fake bot observations; no platform requests."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import json
import threading
import unittest
from unittest.mock import Mock

import psycopg

from src.plugins.ai_chat.adapters.qq_official import identity_store
from src.plugins.ai_chat.adapters.qq_official.identity import MAX_OBSERVATION_TIME, OFFICIAL_SDK_COMMIT
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
        self.assertEqual(json.loads(row["bot_identity_contract"])["c"], OFFICIAL_SDK_COMMIT)
        self.assertNotIn("TEST_BOT", repr(author))
        self.assertNotIn("TEST_APP", repr(author))

    def test_successful_self_api_is_an_independent_first_source(self):
        result = self.observe_self({"id": " 00123 ", "bot": True})
        self.assertEqual(result.status, "verified")
        self.assertEqual(self.snapshot().source, "self_api")
        self.assertEqual(self.snapshot().bot_actor.native_user_id, " 00123 ")

    def test_repeated_observation_is_fully_idempotent_without_revision_or_time_refresh(self):
        self.observe_ready()
        original = self.row()
        for now in (NOW, NOW + 20, NOW - 20):
            result = self.observe_ready(now=now)
            self.assertEqual((result.status, result.revision, result.changed), ("verified", 1, False))
            self.assertEqual(self.row(), original)

    def test_second_source_adds_bounded_evidence_without_changing_revision(self):
        self.observe_ready()
        result = self.observe_self(now=NOW + 1)
        self.assertEqual((result.status, result.revision, result.changed), ("verified", 1, True))
        row = self.row()
        self.assertEqual(row["bot_identity_source"], "ready_and_self_api")
        self.assertEqual(row["bot_identity_verified_at"], NOW)
        self.assertEqual(json.loads(row["bot_identity_contract"])["e"], [["ready", NOW], ["self_api", NOW + 1]])
        self.observe_self(now=NOW + 100)
        self.assertEqual(self.row(), row)

    def test_conflict_is_sticky_after_new_connection_and_new_lease(self):
        self.observe_ready()
        original_id = self.snapshot().bot_actor.native_user_id
        result = self.observe_self({"id": "TEST_CONFLICTING_BOT", "bot": True}, now=NOW + 1)
        self.assertEqual((result.status, result.revision, result.changed), ("mismatch", 2, True))
        row = self.row()
        self.assertEqual(row["bot_native_id"], original_id)
        self.assertEqual(row["bot_identity_source"], "mismatch")
        self.assertEqual(json.loads(row["bot_identity_contract"])["x"], ["self_api", NOW + 1])
        self.assertNotIn("TEST_CONFLICTING_BOT", row["bot_identity_contract"])
        self.assertIsNone(self.snapshot())
        self.lease.close()
        token = self.make_lease(self.account).acquire()
        self.assertGreater(token.epoch, self.token.epoch)
        before = self.row()
        for payload in (ready(), ready("TEST_THIRD_BOT"), {"not": "ready"}):
            result = self.observe_ready(payload, token=token, now=NOW + 3)
            self.assertEqual((result.status, result.revision, result.changed), ("mismatch", 2, False))
            self.assertEqual(self.row(), before)
        self.assertIsNone(self.snapshot())

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
        self.assertIsNone(self.snapshot(accounts[1]))
        self.assertIsNotNone(self.snapshot(accounts[2]))

    def test_rejected_payloads_do_not_store_or_discard_identity(self):
        original = self.row()
        for payload in (ready(bot=False), ready(bot=1), ready("BAD\x00ID"),
                        {"op": 0, "t": "RESUMED", "d": ready()["d"]},
                        {"op": 0, "t": "READY", "d": {"session_id": "TEST_SESSION", "user": {"user_openid": "TEST_HUMAN", "bot": True}}}):
            with self.subTest(payload_type=payload.get("t")):
                self.assertFalse(self.observe_ready(payload).changed)
                self.assertEqual(self.row(), original)
        for payload in ({"id": "TEST_BOT", "bot": True, "code": 40034006},
                        {"app_id": self.account.app_id, "bot": True},
                        {"member_openid": "TEST_HUMAN", "bot": True}):
            self.assertFalse(self.observe_self(payload).changed)
        self.observe_ready()
        verified = self.row()
        self.assertFalse(self.observe_self({"id": "TEST_WRONG", "bot": False}).changed)
        self.assertEqual(self.row(), verified)

    def test_compact_contract_does_not_store_payload_nickname_or_requester(self):
        self.observe_ready(now=MAX_OBSERVATION_TIME - 2)
        self.observe_self(now=MAX_OBSERVATION_TIME - 1)
        self.observe_ready(ready("TEST_CONFLICT"), now=MAX_OBSERVATION_TIME)
        row = self.row()
        self.assertLessEqual(len(row["bot_identity_contract"].encode()), 256)
        stored = json.dumps(row)
        for value in ("TEST_PRIVATE_SESSION", "TEST_PRIVATE_NICK", "PRIVATE_AVATAR",
                      "TEST_PRIVATE_REQUESTER", "TEST_RAW_PAYLOAD", "TEST_CONFLICT"):
            self.assertNotIn(value, stored)

    def test_caller_rollback_owns_identity_transaction(self):
        with self.assertRaisesRegex(RuntimeError, "TEST rollback"):
            with self.tx() as cursor:
                identity_store.observe_ready(cursor, self.token, ready(), NOW)
                self.assertIsNotNone(identity_store.snapshot(cursor, self.account))
                self.assertIsNone(self.snapshot())  # no internal commit
                raise RuntimeError("TEST rollback")
        self.assertIsNone(self.snapshot())
        self.assertEqual(self.row()["bot_identity_revision"], 0)

    def test_concurrent_duplicates_advance_revision_once(self):
        barrier = threading.Barrier(2)

        def observe(_):
            barrier.wait(timeout=10)
            return self.observe_ready()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(observe, range(2)))
        self.assertEqual([result.revision for result in results], [1, 1])
        self.assertEqual(sum(result.changed for result in results), 1)
        self.assertEqual(self.snapshot().revision, 1)

    def test_concurrent_conflicting_observations_never_overwrite_first_author(self):
        barrier = threading.Barrier(2)

        def observe(native):
            barrier.wait(timeout=10)
            result = self.observe_ready(ready(native))
            return native, result

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(observe, ("TEST_FIRST", "TEST_SECOND")))
        first_native = next(native for native, result in results if result.status == "verified")
        self.assertEqual(self.row()["bot_native_id"], first_native)
        self.assertEqual(self.row()["bot_identity_revision"], 2)
        self.assertIsNone(self.snapshot())

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

    def test_loss_after_read_is_checked_again_before_write(self):
        with self.assertRaises(LeaseLost):
            with self.tx() as cursor:
                database, token = self.database, self.token

                class LoseAfterRead:
                    def execute(self, statement, parameters=()):
                        result = cursor.execute(statement, parameters)
                        if statement.startswith("SELECT account_key,bot_native_id"):
                            with psycopg.connect(database.dsn, autocommit=True) as admin:
                                admin.execute("SELECT pg_terminate_backend(%s,1000)", (token.backend_pid,))
                        return result

                identity_store.observe_ready(LoseAfterRead(), self.token, ready(), NOW)
        self.assertIsNone(self.snapshot())

    def test_db_fault_after_update_is_safe_and_rolls_back_with_caller(self):
        with self.assertRaisesRegex(identity_store.IdentityStoreError, "storage operation failed") as caught:
            with self.tx() as cursor:

                class FailAfterWrite:
                    def execute(self, statement, parameters=()):
                        result = cursor.execute(statement, parameters)
                        if statement.startswith("UPDATE qq_official_accounts SET"):
                            raise RuntimeError("TEST_PRIVATE_DATABASE_PARAMETERS")
                        return result

                identity_store.observe_ready(FailAfterWrite(), self.token, ready(), NOW)
        self.assertNotIn("TEST_PRIVATE", str(caught.exception))
        self.assertIsNone(self.snapshot())

    def test_invalid_stored_contract_never_becomes_an_author_or_gets_overwritten(self):
        self.observe_ready()
        for contract in (None, "{}", '{"private":"TEST_PRIVATE_SECRET"}',
                         '{"v":true,"c":"wrong","e":[]}'):
            with self.tx() as cursor:
                cursor.execute("UPDATE qq_official_accounts SET bot_identity_contract=? WHERE account_key=?",
                               (contract, self.account.key))
            before = self.row()
            with self.assertRaisesRegex(identity_store.IdentityStoreError, "stored evidence is invalid") as caught:
                self.snapshot()
            self.assertNotIn("TEST_PRIVATE", str(caught.exception))
            with self.assertRaises(identity_store.IdentityStoreError):
                self.observe_ready()
            self.assertEqual(self.row(), before)

    def test_invalid_observation_time_and_account_fail_with_fixed_diagnostics(self):
        for now in (-1, True, MAX_OBSERVATION_TIME + 1):
            with self.assertRaisesRegex(identity_store.IdentityStoreError, "observation time is invalid"):
                self.observe_ready(now=now)
        cursor = Mock()
        with self.assertRaisesRegex(identity_store.IdentityStoreError, "account is invalid"):
            identity_store.snapshot(cursor, AccountKey("onebot-v11", "TEST_PRIVATE_ID", "test"))
        cursor.execute.assert_not_called()
        self.assertIsNone(self.snapshot())

    def test_revision_exhaustion_still_persists_blocking_conflict(self):
        self.observe_ready()
        with self.tx() as cursor:
            cursor.execute("UPDATE qq_official_accounts SET bot_identity_revision=? WHERE account_key=?",
                           (MAX_OBSERVATION_TIME, self.account.key))
        result = self.observe_self({"id": "TEST_CONFLICT", "bot": True})
        self.assertEqual((result.status, result.revision), ("mismatch", MAX_OBSERVATION_TIME))
        self.assertIsNone(self.snapshot())


if __name__ == "__main__":
    unittest.main()
