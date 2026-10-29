"""Real source/execution + artifact + PG publication + loopback integration.

The ordinary owned PostgreSQL fixture applies the formal 0065 schema.
No external transport, deployment or formal migration is modified.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import os
import socket
import unittest
import zipfile
from contextlib import asynccontextmanager

import aiohttp
from aiohttp import web
from src.plugins.ai_chat.adapters.qq_official.file_artifacts import current_source
from src.bot_storage.async_calls import DatabaseCalls
from src.plugins.ai_chat.agent.control import LeaseLost
from tests.qq_official_file_fixture import FileFixture

from src.plugins.ai_chat.adapters.qq_official.file_links import (
    ArtifactRef,
    FileCredentialVault,
    FileLinkPublisher,
    VerifiedStream,
    create_download_app,
    create_download_runner,
)
from src.plugins.ai_chat.adapters.qq_official.file_publications import PgPublicationStore

PDF = b"%PDF-1.4\n% TEST offline integration\n%%EOF\n"
KEY = hashlib.sha256(b"TEST_ARTIFACT_LINK_SOURCE").hexdigest()


class IntegrationTests(FileFixture):
    def setUp(self):
        super().setUp()
        self.root = self.blobs.root.parent

    def register(self, content=PDF, filename="test.pdf", key=KEY):
        return self.files.register(self.source, content=content, filename=filename, logical_request_key=key)

    def publication_authority(self, cursor, artifact, expiry):
        source = current_source(cursor, self.source, self.now)
        row = cursor.execute("SELECT * FROM qq_official_file_artifacts WHERE artifact_id=?",
                             (artifact.artifact_id,)).fetchone()
        return (row is not None and row["source_execution_id"] == self.source.source_execution_id
            and row["account_key"] == source["account_key"]
            and row["endpoint_key"] == source["endpoint_key"]
            and row["requester_actor_key"] == source["requester_actor_key"]
            and expiry <= row["retain_until"] and expiry <= row["pending_deadline_at"])

    async def with_server(self, scenario):
        calls = DatabaseCalls()
        vault = FileCredentialVault(self.root / "credentials")
        self.host_reads = 0
        self.hold_stream = None

        @asynccontextmanager
        async def reader(ref):
            self.host_reads += 1
            # Real bounded host read validates the whole immutable object before
            # any response bytes are written; the stream holds its own byte copy.
            body = await calls(self.files.read_verified, ref.artifact_id,
                               sha256=ref.sha256, byte_size=ref.size)

            async def chunks():
                yield body[:8]
                if self.hold_stream is not None:
                    await self.hold_stream.wait()
                yield body[8:]

            yield VerifiedStream(ref.sha256, ref.size, chunks())

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        store = PgPublicationStore(transaction_factory=self.tx, calls=calls,
            clock=lambda: self.now, publication_authority=self.publication_authority, allow_loopback=True)
        publisher = FileLinkPublisher(store=store, vault=vault, open_artifact=reader,
            origin=origin, clock=lambda: self.now, allow_loopback=True)
        runner = create_download_runner(create_download_app(publisher))
        await runner.setup()
        await web.SockSite(runner, sock).start()
        try:
            async with aiohttp.ClientSession() as client:
                await scenario(calls, publisher, client)
        finally:
            if self.hold_stream is not None:
                self.hold_stream.set()
            await runner.cleanup()
            await calls.close()
            vault.close()

    @staticmethod
    def ref(item):
        return ArtifactRef(item.artifact_id, item.sha256, item.byte_size, item.mime, item.filename)

    async def publish(self, publisher, item, *, expiry=None):
        return await publisher.publish(self.ref(item),
            request_key=hashlib.sha256(("publication:" + item.artifact_id).encode()).hexdigest(),
            expires_at=self.now + 60 if expiry is None else expiry)

    def test_link_real_pdf_and_zip_sources_get_head_hash_and_queue_semantics(self):
        zip_bytes = io.BytesIO()
        with zipfile.ZipFile(zip_bytes, "w") as archive:
            archive.writestr("report.txt", "TEST synthetic data")

        async def scenario(calls, publisher, client):
            for number, (body, filename) in enumerate(((PDF, "test.pdf"), (zip_bytes.getvalue(), "test.zip"))):
                item = await calls(self.register, body, filename, hashlib.sha256(str(number).encode()).hexdigest())
                link = await self.publish(publisher, item)
                before_reads = self.host_reads
                async with client.head(link.url) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(int(response.headers["Content-Length"]), len(body))
                    self.assertEqual(await response.read(), b"")
                self.assertEqual(self.host_reads, before_reads)
                async with client.get(link.url) as response:
                    self.assertEqual(response.status, 200)
                    received = await response.read()
                    self.assertEqual(hashlib.sha256(received).hexdigest(), item.sha256)
                    self.assertEqual(response.headers["Content-Type"], item.mime)
                async with asyncio.timeout(2):
                    while (await publisher.store.get(link.publication_ref)).served_complete_count != 1:
                        await asyncio.sleep(0.005)
                current = await calls(self.files.get, item.artifact_id)
                self.assertFalse(current.message_accepted)
                self.assertEqual(current.delivery_state, "stored")
                self.assertEqual(self.query("SELECT COUNT(*) AS n FROM qq_official_file_deliveries")[0]["n"], number + 1)
                self.assertEqual(self.query("SELECT COUNT(*) AS n FROM delivery_plans")[0]["n"], 0)
                self.assertEqual(self.query("SELECT COUNT(*) AS n FROM delivery_receipts")[0]["n"], 0)

        asyncio.run(self.with_server(scenario))

    def test_link_publication_rechecks_real_source_authority(self):
        item = self.register()
        self.admitted = False

        async def scenario(calls, publisher, client):
            with self.assertRaises(LeaseLost):
                await self.publish(publisher, item)
            self.assertEqual(self.query("SELECT COUNT(*) AS n FROM qq_official_file_publications")[0]["n"], 0)
            self.assertEqual(self.host_reads, 0)
            self.assertTrue(self.blobs.path(item.sha256).exists())

        asyncio.run(self.with_server(scenario))

    def test_link_revoked_during_real_verified_stream_does_not_count_complete(self):
        item = self.register()

        async def scenario(calls, publisher, client):
            link = await self.publish(publisher, item)
            self.hold_stream = asyncio.Event()
            response = await client.get(link.url)
            await response.content.readexactly(8)
            await publisher.revoke(link.publication_ref)
            self.hold_stream.set()
            with self.assertRaises(aiohttp.ClientPayloadError):
                await response.read()
            self.assertEqual((await publisher.store.get(link.publication_ref)).served_complete_count, 0)
            self.assertEqual(self.files.get(item.artifact_id).bytes_state, "available")

        asyncio.run(self.with_server(scenario))

    def test_link_expired_then_artifact_gc_keeps_audit_and_no_live_bytes(self):
        item = self.register()

        async def scenario(calls, publisher, client):
            link = await self.publish(publisher, item)
            self.now = link.expires_at
            async with client.get(link.url) as response:
                self.assertEqual(response.status, 404)
            self.assertEqual((await publisher.store.get(link.publication_ref)).state, "expired")
            self.now = item.pending_deadline_at
            self.assertEqual(await calls(self.files.collect_expired), 1)
            self.assertFalse(self.blobs.path(item.sha256).exists())
            self.assertEqual(self.files.get(item.artifact_id).bytes_state, "deleted")
            self.assertIsNotNone(await publisher.store.get(link.publication_ref))

        asyncio.run(self.with_server(scenario))

    def test_review_transient_unlink_failure_must_be_retried_without_orphan_grace(self):
        item = self.register()
        self.now = item.pending_deadline_at
        remove = self.blobs.remove
        failures = [True]

        def flaky(sha):
            if failures.pop() if failures else False:
                raise OSError("TEST transient unlink failure")
            return remove(sha)

        self.blobs.remove = flaky
        self.assertEqual(self.files.collect_expired(), 1)
        self.assertEqual(self.files.get(item.artifact_id).bytes_state, "deleted")
        self.assertTrue(self.blobs.path(item.sha256).exists())
        pending = self.query("SELECT cleanup_pending,cleanup_retry_at FROM qq_official_file_artifacts")[0]
        self.assertTrue(pending["cleanup_pending"])
        self.assertEqual(pending["cleanup_retry_at"], self.now + 30)
        self.assertEqual(self.files.collect_expired(), 0)
        self.assertTrue(self.blobs.path(item.sha256).exists(), "physical retry must respect its short backoff")
        self.now += 30
        self.files.collect_expired()
        self.assertFalse(self.blobs.path(item.sha256).exists(), "committed tombstone must retry physical cleanup")
        self.assertFalse(self.query("SELECT cleanup_pending FROM qq_official_file_artifacts")[0]["cleanup_pending"])

    def test_review_referenced_orphan_prefix_must_not_starve_later_orphan(self):
        item = self.register()
        # Put the live reference lexically first to exercise real inventory
        # pagination, rather than relying on any filesystem enumeration order.
        orphan_bytes = next(PDF + b"TEST orphan" + str(index).encode() for index in range(10000)
            if hashlib.sha256(PDF + b"TEST orphan" + str(index).encode()).hexdigest() > item.sha256)
        orphan = self.blobs.put(orphan_bytes)
        for sha in (item.sha256, orphan.sha256):
            os.utime(self.blobs.path(sha), (self.now - 86401, self.now - 86401))
        removed, after = self.files.collect_orphans(limit=1)
        self.assertEqual((removed, after), (0, item.sha256))
        removed, after = self.files.collect_orphans(after_sha=after, limit=1)
        self.assertEqual((removed, after), (1, None))
        self.assertTrue(self.blobs.path(item.sha256).exists())
        self.assertFalse(self.blobs.path(orphan.sha256).exists(), "live prefix must not starve an unreferenced candidate")


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(IntegrationTests(name) for name in dir(IntegrationTests)
        if name.startswith(("test_link_", "test_review_")))


if __name__ == "__main__":
    unittest.main()
