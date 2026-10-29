"""Explicit, independent attachment listener and live reverse-proxy evidence.

This host-only configuration/port cannot be constructed from chat/tool input.
The HTTPS probe establishes only that the configured proxy reaches this exact
listener. It does not prove QQ domain policy or a recipient's network access;
those observations remain exact, expiring operator-reviewed route evidence.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hmac
import secrets
import time

from aiohttp import web
import httpx2

from ...mcp_bridge.http_transport import HttpPolicy, PinnedHttpTransport
from ...platform_types import AccountKey, EndpointKey
from .file_links import FileLinkPublisher, _headers, _origin, create_download_app, create_download_runner

CONTRACT = 'controlled-download-v1'
PROBE_PATH = '/health/download'


class DownloadError(ValueError):
    """Fixed diagnostics only; no paths, bearer URLs or remote error text."""


def _integer(value, lower, upper):
    return type(value) is int and lower <= value <= upper


@dataclass(frozen=True, repr=False)
class LinkRouteEvidence:
    endpoint: EndpointKey
    endpoint_generation: int
    route_epoch: int
    capability_revision: int
    verified_at: int
    expires_at: int
    contract: str

    def __post_init__(self):
        if (type(self.endpoint) is not EndpointKey or self.endpoint.account.platform != 'qq-official'
                or any(not _integer(v, 1, 2**63-1) for v in
                       (self.endpoint_generation, self.route_epoch, self.capability_revision))
                or not _integer(self.verified_at, 0, 2**63-1)
                or not _integer(self.expires_at, self.verified_at + 1, 2**63-1)
                or type(self.contract) is not str or not 1 <= len(self.contract.encode()) <= 256
                or any(ord(c) < 32 or ord(c) == 127 for c in self.contract)):
            raise DownloadError('invalid_download_route_evidence')


@dataclass(frozen=True, repr=False)
class DownloadConfiguration:
    origin: str
    listen_host: str
    listen_port: int
    routes: tuple[LinkRouteEvidence, ...] = field(repr=False)

    def __post_init__(self):
        try:
            normalized = _origin(self.origin, False)
        except ValueError:
            raise DownloadError('invalid_download_configuration') from None
        if (normalized != self.origin or self.listen_host not in {'127.0.0.1', '::1'}
                or not _integer(self.listen_port, 1024, 65535) or type(self.routes) is not tuple
                or len(self.routes) > 128 or any(type(v) is not LinkRouteEvidence for v in self.routes)
                or len({v.endpoint.key for v in self.routes}) != len(self.routes)):
            raise DownloadError('invalid_download_configuration')


async def probe_https(url, nonce, *, transport_factory=None):
    """One bounded HTTPS GET: pinned public DNS, verified TLS, no redirects.

    The optional transport factory is a trusted test/host injection, never a
    field accepted from configuration. Production uses the existing pinned
    HTTP transport with trust_env=False; no cookie jar or URL logging client.
    """
    transport = transport_factory(url) if transport_factory else PinnedHttpTransport(url, HttpPolicy())
    response = None
    try:
        async with asyncio.timeout(5):
            response = await transport.handle_async_request(httpx2.Request('GET', url,
                headers={'X-Gaoji-Probe': nonce, 'Accept-Encoding': 'identity'},
                extensions={'timeout': {'connect': 3, 'read': 3, 'write': 3, 'pool': 1}}))
            if (response.status_code != 200
                    or response.headers.get('Content-Encoding', 'identity').lower() != 'identity'
                    or response.headers.get('Content-Type', '').split(';')[0] != 'text/plain'):
                raise DownloadError('download_probe_unavailable')
            body = bytearray()
            async for block in response.aiter_bytes():
                if len(body) + len(block) > 64:
                    raise DownloadError('download_probe_unavailable')
                body.extend(block)
            if len(body) != 64:
                raise DownloadError('download_probe_unavailable')
            return bytes(body)
    except asyncio.CancelledError:
        raise
    except Exception:
        raise DownloadError('download_probe_unavailable') from None
    finally:
        if response is not None:
            await response.aclose()
        await transport.aclose()


class DownloadHost:
    """App-lifetime listener; a current account epoch supplies the publisher.

    Starting a listener never publishes, renews, or authorizes an artifact.
    Every request captures one real publisher; its existing DB/source fences
    apply before headers and every chunk, including across account replacement.
    """
    def __init__(self, config: DownloadConfiguration, account: AccountKey, *,
                 probe=probe_https, monotonic=time.monotonic, clock=time.time):
        if (type(config) is not DownloadConfiguration or type(account) is not AccountKey
                or account.platform != 'qq-official'
                or any(row.endpoint.account != account for row in config.routes)):
            raise DownloadError('invalid_download_account')
        self.config, self.account = config, account
        self._probe, self._monotonic, self.clock = probe, monotonic, clock
        self._runner = self._site = self._task = self._service = None
        self._closed = False
        self._ready_until = 0.0
        self._challenge = None
        self._lock = asyncio.Lock()
        self._probe_lock = asyncio.Lock()
        self._routes = {value.endpoint.key: value for value in config.routes}

    def publisher(self):
        value = self._service
        if (self._closed or value is None or not value._local()
                or value.token.account_key != self.account.key
                or value.config.link_origin != self.config.origin):
            return None
        publisher = value.download_publisher
        return publisher if isinstance(publisher, FileLinkPublisher) else None

    def attach(self, service):
        if (self._closed or service.token.account_key != self.account.key or not service._local()
                or service.config.link_origin != self.config.origin
                or not isinstance(service.download_publisher, FileLinkPublisher)):
            raise DownloadError('download_epoch_unavailable')
        if self._service is not None and self._service is not service and self._service._local():
            raise DownloadError('download_epoch_conflict')
        self._service = service

    def status(self):
        return dict(configured=True,listening=not self._closed and self._runner is not None,
            proxy_reachable=self.reachable(),evidence_configured=bool(self._routes))

    def reachable(self):
        return (not self._closed and self._runner is not None
                and self._monotonic() < self._ready_until)

    def route_allowed(self, cursor, endpoint, actor, route, *, retained=False):
        if (route != 'link' or endpoint.account != self.account or not actor.matches_endpoint(endpoint)
                or actor.identity_kind == 'bot'):
            return False
        proof = self._routes.get(endpoint.key)
        if proof is None or proof.contract != CONTRACT:
            return False
        row = cursor.execute('SELECT generation,route_epoch,capability_revision FROM qq_official_endpoints '
            'WHERE endpoint_key=? AND account_key=? FOR SHARE', (endpoint.key, self.account.key)).fetchone()
        if (row is None or (proof.endpoint_generation, proof.route_epoch, proof.capability_revision) !=
                (row['generation'], row['route_epoch'], row['capability_revision'])):
            return False
        # An already-issued publication keeps its original absolute expiry.
        # Current source/actor/retention checks live in FileServices, not here.
        return retained or (proof.verified_at <= int(self.clock()) < proof.expires_at and self.reachable())

    def retained(self, cursor, artifact):
        from ...platform_types import ActorKey
        return self.route_allowed(cursor, EndpointKey.parse(artifact['endpoint_key']),
            ActorKey.parse(artifact['requester_actor_key']), 'link', retained=True)

    async def _answer_probe(self, request):
        item = self._challenge
        nonce = request.headers.get('X-Gaoji-Probe', '')
        if (item is None or len(nonce) != 64 or self._monotonic() >= item[2]
                or not hmac.compare_digest(nonce, item[0])):
            return web.Response(status=404, headers=_headers())
        # The unpredictable proof is distinct from the nonce sent to the proxy.
        return web.Response(body=item[1], content_type='text/plain', headers=_headers())

    async def check(self):
        async with self._probe_lock:
            self._ready_until = 0.0
            if self._closed or self._runner is None:
                return False
            nonce, proof = secrets.token_hex(32), secrets.token_hex(32).encode('ascii')
            self._challenge = (nonce, proof, self._monotonic() + 5)
            try:
                async with asyncio.timeout(5):
                    actual = await self._probe(self.config.origin + PROBE_PATH, nonce)
                if (type(actual) is not bytes or not hmac.compare_digest(actual, proof)
                        or self._closed or self._monotonic() >= self._challenge[2]):
                    return False
                self._ready_until = self._monotonic() + 60
                return True
            except asyncio.CancelledError:
                raise
            except Exception:
                return False
            finally:
                self._challenge = None

    async def _run(self):
        while not self._closed:
            await self.check()
            await asyncio.sleep(30)

    async def start(self):
        async with self._lock:
            if self._closed:
                raise DownloadError('download_host_closed')
            if self._runner is not None:
                return
            app = create_download_app(publisher_getter=self.publisher)
            app.router.add_get(PROBE_PATH, self._answer_probe, allow_head=False)
            runner = create_download_runner(app)
            try:
                await runner.setup()
                site = web.TCPSite(runner, self.config.listen_host, self.config.listen_port)
                await site.start()
            except BaseException as error:
                await runner.cleanup()
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise DownloadError('download_listener_unavailable') from None
            self._runner, self._site = runner, site
            self._task = asyncio.create_task(self._run())

    async def close(self):
        async with self._lock:
            self._closed = True
            self._ready_until = 0.0
            self._service = None
            if self._task is not None:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
                self._task = None
            if self._runner is not None:
                await self._runner.cleanup()
                self._runner = self._site = None
