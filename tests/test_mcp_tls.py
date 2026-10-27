"""Actual loopback TLS: numeric pinning must retain hostname verification/SNI."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import socket
import ssl
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
import httpx2
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
import nonebot
import uvicorn

nonebot.init()

from src.plugins.ai_chat.mcp_bridge.client import McpCallError, McpWireClient
from src.plugins.ai_chat.mcp_bridge.http_transport import HttpPolicy, HttpSessions


class McpTlsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory(prefix="gaoji-mcp-tls-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mcp.tls.test")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("mcp.tls.test")]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(key, hashes.SHA256()))
        cert_file, key_file = root / "cert.pem", root / "key.pem"
        cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        key_file.chmod(0o600)
        self.trusted = ssl.create_default_context(cafile=str(cert_file))
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.setblocking(False)
        self.addCleanup(listener.close)
        self.port = listener.getsockname()[1]
        self.url = f"https://mcp.tls.test:{self.port}/mcp"
        self.requests, self.calls = [], []
        mcp = MCPServer("local-tls-fixture")

        @mcp.tool(structured_output=True)
        def echo(value: str) -> dict[str, str]:
            self.calls.append(value)
            return {"value": value}

        app = mcp.streamable_http_app(streamable_http_path="/mcp", json_response=True,
            stateless_http=True, transport_security=TransportSecuritySettings(allowed_hosts=[f"mcp.tls.test:{self.port}"]))

        async def record(scope, receive, send):
            if scope["type"] == "http":
                self.requests.append(dict(scope["headers"]))
            await app(scope, receive, send)

        server = uvicorn.Server(uvicorn.Config(record, log_level="critical", access_log=False,
            ssl_keyfile=str(key_file), ssl_certfile=str(cert_file)))
        running = asyncio.create_task(server.serve(sockets=[listener]))

        async def stop():
            server.should_exit = True
            await asyncio.wait_for(running, 5)
        self.addAsyncCleanup(stop)
        async with asyncio.timeout(5):
            while not server.started:
                if running.done():
                    await running
                    self.fail("Local TLS fixture did not start")
                await asyncio.sleep(0.01)

    def wire(self, *, url=None, trust=True):
        url = url or self.url
        resolver = AsyncMock(return_value=("127.0.0.1",))
        self.resolver = resolver
        sessions = HttpSessions(HttpPolicy(frozenset({url}), {"GAOJI_MCP_SECRET_TLS": "local-test-only"}),
            resolver=resolver, transport_factory=lambda: httpx2.AsyncHTTPTransport(
                verify=self.trusted if trust else True, trust_env=False, retries=0))
        return McpWireClient(sessions, timeout_seconds=5)

    async def test_real_tls_pinning_preserves_verified_hostname_and_host_without_proxy(self):
        wire = self.wire()
        config = {"transport": "http", "url": self.url, "credential_ref": "GAOJI_MCP_SECRET_TLS"}
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:1", "ALL_PROXY": "http://127.0.0.1:1"}):
            definitions = await wire.discover(config)
            guard = AsyncMock()
            result = await wire.call(config, definitions[0], {"value": "local-only"}, before_call=guard)
        self.assertTrue(result["ok"])
        self.assertEqual(self.calls, ["local-only"])
        guard.assert_awaited_once()
        self.resolver.assert_awaited_with("mcp.tls.test", self.port)
        self.assertTrue(self.requests)
        self.assertTrue(all(item[b"host"] == f"mcp.tls.test:{self.port}".encode() for item in self.requests))
        self.assertTrue(all(item[b"authorization"] == b"Bearer local-test-only" for item in self.requests))

    async def test_untrusted_certificate_and_wrong_hostname_fail_before_http_or_execution(self):
        wrong = self.url.replace("mcp.tls.test", "other.tls.test")
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()

        def expected_handshake_reset(loop, context):
            # These two deliberately rejected handshakes reset the server side.
            # Forward everything else so unrelated transport failures stay visible.
            if (context.get("message") == "Error on transport creation for incoming connection"
                    and isinstance(context.get("exception"), ConnectionResetError)):
                return
            (previous or (lambda loop, context: loop.default_exception_handler(context)))(loop, context)

        loop.set_exception_handler(expected_handshake_reset)
        try:
            for wire, url in ((self.wire(trust=False), self.url), (self.wire(url=wrong), wrong)):
                with self.subTest(url=url), self.assertRaises(McpCallError) as error:
                    await wire.discover({"transport": "http", "url": url})
                self.assertFalse(error.exception.may_have_run)
            await asyncio.sleep(0.01)
        finally:
            loop.set_exception_handler(previous)
        self.assertFalse(self.requests)
        self.assertFalse(self.calls)


if __name__ == "__main__":
    unittest.main()
