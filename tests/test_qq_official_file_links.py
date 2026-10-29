import asyncio
import hashlib
import json
import logging
import os
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

import aiohttp
from aiohttp import web
from src.qq_official.credentials import CredentialError

from src.plugins.ai_chat.adapters.qq_official.file_links import (
    MAX_TTL,
    ArtifactRef,
    FileCredentialVault,
    FileLinkPublisher,
    LinkError,
    VerifiedStream,
    create_download_app,
    create_download_runner,
)


class MemoryStore:
    """Fake atomic store contract only; intentionally no SQLite or durability claim."""

    def __init__(self):
        self.rows = {}
        self.fail_create = False
        self.commit_then_fail = False
        self.fail_record = False

    async def by_request(self, key):
        return next((r for r in self.rows.values() if r.request_key == key), None)

    async def by_credential(self, key):
        return next((r for r in self.rows.values() if r.credential_ref == key), None)

    async def by_digest(self, key):
        return next((r for r in self.rows.values() if r.token_digest == key), None)

    async def get(self, key):
        return self.rows.get(key)

    async def create(self, row):
        await asyncio.sleep(0)  # Force concurrent publisher interleaving.
        if self.fail_create:
            raise RuntimeError("store-secret-must-not-leak")
        existing = await self.by_request(row.request_key)
        if existing is not None:
            return existing
        self.rows[row.publication_ref] = row
        if self.commit_then_fail:
            raise RuntimeError("store-secret-must-not-leak")
        return row

    async def deactivate(self, key, state):
        row = self.rows[key]
        if state == "deleted" or row.state == "active":
            self.rows[key] = replace(row, state=state)

    async def record_served(self, key, *, now, sha256, size):
        if self.fail_record:
            raise RuntimeError("store-secret-must-not-leak")
        row = self.rows[key]
        if row.state != "active" or row.expires_at <= now or row.artifact.sha256 != sha256 or row.artifact.size != size:
            return False
        self.rows[key] = replace(row, served_complete_count=row.served_complete_count + 1)
        return True


class LinkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name) / "credentials"
        self.vault = FileCredentialVault(self.directory)
        self.store = MemoryStore()
        self.time = 1000
        self.content = b"%PDF-1.7\nFAKE_OFFLINE_ARTIFACT\n%%EOF\n"
        self.artifact = ArtifactRef("synthetic-artifact", hashlib.sha256(self.content).hexdigest(),
                                    len(self.content), "application/pdf", "测试文档.pdf")
        self.key = hashlib.sha256(b"synthetic-request").hexdigest()
        self.opens = 0
        self.closed = 0
        self.gate = None
        self.bad_content = False
        self.bad_metadata = False
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.origin = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        self.publisher = self.make_publisher()
        self.runner = create_download_runner(create_download_app(self.publisher))
        await self.runner.setup()
        self.site = web.SockSite(self.runner, self.sock)
        await self.site.start()
        self.client = aiohttp.ClientSession()

    async def asyncTearDown(self):
        if self.gate is not None:
            self.gate.set()
        await self.client.close()
        await self.runner.cleanup()
        self.vault.close()
        self.tmp.cleanup()

    def make_publisher(self, **kwargs):
        return FileLinkPublisher(store=self.store, vault=self.vault, open_artifact=self.reader,
            origin=kwargs.pop("origin", self.origin), clock=lambda: self.time,
            allow_loopback=kwargs.pop("allow_loopback", True), **kwargs)

    @asynccontextmanager
    async def reader(self, ref):
        self.opens += 1
        self.assertEqual(ref, self.artifact)
        data = self.content if not self.bad_content else b"x" * len(self.content)

        async def blocks():
            yield data[:8]
            if self.gate is not None:
                await self.gate.wait()
            yield data[8:]

        try:
            yield VerifiedStream("0" * 64 if self.bad_metadata else ref.sha256, ref.size, blocks())
        finally:
            self.closed += 1

    async def publish(self, **kwargs):
        return await self.publisher.publish(self.artifact, request_key=kwargs.pop("request_key", self.key),
                                            expires_at=kwargs.pop("expires_at", 1060), **kwargs)

    async def wait_idle(self):
        async with asyncio.timeout(2):
            while self.publisher._active:
                await asyncio.sleep(0.005)

    async def test_actual_get_hash_headers_and_complete_is_not_downloaded(self):
        item = await self.publish()
        async with self.client.get(item.url, headers={"Cookie": "admin=ignored"}) as response:
            self.assertEqual(response.status, 200)
            data = await response.read()
            self.assertEqual(data, self.content)
            self.assertEqual(response.headers["Content-Type"], "application/pdf")
            self.assertTrue(response.headers["Content-Disposition"].startswith("attachment;"))
            self.assertIn("filename*=UTF-8", response.headers["Content-Disposition"])
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("no-store", response.headers["Cache-Control"])
            self.assertNotIn("Set-Cookie", response.headers)
            self.assertNotIn("Access-Control-Allow-Origin", response.headers)
        await self.wait_idle()
        row = self.store.rows[item.publication_ref]
        self.assertEqual(row.served_complete_count, 1)
        self.assertNotIn("downloaded", asdict(row))
        self.assertEqual(self.opens, self.closed)

    async def test_head_and_range_never_read_body_or_record_complete(self):
        item = await self.publish()
        async with self.client.head(item.url) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.read(), b"")
            self.assertEqual(int(response.headers["Content-Length"]), len(self.content))
        for headers in ({"Range": "bytes=0-3"}, {"If-Range": "untrusted"}):
            async with self.client.get(item.url, headers=headers) as response:
                self.assertEqual(response.status, 416)
                self.assertEqual(await response.read(), b"")
        self.assertEqual(self.opens, 0)
        self.assertEqual(self.store.rows[item.publication_ref].served_complete_count, 0)

    async def test_256_bit_tokens_digest_only_and_private_permissions(self):
        item = await self.publish()
        token = item.url.rsplit("/", 1)[1]
        self.assertEqual(len(token), 43)
        row = self.store.rows[item.publication_ref]
        self.assertEqual(row.token_digest, hashlib.sha256(token.encode()).hexdigest())
        self.assertNotIn(token, json.dumps(asdict(row)))
        self.assertNotIn(item.url, json.dumps(asdict(row)))
        for value in (item, row, self.artifact, self.publisher, self.vault):
            self.assertNotIn(token, repr(value))
            self.assertNotIn("synthetic-artifact", repr(value))
            self.assertNotIn(str(self.directory), repr(value))
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        path = self.directory / (item.credential_ref + ".json")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(path.read_text())["token"], token)

    async def test_concurrent_same_key_one_publication_one_secret(self):
        items = await asyncio.gather(*(self.publish() for _ in range(20)))
        self.assertEqual(len({i.url for i in items}), 1)
        self.assertEqual(len(self.store.rows), 1)
        self.assertEqual(len(list(self.directory.iterdir())), 1)

    async def test_restart_recovers_exact_token_and_commitment(self):
        item = await self.publish()
        self.vault.close()
        self.vault = FileCredentialVault(self.directory)
        restarted = self.make_publisher()
        restored = await restarted.restore(item.credential_ref)
        replayed = await restarted.publish(self.artifact, request_key=self.key, expires_at=item.expires_at)
        self.assertEqual(restored, item)
        self.assertEqual(replayed, item)
        self.assertEqual(len(list(self.directory.iterdir())), 1)

    async def test_replay_cannot_change_expiry_artifact_or_origin(self):
        item = await self.publish()
        with self.assertRaisesRegex(LinkError, "conflict"):
            await self.publish(expires_at=1061)
        with self.assertRaisesRegex(LinkError, "conflict"):
            await self.publisher.publish(replace(self.artifact, filename="another.pdf"),
                                         request_key=self.key, expires_at=1060)
        other = self.make_publisher(origin="https://downloads.example.test", allow_loopback=False)
        with self.assertRaisesRegex(LinkError, "origin_changed"):
            await other.restore(item.credential_ref)
        self.assertEqual(len(self.store.rows), 1)

    async def test_expiry_exact_boundary_and_clock_rollback_cannot_revive(self):
        item = await self.publish()
        self.time = item.expires_at
        async with self.client.get(item.url) as response:
            self.assertEqual(response.status, 404)
        self.time = 1000
        async with self.client.get(item.url) as response:
            self.assertEqual(response.status, 404)
        self.assertFalse((self.directory / (item.credential_ref + ".json")).exists())
        with self.assertRaises(LinkError):
            await self.publish()
        self.assertEqual(len(self.store.rows), 1)

    async def test_expiry_validation(self):
        for expiry in (0, 1000, 4601, True, 1060.0, 2**63):
            with self.subTest(expiry=expiry), self.assertRaises(LinkError):
                await self.publish(expires_at=expiry)
        item = await self.publish(expires_at=1000 + MAX_TTL)
        self.assertEqual(item.expires_at, 4600)

    async def test_revoke_delete_and_cookie_never_grants_access(self):
        item = await self.publish()
        await self.publisher.revoke(item.publication_ref)
        await self.publisher.revoke(item.publication_ref)
        async with self.client.get(item.url, headers={"Cookie": "admin=valid", "Authorization": "Bearer fake"}) as response:
            self.assertEqual(response.status, 404)
        with self.assertRaises(LinkError):
            await self.publisher.restore(item.credential_ref)
        await self.publisher.delete(item.publication_ref)
        self.assertEqual(self.store.rows[item.publication_ref].state, "deleted")
        self.assertEqual(self.content[:4], b"%PDF")  # Shared source is untouched.

    async def test_revoke_during_stream_aborts_and_no_complete(self):
        self.gate = asyncio.Event()
        item = await self.publish()
        response = await self.client.get(item.url)
        self.assertEqual(await response.content.readexactly(8), self.content[:8])
        await self.publisher.revoke(item.publication_ref)
        self.gate.set()
        with self.assertRaises(aiohttp.ClientPayloadError):
            await response.read()
        await self.wait_idle()
        self.assertEqual(self.store.rows[item.publication_ref].served_complete_count, 0)
        self.assertEqual(self.closed, 1)

    async def test_expire_during_stream_aborts_and_no_complete(self):
        self.gate = asyncio.Event()
        item = await self.publish()
        response = await self.client.get(item.url)
        await response.content.readexactly(8)
        self.time = 1060
        self.gate.set()
        with self.assertRaises(aiohttp.ClientPayloadError):
            await response.read()
        await self.wait_idle()
        self.assertEqual(self.store.rows[item.publication_ref].state, "expired")
        self.assertEqual(self.store.rows[item.publication_ref].served_complete_count, 0)

    async def test_client_disconnect_does_not_record_complete(self):
        self.gate = asyncio.Event()
        item = await self.publish()
        response = await self.client.get(item.url)
        await response.content.readexactly(8)
        response.close()
        await asyncio.sleep(0.02)
        self.gate.set()
        await self.wait_idle()
        self.assertEqual(self.store.rows[item.publication_ref].served_complete_count, 0)

    async def test_source_metadata_rejected_before_bytes(self):
        self.bad_metadata = True
        item = await self.publish()
        async with self.client.get(item.url) as response:
            self.assertEqual(response.status, 404)
            self.assertEqual(await response.read(), b"")
        self.assertEqual(self.closed, 1)

    async def test_corrupt_source_never_records_complete(self):
        self.bad_content = True
        item = await self.publish()
        async with self.client.get(item.url) as response:
            try:
                await response.read()
            except aiohttp.ClientPayloadError:
                pass
        await self.wait_idle()
        self.assertEqual(self.store.rows[item.publication_ref].served_complete_count, 0)

    async def test_concurrency_limit_does_not_wait_or_leak_permits(self):
        self.publisher.max_concurrent = 1
        self.gate = asyncio.Event()
        item = await self.publish()
        first = await self.client.get(item.url)
        await first.content.readexactly(8)
        async with self.client.get(item.url) as second:
            self.assertEqual(second.status, 503)
        first.close()
        self.gate.set()
        await self.wait_idle()
        self.assertEqual(self.publisher._active, 0)

    async def test_token_tamper_and_unrelated_routes_are_empty(self):
        item = await self.publish()
        for url in (self.origin + "/files/" + secrets.token_urlsafe(32), self.origin + "/previews/abc/",
                    self.origin + "/admin", self.origin + "/files/short"):
            async with self.client.get(url) as response:
                self.assertEqual(response.status, 404)
                self.assertEqual(await response.read(), b"")
        async with self.client.post(item.url, data="untrusted") as response:
            self.assertEqual(response.status, 405)
            self.assertEqual(await response.read(), b"")

    async def test_vault_corruption_symlink_and_unsafe_mode_fail_closed(self):
        item = await self.publish()
        path = self.directory / (item.credential_ref + ".json")
        original = path.read_bytes()
        path.chmod(0o644)
        with self.assertRaises(LinkError):
            await self.publisher.restore(item.credential_ref)
        path.chmod(0o600)
        path.write_text("{}")
        with self.assertRaises(LinkError):
            await self.publisher.restore(item.credential_ref)
        path.unlink()
        other = Path(self.tmp.name) / "other.json"
        other.write_bytes(original)
        other.chmod(0o600)
        path.symlink_to(other)
        with self.assertRaises(LinkError):
            await self.publisher.restore(item.credential_ref)

    async def test_directory_permissions_and_acl_fail_closed(self):
        wrong = Path(self.tmp.name) / "unsafe"
        wrong.mkdir(mode=0o755)
        with self.assertRaises(LinkError):
            FileCredentialVault(wrong)
        with (patch("src.plugins.ai_chat.adapters.qq_official.file_links.verify_private_file", side_effect=CredentialError("unsafe")),
              self.assertRaises(LinkError)):
            await self.publish()
        self.assertEqual(list(self.directory.iterdir()), [])

    @unittest.skipUnless(sys.platform == "darwin", "macOS extended ACL test")
    async def test_real_extended_acl_rejects_0600_token_file(self):
        item = await self.publish()
        path = self.directory / (item.credential_ref + ".json")
        await asyncio.to_thread(subprocess.run, ["/bin/chmod", "+a", "everyone allow read", str(path)],
                                check=True, capture_output=True)
        try:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(LinkError):
                await self.publisher.restore(item.credential_ref)
        finally:
            await asyncio.to_thread(subprocess.run, ["/bin/chmod", "-N", str(path)], check=True, capture_output=True)

    async def test_stalled_reader_times_out_and_releases_capacity(self):
        self.gate = asyncio.Event()
        item = await self.publish(expires_at=1001)
        response = await self.client.get(item.url)
        await response.content.readexactly(8)
        with self.assertRaises(aiohttp.ClientPayloadError):
            await response.read()
        await self.wait_idle()
        self.assertEqual(self.closed, 1)
        self.assertEqual(self.store.rows[item.publication_ref].served_complete_count, 0)

    async def test_record_complete_race_rechecks_terminal_state(self):
        item = await self.publish()
        record = self.store.record_served

        async def race(key, **kwargs):
            await self.store.deactivate(key, "revoked")
            return await record(key, **kwargs)

        self.store.record_served = race
        async with self.client.get(item.url) as response:
            self.assertEqual(await response.read(), self.content)
        await self.wait_idle()
        self.assertEqual(self.store.rows[item.publication_ref].served_complete_count, 0)

    async def test_insert_failure_keeps_orphan_until_original_expiry_then_collects(self):
        self.store.fail_create = True
        with self.assertRaisesRegex(LinkError, "publication_operation_unavailable"):
            await self.publish()
        self.assertEqual(len(list(self.directory.iterdir())), 1)
        result = await self.publisher.collect_credentials()
        self.assertEqual(result["deleted"], 0)
        self.time = 1060
        result = await self.publisher.collect_credentials()
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_unknown_insert_commit_recovers_same_secret_never_resigns(self):
        self.store.commit_then_fail = True
        with self.assertRaises(LinkError):
            await self.publish()
        stored_secret = next(self.directory.iterdir()).read_bytes()
        self.store.commit_then_fail = False
        item = await self.publish()
        self.assertEqual(next(self.directory.iterdir()).read_bytes(), stored_secret)
        self.assertEqual(json.loads(stored_secret)["token"], item.url.rsplit("/", 1)[1])

    async def test_cleanup_bounded_pages_and_incomplete_file_grace(self):
        for number in range(3):
            await self.publish(request_key=hashlib.sha256(str(number).encode()).hexdigest())
        broken = self.directory / ("f" * 32 + ".json")
        broken.write_text("{")
        broken.chmod(0o600)
        os.utime(broken, (1000, 1000))
        self.time = 1060
        first = await self.publisher.collect_credentials(limit=2)
        self.assertEqual(first["scanned"], 2)
        self.assertIsNotNone(first["next_ref"])
        await self.publisher.collect_credentials(after_ref=first["next_ref"], limit=2)
        self.assertTrue(broken.exists())
        self.time = 4660
        await self.publisher.collect_credentials()
        self.assertFalse(broken.exists())
        self.assertEqual(len(list(self.directory.iterdir())), 0)

    async def test_origin_and_artifact_validation(self):
        for origin in ("http://example.test", "https://user@example.test", "https://example.test?", "https://example.test#",
                       "https://example.test/path", "https://127.0.0.1", "https://[::1]", "https://[fc00::1]", "https://localhost"):
            with self.subTest(origin=origin), self.assertRaises(LinkError):
                self.make_publisher(origin=origin, allow_loopback=False)
        for origin in ("http://localhost:1234", "http://0.0.0.0:1234", "https://127.0.0.1:1234"):
            with self.subTest(origin=origin), self.assertRaises(LinkError):
                self.make_publisher(origin=origin)
        for changes in ({"filename": "../secret"}, {"filename": "x\nheader"}, {"mime": "text/html;script"},
                        {"size": True}, {"sha256": "z" * 64}, {"artifact_id": "x\x00"}, {"artifact_id": "\ud800"}):
            with self.subTest(changes=changes), self.assertRaises(LinkError):
                replace(self.artifact, **changes)

    async def test_transport_logs_do_not_contain_url_or_injected_error(self):
        item = await self.publish()
        messages = []

        class Capture(logging.Handler):
            def emit(self, record):
                messages.append(record.getMessage())

        handler = Capture()
        logger = logging.getLogger()
        logger.addHandler(handler)
        previous = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            self.store.fail_record = True
            async with self.client.get(item.url) as response:
                await response.read()
            async with self.client.get(self.origin + "/files/bad") as response:
                await response.read()
            # Parser errors happen outside middleware; its dedicated logger also
            # must not propagate credential-bearing request lines/tracebacks.
            reader, writer = await asyncio.open_connection("127.0.0.1", self.sock.getsockname()[1])
            token = item.url.rsplit("/", 1)[1]
            writer.write(("GET /files/" + token + " HTTP/WRONG\r\nHost: localhost\r\n\r\n").encode())
            await writer.drain()
            await reader.read()
            writer.close()
            await writer.wait_closed()
            await self.wait_idle()
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)
        output = "\n".join(messages)
        self.assertNotIn(item.url.rsplit("/", 1)[1], output)
        self.assertNotIn("store-secret-must-not-leak", output)


if __name__ == "__main__":
    unittest.main()
