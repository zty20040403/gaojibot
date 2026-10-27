"""Exact-endpoint HTTP transport with DNS pinning and bounded wire traffic."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import ipaddress
import socket

import httpx2
from mcp.client.streamable_http import streamable_http_client

from .contracts import server_config

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_SESSION_BYTES = 8 * 1024 * 1024
MAX_REQUEST_BYTES = 256 * 1024


class McpTransportDenied(ValueError):
    pass


async def resolve_addresses(host: str, port: int) -> tuple[str, ...]:
    try:
        return (str(ipaddress.ip_address(host)),)
    except ValueError:
        pass
    async with asyncio.timeout(5):
        rows = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(sorted({row[4][0] for row in rows}))


@dataclass(frozen=True)
class HttpPolicy:
    # Operator configuration, never a flag supplied by the remote MCP server or LLM.
    private_endpoints: frozenset[str] = frozenset()
    secrets: Mapping[str, str] = field(default_factory=dict, repr=False)

    def allows_private(self, url: httpx2.URL) -> bool:
        return str(url) in {str(httpx2.URL(value)) for value in self.private_endpoints}

    def credential(self, config: dict) -> str:
        reference = config.get("credential_ref", "")
        if not reference:
            return ""
        value = self.secrets.get(reference, "")
        if not isinstance(value, str) or not 1 <= len(value) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in value):
            raise McpTransportDenied("MCP credential reference is unavailable")
        url = httpx2.URL(config["url"])
        if url.scheme != "https" and not self.allows_private(url):
            raise McpTransportDenied("MCP credentials require HTTPS or an explicitly trusted private endpoint")
        return value


class _BoundedStream(httpx2.AsyncByteStream):
    def __init__(self, stream: httpx2.AsyncByteStream, transport: PinnedHttpTransport):
        self.stream, self.transport = stream, transport

    async def __aiter__(self):
        received = 0
        try:
            async for chunk in self.stream:
                received += len(chunk)
                self.transport.received += len(chunk)
                if received > MAX_RESPONSE_BYTES or self.transport.received > MAX_SESSION_BYTES:
                    raise McpTransportDenied("MCP response exceeds the byte limit")
                yield chunk
        finally:
            await self.stream.aclose()

    async def aclose(self):
        await self.stream.aclose()


class PinnedHttpTransport(httpx2.AsyncBaseTransport):
    def __init__(self, endpoint: str, policy: HttpPolicy, *,
                 resolver: Callable[[str, int], Awaitable[tuple[str, ...]]] = resolve_addresses,
                 transport: httpx2.AsyncBaseTransport | None = None):
        self.endpoint = httpx2.URL(server_config({"transport": "http", "url": endpoint})["url"])
        self.policy, self.resolver = policy, resolver
        self.transport = transport or httpx2.AsyncHTTPTransport(
            trust_env=False, retries=0, limits=httpx2.Limits(max_connections=4, max_keepalive_connections=0))
        self.received = 0
        self._address: str | None = None
        self._resolve_lock = asyncio.Lock()

    async def address(self) -> str:
        async with self._resolve_lock:
            if self._address is None:
                addresses = await self.resolver(self.endpoint.host, self.endpoint.port or (443 if self.endpoint.scheme == "https" else 80))
                if not addresses or len(addresses) > 32:
                    raise McpTransportDenied("MCP endpoint has no bounded address set")
                for value in addresses:
                    if "%" in value:
                        raise McpTransportDenied("Scoped IP addresses are not supported")
                    address = ipaddress.ip_address(value)
                    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
                        address = address.ipv4_mapped
                    if address.is_unspecified or address.is_multicast or address.is_link_local or address.is_reserved:
                        raise McpTransportDenied("MCP endpoint resolves to a prohibited address")
                    if not address.is_global and not self.policy.allows_private(self.endpoint):
                        raise McpTransportDenied("Private MCP endpoints require an exact operator allowlist entry")
                # Only the validated numeric address reaches the connector; no second DNS lookup.
                self._address = str(ipaddress.ip_address(addresses[0]))
            return self._address

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if request.url != self.endpoint or request.method not in {"POST", "GET", "DELETE"}:
            raise McpTransportDenied("MCP request must use the registered endpoint")
        body = bytearray()
        async for chunk in request.stream:
            if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                raise McpTransportDenied("MCP request exceeds the byte limit")
            body.extend(chunk)
        headers = httpx2.Headers(request.headers)
        headers["Host"] = self.endpoint.netloc.decode("ascii")
        headers["Accept-Encoding"] = "identity"
        pinned = httpx2.Request(request.method, self.endpoint.copy_with(host=await self.address()),
            headers=headers, content=bytes(body),
            extensions={**request.extensions, "sni_hostname": self.endpoint.host})
        response = await self.transport.handle_async_request(pinned)
        try:
            # The SDK follows same-origin redirects itself, so reject them at the wire boundary.
            if 300 <= response.status_code < 400:
                raise McpTransportDenied("MCP redirects are disabled; register the final endpoint")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise McpTransportDenied("Compressed MCP responses are disabled")
            size = response.headers.get("Content-Length")
            if size is not None and (not size.isascii() or not size.isdecimal() or len(size) > 10 or int(size) > MAX_RESPONSE_BYTES):
                raise McpTransportDenied("MCP response exceeds the byte limit")
        except BaseException:
            await response.aclose()
            raise
        return httpx2.Response(response.status_code, headers=response.headers,
            stream=_BoundedStream(response.stream, self), extensions=response.extensions)

    async def aclose(self):
        await self.transport.aclose()


class HttpSessions:
    def __init__(self, policy: HttpPolicy, *, resolver=resolve_addresses, transport_factory=None):
        self.policy, self.resolver, self.transport_factory = policy, resolver, transport_factory

    def credential(self, config: dict) -> str:
        return self.policy.credential(config)

    @asynccontextmanager
    async def connect(self, config: dict):
        config = server_config(config)
        if config["transport"] != "http":
            raise McpTransportDenied("This session factory only supports HTTP")
        secret = self.credential(config)
        transport = PinnedHttpTransport(config["url"], self.policy, resolver=self.resolver,
            transport=self.transport_factory() if self.transport_factory else None)
        async with httpx2.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                timeout=httpx2.Timeout(45, connect=5, pool=2),
                headers={"Authorization": "Bearer " + secret} if secret else {}) as http:
            async with streamable_http_client(config["url"], http_client=http, max_sse_event_size=256 * 1024) as streams:
                yield streams
