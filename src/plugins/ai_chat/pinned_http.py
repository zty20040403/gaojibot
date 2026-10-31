"""Exact-endpoint HTTP transport with DNS pinning and bounded wire traffic."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import ipaddress
import socket

import httpx2

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

    def allows_private(self, url: httpx2.URL) -> bool:
        return str(url) in {str(httpx2.URL(value)) for value in self.private_endpoints}



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
        self.endpoint = httpx2.URL(endpoint)
        if self.endpoint.scheme not in {"http", "https"} or not self.endpoint.host:
            raise McpTransportDenied("Pinned HTTP endpoint must be an absolute http(s) URL")
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
