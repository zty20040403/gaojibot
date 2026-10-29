"""Real 0065 publication constraints and the production DatabaseCalls boundary.

The source-callback table is an explicitly synthetic fence; real source authority
and immutable bytes are separately exercised by the file-links integration test.
"""
from __future__ import annotations

import asyncio
import hashlib
import secrets
import tempfile
import threading
import traceback
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from src.bot_storage.async_calls import DatabaseCalls
from src.bot_storage.database import DatabaseError
from tests.qq_official_file_fixture import FileFixture, PDF
from src.plugins.ai_chat.adapters.qq_official.file_artifacts import foreground_source

from src.plugins.ai_chat.adapters.qq_official.file_links import (
    ArtifactRef,
    FileCredentialVault,
    FileLinkPublisher,
    LinkError,
    Publication,
)
from src.plugins.ai_chat.adapters.qq_official.file_publications import MAX_BIGINT, PgPublicationStore

NOW = 1_800_000_000
ORIGIN = "https://download.example.test"
_AUTHORITY_FIXTURE = """
CREATE TABLE IF NOT EXISTS test_publication_authority (
    artifact_id TEXT PRIMARY KEY REFERENCES qq_official_file_artifacts(artifact_id),
    account_key TEXT NOT NULL, endpoint_key TEXT NOT NULL, actor_key TEXT NOT NULL,
    account_generation BIGINT NOT NULL, job_generation BIGINT NOT NULL,
    active BOOLEAN NOT NULL, deadline BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS test_authority_observations (marker TEXT NOT NULL);
"""


class PgPublicationTests(FileFixture, unittest.IsolatedAsyncioTestCase):
    @contextmanager
    def transaction(self):
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            cursor.execute("BEGIN")
            yield cursor
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    async def asyncSetUp(self):
        self.time = NOW
        self.calls = DatabaseCalls(concurrency=8)
        self.addAsyncCleanup(self.calls.close)
        self.content = PDF
        self.artifact = self.register_artifact()
        self.request_key = hashlib.sha256(b"SYNTHETIC_LOGICAL_REQUEST").hexdigest()
        self.expected = ("TEST_ACCOUNT:prod", "TEST_ENDPOINT:group", "TEST_ACTOR:member", 1, 1)
        self.authority_threads = []
        with self.transaction() as cursor:
            cursor.execute(_AUTHORITY_FIXTURE)
            cursor.execute("INSERT INTO test_publication_authority VALUES (?,?,?,?,?,?,TRUE,?)",
                (self.artifact.artifact_id, *self.expected, NOW + 3600))
        self.store = self.make_store(publication_authority=self.authority)

    def register_artifact(self, *, deadline=NOW + 3600):
        with self.tx() as cursor:
            source = foreground_source(cursor, token=self.token, handle=self.handle, job=self.job,
                admission=self.admission, now=self.now, pending_deadline_at=deadline)
        item = self.files.register(source, content=self.content, filename="fake.pdf", logical_request_key=secrets.token_hex(32))
        return ArtifactRef(item.artifact_id, item.sha256, item.byte_size, item.mime, item.filename)

    def authority(self, cursor, artifact, expiry):
        self.authority_threads.append(threading.get_ident())
        row = cursor.execute("SELECT * FROM test_publication_authority WHERE artifact_id=? FOR UPDATE",
                             (artifact.artifact_id,)).fetchone()
        return (row is not None and row["active"] is True and expiry <= row["deadline"]
                and tuple(row[name] for name in ("account_key", "endpoint_key", "actor_key",
                    "account_generation", "job_generation")) == self.expected)

    def make_store(self, **kwargs):
        return PgPublicationStore(transaction_factory=self.transaction, calls=self.calls,
                                  clock=lambda: self.time, **kwargs)

    def publication(self, **kwargs):
        token = secrets.token_urlsafe(32)
        row = Publication(secrets.token_hex(16), secrets.token_hex(16), self.request_key,
            self.artifact, hashlib.sha256(token.encode()).hexdigest(),
            hashlib.sha256((ORIGIN + "/files/" + token).encode()).hexdigest(),
            ORIGIN, NOW, NOW + 60)
        return replace(row, **kwargs)

    def count(self):
        with self.transaction() as cursor:
            return cursor.execute("SELECT COUNT(*) AS n FROM qq_official_file_publications").fetchone()["n"]

    async def test_create_all_lookup_methods_and_fresh_adapter_restore(self):
        row = await self.store.create(self.publication())
        reader = self.make_store()  # No create authority needed for existing reads.
        self.assertEqual(await reader.get(row.publication_ref), row)
        self.assertEqual(await reader.by_request(row.request_key), row)
        self.assertEqual(await reader.by_digest(row.token_digest), row)
        self.assertEqual(await reader.by_credential(row.credential_ref), row)
        self.assertIsNone(await reader.get("b" * 32))
        self.assertTrue(self.authority_threads)
        self.assertNotIn(threading.get_ident(), self.authority_threads)
        self.assertNotIn(self.artifact.artifact_id, repr(reader))

    async def test_default_deny_and_strict_true_callback(self):
        for result in (None, False, 0, 1, "yes", object()):
            store = self.make_store(publication_authority=lambda cursor, artifact, expiry, result=result: result)
            with self.subTest(result=type(result)), self.assertRaisesRegex(LinkError, "publication_storage_unavailable"):
                await store.create(self.publication())
        with self.assertRaises(LinkError):
            await self.make_store().create(self.publication())
        self.assertEqual(self.count(), 0)

    async def test_authority_exception_and_fingerprint_mismatch_rollback_same_transaction(self):
        def authority(cursor, artifact, expiry):
            cursor.execute("INSERT INTO test_authority_observations VALUES (?)", ("FAKE_PRIVATE_MARKER",))
            return True

        store = self.make_store(publication_authority=authority)
        with self.assertRaises(LinkError):
            await store.create(self.publication(artifact=replace(self.artifact, filename="wrong.pdf")))
        with self.transaction() as cursor:
            self.assertEqual(cursor.execute("SELECT COUNT(*) AS n FROM test_authority_observations").fetchone()["n"], 0)

        def failed(*args):
            raise RuntimeError("FAKE_PRIVATE_DATABASE_PARAMETER")

        store = self.make_store(publication_authority=failed)
        try:
            await store.create(self.publication())
        except LinkError as error:
            self.assertNotIn("FAKE_PRIVATE", "".join(traceback.format_exception(error)))
        else:
            self.fail("exception expected")
        self.assertEqual(self.count(), 0)

    async def test_source_account_environment_endpoint_actor_and_generations_are_checked(self):
        original = self.expected
        for field, replacement in ((0, "TEST_ACCOUNT:sandbox"), (0, "TEST_OTHER_ACCOUNT:prod"),
                (1, "TEST_ENDPOINT:private"), (2, "TEST_OTHER_ACTOR:member"), (3, 2), (4, 2)):
            changed = list(original)
            changed[field] = replacement
            self.expected = tuple(changed)
            with self.subTest(field=field, value=replacement), self.assertRaises(LinkError):
                await self.store.create(self.publication())
        self.expected = original
        with self.transaction() as cursor:
            cursor.execute("UPDATE test_publication_authority SET active=FALSE")
        with self.assertRaises(LinkError):
            await self.store.create(self.publication())
        self.assertEqual(self.count(), 0)

    async def test_twenty_concurrent_creates_one_winner(self):
        # Callback allows parallel SHARE locks so DB unique index is tested,
        # rather than serializing all contenders behind a synthetic source row.
        store = self.make_store(publication_authority=lambda *args: True)
        rows = await asyncio.gather(*(store.create(self.publication()) for _ in range(20)))
        self.assertEqual(len({row.publication_ref for row in rows}), 1)
        self.assertEqual(len({row.credential_ref for row in rows}), 1)
        self.assertEqual(self.count(), 1)

    async def test_same_key_different_semantics_never_overwrite(self):
        first = await self.store.create(self.publication())
        for changes in ({"expires_at": NOW + 61}, {"origin": "https://other.example.test"},
                        {"artifact": replace(self.artifact, sha256="e" * 64)}):
            with self.subTest(changes=changes), self.assertRaises(LinkError):
                await self.store.create(self.publication(**changes))
        self.assertEqual(await self.store.get(first.publication_ref), first)

    async def test_credential_and_digest_unique_conflicts_safe_and_atomic(self):
        first = await self.store.create(self.publication())
        for changes in ({"credential_ref": first.credential_ref}, {"token_digest": first.token_digest},
                        {"publication_ref": first.publication_ref}):
            with self.assertRaisesRegex(LinkError, "publication_storage_unavailable"):
                await self.store.create(self.publication(request_key=secrets.token_hex(32), **changes))
        self.assertEqual(self.count(), 1)

    async def test_ttl_and_strict_field_validation(self):
        cases = (("created_at", True), ("created_at", NOW + 1), ("expires_at", NOW),
                 ("expires_at", NOW + 3601), ("expires_at", 1.5), ("served_complete_count", True),
                 ("served_complete_count", MAX_BIGINT + 1), ("state", "revoked"),
                 ("origin", ORIGIN + "?"))
        for key, value in cases:
            row = self.publication()
            object.__setattr__(row, key, value)  # Malformed trusted DB/internal object defense.
            with self.subTest(key=key, value=value), self.assertRaises(LinkError):
                await self.store.create(row)
        self.assertEqual(self.count(), 0)

    async def test_expired_after_authority_lock_wait_is_rejected(self):
        entered, release = threading.Event(), threading.Event()

        def authority(cursor, artifact, expiry):
            self.authority(cursor, artifact, expiry)
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test timeout")
            return True

        store = self.make_store(publication_authority=authority)
        task = asyncio.create_task(store.create(self.publication()))
        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
        self.time = NOW + 60
        release.set()
        with self.assertRaises(LinkError):
            await task
        self.assertEqual(self.count(), 0)

    async def test_artifact_deleted_retention_and_pending_deadline_reject_create(self):
        for field, value in (("bytes_state", "deleted"), ("retain_until", NOW + 59),
                             ("pending_deadline_at", NOW + 59)):
            # Each case gets a fresh real manifest; immutable tombstones and
            # original deadlines must never be rewritten merely for a fixture.
            self.artifact = self.register_artifact(deadline=value if field == "pending_deadline_at" else NOW + 3600)
            with self.transaction() as cursor:
                cursor.execute("INSERT INTO test_publication_authority VALUES (?,?,?,?,?,?,TRUE,?)",
                    (self.artifact.artifact_id, *self.expected, NOW + 3600))
                if field == "bytes_state":
                    cursor.execute("UPDATE qq_official_file_artifacts SET bytes_state='deleted',bytes_deleted_at=? WHERE artifact_id=?",
                                   (NOW, self.artifact.artifact_id))
                elif field == "retain_until":
                    cursor.execute("UPDATE qq_official_file_artifacts SET retain_until=? WHERE artifact_id=?",
                                   (value, self.artifact.artifact_id))
            with self.subTest(field=field), self.assertRaises(LinkError):
                await self.store.create(self.publication())
        self.assertEqual(self.count(), 0)

    async def test_gc_retention_update_wins_then_waiting_publication_rejects(self):
        locked, release = threading.Event(), threading.Event()

        def gc():
            with self.transaction() as cursor:
                cursor.execute("SELECT artifact_id FROM qq_official_file_artifacts FOR UPDATE")
                cursor.execute("UPDATE qq_official_file_artifacts SET retain_until=?", (NOW + 30,))
                locked.set()
                if not release.wait(3):
                    raise RuntimeError("test timeout")

        collector = asyncio.create_task(asyncio.to_thread(gc))
        self.assertTrue(await asyncio.to_thread(locked.wait, 2))
        creator = asyncio.create_task(self.store.create(self.publication()))
        await asyncio.sleep(0.03)
        self.assertFalse(creator.done())
        release.set()
        await collector
        with self.assertRaises(LinkError):
            await creator
        self.assertEqual(self.count(), 0)

    async def test_publication_share_lock_blocks_gc_then_reference_pins_bytes(self):
        locked, release, gc_entered = threading.Event(), threading.Event(), threading.Event()
        outer = self

        class PausingCursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def execute(self, query, args=()):
                result = self.cursor.execute(query, args)
                if "FROM qq_official_file_artifacts WHERE" in query and "FOR SHARE" in query:
                    locked.set()
                    if not release.wait(3):
                        raise RuntimeError("test timeout")
                return result

        @contextmanager
        def transaction():
            with outer.transaction() as cursor:
                yield PausingCursor(cursor)

        store = PgPublicationStore(transaction_factory=transaction, calls=self.calls,
            clock=lambda: self.time, publication_authority=self.authority)
        creator = asyncio.create_task(store.create(self.publication()))
        self.assertTrue(await asyncio.to_thread(locked.wait, 2))

        def gc():
            with self.transaction() as cursor:
                gc_entered.set()
                cursor.execute("SELECT artifact_id FROM qq_official_file_artifacts FOR UPDATE")
                references = cursor.execute("""SELECT COUNT(*) AS n FROM qq_official_file_publications
                    WHERE artifact_id=? AND state='active' AND expires_at>?""",
                    (self.artifact.artifact_id, self.time)).fetchone()["n"]
                if references:
                    return "pinned"
                cursor.execute("UPDATE qq_official_file_artifacts SET bytes_state='deleted',bytes_deleted_at=?", (NOW,))
                return "deleted"

        collector = asyncio.create_task(asyncio.to_thread(gc))
        self.assertTrue(await asyncio.to_thread(gc_entered.wait, 2))
        await asyncio.sleep(0.03)
        self.assertFalse(collector.done())
        release.set()
        await creator
        self.assertEqual(await collector, "pinned")
        with self.transaction() as cursor:
            self.assertEqual(cursor.execute("SELECT bytes_state FROM qq_official_file_artifacts").fetchone()[0], "available")

    async def test_cancellation_waits_for_transaction_then_recovers_original_vault(self):
        entered, release = threading.Event(), threading.Event()

        def authority(cursor, artifact, expiry):
            permitted = self.authority(cursor, artifact, expiry)
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test timeout")
            return permitted

        store = self.make_store(publication_authority=authority)
        with tempfile.TemporaryDirectory() as temporary:
            vault = FileCredentialVault(Path(temporary) / "vault")
            try:
                publisher = FileLinkPublisher(store=store, vault=vault, open_artifact=None,
                                               origin=ORIGIN, clock=lambda: self.time)
                task = asyncio.create_task(publisher.publish(self.artifact, request_key=self.request_key,
                                                             expires_at=NOW + 60))
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                original = next((Path(temporary) / "vault").iterdir()).read_bytes()
                task.cancel()
                await asyncio.sleep(0.01)
                self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(self.count(), 1)
                restored = await publisher.publish(self.artifact, request_key=self.request_key, expires_at=NOW + 60)
                self.assertEqual(next((Path(temporary) / "vault").iterdir()).read_bytes(), original)
                self.assertTrue(restored.url.startswith(ORIGIN))
            finally:
                vault.close()

    async def test_terminal_cas_and_no_early_expiry(self):
        row = await self.store.create(self.publication())
        with self.assertRaises(LinkError):
            await self.store.deactivate(row.publication_ref, "expired")
        await self.store.deactivate(row.publication_ref, "revoked")
        await self.store.deactivate(row.publication_ref, "expired")
        self.assertEqual((await self.store.get(row.publication_ref)).state, "revoked")
        await self.store.deactivate(row.publication_ref, "deleted")
        await self.store.deactivate(row.publication_ref, "revoked")
        self.assertEqual((await self.store.get(row.publication_ref)).state, "deleted")
        for state in ("active", "other", [], True):
            with self.assertRaises(LinkError):
                await self.store.deactivate(row.publication_ref, state)
        await self.store.deactivate("b" * 32, "deleted")

    async def test_expiry_persists_across_clock_rollback_and_restart(self):
        row = await self.store.create(self.publication())
        self.time = row.expires_at
        await self.store.deactivate(row.publication_ref, "expired")
        self.time = NOW
        other = self.make_store(publication_authority=self.authority)
        restored = await other.create(self.publication())
        self.assertEqual(restored.state, "expired")
        self.assertEqual(restored.credential_ref, row.credential_ref)
        self.assertFalse(await other.record_served(row.publication_ref, now=NOW,
            sha256=self.artifact.sha256, size=self.artifact.size))

    async def test_complete_count_hash_size_time_and_saturation(self):
        row = await self.store.create(self.publication())
        self.assertTrue(await self.store.record_served(row.publication_ref, now=NOW,
            sha256=self.artifact.sha256, size=self.artifact.size))
        self.assertFalse(await self.store.record_served(row.publication_ref, now=NOW,
            sha256="e" * 64, size=self.artifact.size))
        self.assertFalse(await self.store.record_served(row.publication_ref, now=NOW,
            sha256=self.artifact.sha256, size=self.artifact.size + 1))
        self.assertFalse(await self.store.record_served(row.publication_ref, now=NOW + 60,
            sha256=self.artifact.sha256, size=self.artifact.size))
        with self.transaction() as cursor:
            cursor.execute("UPDATE qq_official_file_publications SET served_complete_count=?", (MAX_BIGINT,))
        self.assertFalse(await self.store.record_served(row.publication_ref, now=NOW,
            sha256=self.artifact.sha256, size=self.artifact.size))
        self.assertEqual((await self.store.get(row.publication_ref)).served_complete_count, MAX_BIGINT)

    async def test_concurrent_complete_and_revoke_linearize(self):
        row = await self.store.create(self.publication())
        results = await asyncio.gather(*(self.store.record_served(row.publication_ref, now=NOW,
            sha256=self.artifact.sha256, size=self.artifact.size) for _ in range(12)),
            self.store.deactivate(row.publication_ref, "revoked"))
        final = await self.store.get(row.publication_ref)
        self.assertEqual(final.state, "revoked")
        self.assertEqual(final.served_complete_count, sum(result is True for result in results))
        self.assertFalse(await self.store.record_served(row.publication_ref, now=NOW,
            sha256=self.artifact.sha256, size=self.artifact.size))

    async def test_complete_waiting_for_row_rechecks_expiry_after_lock(self):
        row = await self.store.create(self.publication())
        locked, release = threading.Event(), threading.Event()

        def hold():
            with self.transaction() as cursor:
                cursor.execute("SELECT publication_ref FROM qq_official_file_publications FOR UPDATE")
                locked.set()
                if not release.wait(3):
                    raise RuntimeError("test timeout")

        holder = asyncio.create_task(asyncio.to_thread(hold))
        self.assertTrue(await asyncio.to_thread(locked.wait, 2))
        count = asyncio.create_task(self.store.record_served(row.publication_ref, now=NOW,
            sha256=self.artifact.sha256, size=self.artifact.size))
        await asyncio.sleep(0.03)
        self.time = row.expires_at
        release.set()
        await holder
        self.assertFalse(await count)

    async def test_database_constraints_immutability_and_fk(self):
        row = await self.store.create(self.publication())
        for query, args in (("UPDATE qq_official_file_publications SET expires_at=?", (NOW + 61,)),
                            ("UPDATE qq_official_file_publications SET token_digest=?", ("e" * 64,)),
                            ("DELETE FROM qq_official_file_artifacts", ()),
                            ("UPDATE qq_official_file_artifacts SET filename=?", ("changed.pdf",))):
            with self.subTest(query=query), self.assertRaises(DatabaseError), self.transaction() as cursor:
                cursor.execute(query, args)
        await self.store.deactivate(row.publication_ref, "deleted")
        with self.assertRaises(DatabaseError), self.transaction() as cursor:
            cursor.execute("UPDATE qq_official_file_publications SET state='active'")

    async def test_parameters_bad_clock_and_async_authority_fail_safe(self):
        for method in (self.store.by_request, self.store.by_digest, self.store.by_credential, self.store.get):
            for key in (None, True, [], "x' OR 1=1 --"):
                with self.assertRaises(LinkError):
                    await method(key)
        for now in (True, -1, 0.5, MAX_BIGINT + 1):
            with self.assertRaises(LinkError):
                await self.store.record_served("a" * 32, now=now, sha256=self.artifact.sha256, size=1)
        self.time = float("nan")
        with self.assertRaises(LinkError):
            await self.store.create(self.publication())

        async def async_authority(*args):
            return True

        with self.assertRaises(LinkError):
            self.make_store(publication_authority=async_authority)

    async def test_real_vault_concurrent_publish_restore_and_deleted_tombstone(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = FileCredentialVault(Path(temporary) / "vault")
            try:
                publisher = FileLinkPublisher(store=self.store, vault=vault, open_artifact=None,
                                               origin=ORIGIN, clock=lambda: self.time)
                items = await asyncio.gather(*(publisher.publish(self.artifact,
                    request_key=self.request_key, expires_at=NOW + 60) for _ in range(12)))
                self.assertEqual(len({item.url for item in items}), 1)
                self.assertEqual(len(list((Path(temporary) / "vault").iterdir())), 1)
                other = FileLinkPublisher(store=self.make_store(), vault=vault, open_artifact=None,
                                          origin=ORIGIN, clock=lambda: self.time)
                self.assertEqual(await other.restore(items[0].credential_ref), items[0])
                await other.delete(items[0].publication_ref)
                self.assertEqual((await self.store.get(items[0].publication_ref)).state, "deleted")
                self.assertEqual(len(list((Path(temporary) / "vault").iterdir())), 0)
            finally:
                vault.close()


if __name__ == "__main__":
    unittest.main()
