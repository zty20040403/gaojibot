"""Independent attachment publisher; no deployment, SQL or QQ transport.

Only trusted host code may construct artifact references and provide the reader.
The reader contract is a fully verified, immutable source before streaming starts.
No API accepts a model-provided path or arbitrary URL. A transport-complete event
does not establish that a person downloaded or read the object.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import stat
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlsplit

from aiohttp import web
from src.qq_official.credentials import (
    CredentialError,
    _verify_posix_acl,
    verify_private_file,
)

MAX_TTL = 3600
MAX_BYTES = 200_000_000  # Local conservative hard bound, not an account entitlement.
MAX_CHUNK = 1024 * 1024
_HEX = re.compile(r"[a-f0-9]{64}\Z")
_REF = re.compile(r"[a-f0-9]{32}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")


class LinkError(ValueError):
    """Fixed diagnostic only: no paths, IDs, credentials or request text."""


class PublicationNotCreated(LinkError):
    """Internal PG authority rejected before INSERT; no publication was created."""
    def __init__(self):
        super().__init__('publication_not_created_recipient_wait')


def _safe_operation(method):
    @wraps(method)
    async def wrapped(*args, **kwargs):
        try:
            return await method(*args, **kwargs)
        except LinkError:
            raise
        except Exception:  # noqa: BLE001 - sanitize injected store failures at the public boundary.
            raise LinkError("publication_operation_unavailable") from None
    return wrapped


def _valid_text(value: object, limit: int) -> bool:
    try:
        return (isinstance(value, str) and 0 < len(value.encode("utf-8")) <= limit
                and not any(ord(c) < 32 or ord(c) == 127 for c in value))
    except UnicodeError:
        return False


def _ref(value: object) -> bool:
    return isinstance(value, str) and _REF.fullmatch(value) is not None


def _digest(value: object) -> bool:
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def _timestamp(value: object) -> bool:
    return type(value) is int and 0 <= value <= 2**63 - 1


@dataclass(frozen=True, repr=False)
class ArtifactRef:
    """Trusted immutable artifact, already authorized by the calling host."""

    artifact_id: str
    sha256: str
    size: int
    mime: str
    filename: str

    def __post_init__(self):
        if (not _valid_text(self.artifact_id, 256) or not _digest(self.sha256)
                or type(self.size) is not int or not 1 <= self.size <= MAX_BYTES
                or not isinstance(self.mime, str)
                or not re.fullmatch(r"[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,63}", self.mime)
                or not _valid_text(self.filename, 180)
                or any(c in self.filename for c in '/\\')
                or self.filename in {'.', '..'}):
            raise LinkError("invalid_artifact_reference")

    def __repr__(self):
        return "ArtifactRef(<protected>)"


@dataclass(frozen=True, repr=False)
class Publication:
    """Persistable metadata; contains no raw token or signed URL.

    A real store must atomically preserve request_key uniqueness and all immutable
    fields. It must serialize deactivate/record_served, and never reactivate a row.
    This row is internal authority; chat/tool callers cannot insert it directly.
    """

    publication_ref: str
    credential_ref: str
    request_key: str
    artifact: ArtifactRef
    token_digest: str
    wire_commitment: str
    origin: str
    created_at: int
    expires_at: int
    state: str = "active"
    served_complete_count: int = 0

    def __post_init__(self):
        if (not _ref(self.publication_ref) or not _ref(self.credential_ref)
                or not _digest(self.request_key) or not _digest(self.token_digest)
                or not _digest(self.wire_commitment) or not isinstance(self.artifact, ArtifactRef)
                or not _timestamp(self.created_at) or not _timestamp(self.expires_at)
                or not self.created_at < self.expires_at <= self.created_at + MAX_TTL
                or self.state not in {"active", "revoked", "expired", "deleted"}
                or type(self.served_complete_count) is not int or self.served_complete_count < 0):
            raise LinkError("invalid_publication")

    def __repr__(self):
        return f"Publication(state={self.state!r}, <protected>)"


@dataclass(frozen=True, repr=False)
class PublishedFile:
    publication_ref: str
    credential_ref: str
    expires_at: int
    wire_commitment: str
    url: str = field(repr=False)

    def __repr__(self):
        return "PublishedFile(<protected>)"


@dataclass(repr=False)
class VerifiedStream:
    """Reader has verified full bytes/hash/size before returning this stream.

    Its immutable backing source must stay pinned until context exit. Iteration
    must be bounded and cancellation-safe. The handler rehashes defensively too.
    """

    sha256: str
    size: int
    chunks: AsyncIterator[bytes]


class PublicationStore(Protocol):
    async def by_request(self, request_key: str) -> Publication | None: ...
    async def by_credential(self, credential_ref: str) -> Publication | None: ...
    async def by_digest(self, token_digest: str) -> Publication | None: ...
    async def get(self, publication_ref: str) -> Publication | None: ...
    async def create(self, publication: Publication) -> Publication:
        """Atomic insert-or-return-existing by request_key; never replace rows."""
        ...
    async def deactivate(self, publication_ref: str, state: str) -> None:
        """Terminal CAS; deleted supersedes other terminal states, never active."""
        ...
    async def record_served(self, publication_ref: str, *, now: int, sha256: str, size: int) -> bool:
        """Atomic increment only if still active/unexpired and artifact matches."""
        ...


class FileCredentialVault:
    """0700 directory and fsynced 0600 files; raw tokens never enter the DB.

    Directory descriptor is pinned, all file operations use dir_fd and NOFOLLOW.
    Existing unsafe directories/files fail closed; we do not chmod them into trust.
    The existing credentials ACL verifier is reused for files and its native ACL
    helper for directories. Adoption may expose a public directory verifier.
    """

    def __init__(self, directory: str | Path):
        self._fd = -1
        try:
            directory = Path(directory)
            if not directory.is_absolute():
                raise LinkError("invalid_credential_directory")
            directory.mkdir(mode=0o700, exist_ok=True)
            self._fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self._verify_directory()
        except (OSError, CredentialError):
            self.close()
            raise LinkError("credential_directory_unavailable") from None

    def _verify_directory(self):
        info = os.fstat(self._fd)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise CredentialError("Private directory permissions are unsafe")
        _verify_posix_acl(self._fd)

    def close(self):
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __repr__(self):
        return "FileCredentialVault(<protected>)"

    @staticmethod
    def _name(credential_ref):
        if not _ref(credential_ref):
            raise LinkError("invalid_credential_reference")
        return credential_ref + ".json"

    def write(self, row: Publication, token: str) -> None:
        name = self._name(row.credential_ref)
        fd = -1
        created = False
        try:
            self._verify_directory()
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=self._fd)
            created = True
            verify_private_file(fd)
            data = json.dumps({"version": 1, "publication_ref": row.publication_ref,
                "credential_ref": row.credential_ref, "token": token,
                "token_digest": row.token_digest, "expires_at": row.expires_at},
                separators=(",", ":")).encode()
            if not _TOKEN.fullmatch(token) or hashlib.sha256(token.encode()).hexdigest() != row.token_digest:
                raise LinkError("credential_integrity_failure")
            with os.fdopen(fd, "wb", closefd=False) as output:
                output.write(data)
                output.flush()
                os.fsync(fd)
            os.fsync(self._fd)
        except (OSError, CredentialError):
            if created:
                try:
                    os.unlink(name, dir_fd=self._fd)
                    os.fsync(self._fd)
                except OSError:
                    pass
            raise LinkError("credential_write_unavailable") from None
        except BaseException:
            if created:
                os.unlink(name, dir_fd=self._fd)
                os.fsync(self._fd)
            raise
        finally:
            if fd >= 0:
                os.close(fd)

    def read(self, row: Publication) -> str:
        fd = -1
        try:
            self._verify_directory()
            fd = os.open(self._name(row.credential_ref), os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self._fd)
            verify_private_file(fd)
            info = os.fstat(fd)
            if info.st_nlink != 1 or info.st_size > 2048:
                raise LinkError("credential_integrity_failure")
            with os.fdopen(fd, "rb", closefd=False) as source:
                data = json.loads(source.read(2049))
            if (not isinstance(data, dict) or set(data) != {"version", "publication_ref", "credential_ref",
                    "token", "token_digest", "expires_at"}
                    or type(data["version"]) is not int or data["version"] != 1
                    or data["publication_ref"] != row.publication_ref
                    or data["credential_ref"] != row.credential_ref
                    or type(data["expires_at"]) is not int or data["expires_at"] != row.expires_at
                    or data["token_digest"] != row.token_digest
                    or not isinstance(data["token"], str) or not _TOKEN.fullmatch(data["token"])
                    or not hmac.compare_digest(hashlib.sha256(data["token"].encode()).hexdigest(), row.token_digest)):
                raise LinkError("credential_integrity_failure")
            return data["token"]
        except (OSError, CredentialError, ValueError, TypeError):
            raise LinkError("credential_unavailable") from None
        finally:
            if fd >= 0:
                os.close(fd)

    def delete(self, credential_ref: str):
        name = self._name(credential_ref)
        try:
            self._verify_directory()
            try:
                os.unlink(name, dir_fd=self._fd)
            except FileNotFoundError:
                pass
            os.fsync(self._fd)
        except (OSError, CredentialError):
            raise LinkError("credential_cleanup_unavailable") from None

    def page(self, after_ref: str | None, limit: int) -> tuple[str, ...]:
        try:
            self._verify_directory()
            # Directory enumeration only, not token contents; the caller owns a
            # bounded publication quota, so this is not an unbounded public path.
            return tuple(sorted(name[:-5] for name in os.listdir(self._fd)
                if name.endswith(".json") and _ref(name[:-5])
                and (after_ref is None or name[:-5] > after_ref))[:limit])
        except (OSError, CredentialError):
            raise LinkError("credential_cleanup_unavailable") from None

    def cleanup_metadata(self, credential_ref: str) -> tuple[int | None, int]:
        """Read protected expiry, or conservative mtime for an incomplete write."""
        fd = -1
        try:
            self._verify_directory()
            fd = os.open(self._name(credential_ref), os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self._fd)
            verify_private_file(fd)
            info = os.fstat(fd)
            if info.st_nlink != 1 or info.st_size > 2048:
                raise LinkError("credential_cleanup_unavailable")
            try:
                with os.fdopen(fd, "rb", closefd=False) as source:
                    data = json.loads(source.read(2049))
                if (isinstance(data, dict) and data.get("credential_ref") == credential_ref
                        and _timestamp(data.get("expires_at"))):
                    return data["expires_at"], int(info.st_mtime)
            except (ValueError, TypeError):
                pass
            return None, int(info.st_mtime)
        except (OSError, CredentialError):
            raise LinkError("credential_cleanup_unavailable") from None
        finally:
            if fd >= 0:
                os.close(fd)


def _origin(value: str, allow_loopback: bool) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (not isinstance(value, str) or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or "?" in value or "#" in value
                or parsed.path not in {"", "/"} or "\\" in value
                or any(c.isspace() or ord(c) < 32 for c in value)):
            raise ValueError
        if allow_loopback:
            if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"} or not port:
                raise ValueError
        else:
            if parsed.scheme != "https" or port not in {None, 443}:
                raise ValueError
            try:
                address = ipaddress.ip_address(parsed.hostname)
            except ValueError:
                address = None  # DNS names are configured, never fetched here.
            if address is not None and not address.is_global:
                raise ValueError
            if parsed.hostname in {"localhost", "localhost.localdomain"}:
                raise ValueError
        return value.rstrip("/")
    except (ValueError, TypeError, AttributeError):
        raise LinkError("invalid_download_origin") from None


class FileLinkPublisher:
    def __init__(self, *, store: PublicationStore, vault: FileCredentialVault,
                 open_artifact: Callable[[ArtifactRef], AbstractAsyncContextManager[VerifiedStream]],
                 origin: str, clock=time.time, allow_loopback: bool = False,
                 max_concurrent: int = 4):
        if type(max_concurrent) is not int or not 1 <= max_concurrent <= 32:
            raise LinkError("invalid_download_capacity")
        self.store, self.vault, self.open_artifact = store, vault, open_artifact
        self.origin = _origin(origin, allow_loopback)
        self._clock, self._last_now = clock, 0
        self.max_concurrent, self._active = max_concurrent, 0

    def __repr__(self):
        return "FileLinkPublisher(<protected>)"

    def now(self) -> int:
        value = self._clock()
        if type(value) not in {int, float} or not 0 <= value <= 2**63 - 1 or not math.isfinite(value):
            raise LinkError("invalid_download_clock")
        self._last_now = max(self._last_now, int(value))
        return self._last_now

    async def _active_row(self, row: Publication | None) -> Publication:
        if row is None or row.state != "active":
            raise LinkError("publication_unavailable")
        if row.expires_at <= self.now():
            await self.store.deactivate(row.publication_ref, "expired")
            self.vault.delete(row.credential_ref)
            raise LinkError("publication_unavailable")
        return row

    def _result(self, row: Publication, token: str) -> PublishedFile:
        url = row.origin + "/files/" + token
        if not hmac.compare_digest(hashlib.sha256(url.encode()).hexdigest(), row.wire_commitment):
            raise LinkError("publication_integrity_failure")
        return PublishedFile(row.publication_ref, row.credential_ref, row.expires_at, row.wire_commitment, url)

    @_safe_operation
    async def publish(self, artifact: ArtifactRef, *, request_key: str, expires_at: int) -> PublishedFile:
        if not isinstance(artifact, ArtifactRef) or not _digest(request_key) or not _timestamp(expires_at):
            raise LinkError("invalid_publication_request")
        existing = await self.store.by_request(request_key)
        if existing is not None:
            if (existing.artifact != artifact or existing.expires_at != expires_at or existing.origin != self.origin):
                raise LinkError("publication_request_conflict")
            return await self.restore(existing.credential_ref)
        now = self.now()
        if not now < expires_at <= now + MAX_TTL:
            raise LinkError("invalid_publication_expiry")
        token = secrets.token_urlsafe(32)
        row = Publication(secrets.token_hex(16), secrets.token_hex(16), request_key, artifact,
            hashlib.sha256(token.encode()).hexdigest(),
            hashlib.sha256((self.origin + "/files/" + token).encode()).hexdigest(),
            self.origin, now, expires_at)
        self.vault.write(row, token)
        # If create has an unknown result, leave the credential for recovery.
        # The manifest owner must GC unreferenced credential files conservatively.
        stored = await self.store.create(row)
        if stored != row:
            self.vault.delete(row.credential_ref)
            if (stored.artifact != artifact or stored.expires_at != expires_at or stored.origin != self.origin):
                raise LinkError("publication_request_conflict")
        return await self.restore(stored.credential_ref)

    @_safe_operation
    async def restore(self, credential_ref: str) -> PublishedFile:
        if not _ref(credential_ref):
            raise LinkError("invalid_credential_reference")
        row = await self._active_row(await self.store.by_credential(credential_ref))
        if row.origin != self.origin:
            raise LinkError("publication_origin_changed")
        return self._result(row, self.vault.read(row))

    @_safe_operation
    async def revoke(self, publication_ref: str):
        if not _ref(publication_ref):
            raise LinkError("invalid_publication_reference")
        row = await self.store.get(publication_ref)
        if row is not None:
            await self.store.deactivate(publication_ref, "revoked")
            self.vault.delete(row.credential_ref)

    @_safe_operation
    async def delete(self, publication_ref: str):
        """Delete this publication/credential; artifact owner performs reference GC."""
        if not _ref(publication_ref):
            raise LinkError("invalid_publication_reference")
        row = await self.store.get(publication_ref)
        if row is not None:
            await self.store.deactivate(publication_ref, "deleted")
            self.vault.delete(row.credential_ref)

    @_safe_operation
    async def expire(self, publication_ref: str):
        """Bounded scheduler hook; caller provides due IDs, no independent queue."""
        if not _ref(publication_ref):
            raise LinkError("invalid_publication_reference")
        row = await self.store.get(publication_ref)
        if row is not None and row.expires_at <= self.now():
            await self.store.deactivate(publication_ref, "expired")
            self.vault.delete(row.credential_ref)

    @_safe_operation
    async def collect_credentials(self, *, after_ref: str | None = None, limit: int = 100) -> dict:
        """Bounded maintenance hook, not a queue. Never collect a live orphan.

        A failed DB insert may actually have committed, or still be in flight.
        An orphan with known expiry is retained until that original expiry.
        An incomplete, unreferenced file waits at least MAX_TTL + 60 since mtime.
        The caller rotates next_ref; unsafe entries are skipped, not repaired.
        """
        if (after_ref is not None and not _ref(after_ref)) or type(limit) is not int or not 1 <= limit <= 100:
            raise LinkError("invalid_cleanup_request")
        refs = self.vault.page(after_ref, limit)
        now, deleted, skipped = self.now(), 0, 0
        for credential_ref in refs:
            try:
                row = await self.store.by_credential(credential_ref)
                if row is not None:
                    if row.expires_at > now and row.state == "active":
                        continue
                    if row.state == "active":
                        await self.store.deactivate(row.publication_ref, "expired")
                else:
                    expiry, modified_at = self.vault.cleanup_metadata(credential_ref)
                    if (expiry is not None and now < expiry) or (expiry is None and now < modified_at + MAX_TTL + 60):
                        continue
                self.vault.delete(credential_ref)
                deleted += 1
            except Exception:  # noqa: BLE001 - unsafe/unavailable entries remain pinned.
                skipped += 1
        return {"scanned": len(refs), "deleted": deleted, "skipped": skipped,
                "next_ref": refs[-1] if len(refs) == limit else None}


def _headers() -> dict[str, str]:
    return {"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'none'; sandbox", "Accept-Ranges": "none"}


def create_download_app(publisher: FileLinkPublisher | None = None, *, publisher_getter=None) -> web.Application:
    """Only attachment GET/HEAD; no sessions, cookies, redirects or HTML preview.

    Use create_download_runner to disable token-bearing access logs. Reverse
    proxies must likewise disable/redact the entire credential route, including
    error/tracing logs; the app cannot control an upstream proxy's logging.
    """
    if (publisher is None) == (publisher_getter is None):
        raise LinkError("invalid_download_publisher")
    if publisher_getter is not None and not callable(publisher_getter):
        raise LinkError("invalid_download_publisher")
    fixed_publisher = publisher

    async def download(request: web.Request) -> web.StreamResponse:
        publisher = None
        response = None
        acquired = False
        try:
            publisher = publisher_getter() if publisher_getter is not None else fixed_publisher
            if not isinstance(publisher, FileLinkPublisher):
                return web.Response(status=503, headers=_headers())
            token = request.match_info.get("token", "")
            if not _TOKEN.fullmatch(token):
                return web.Response(status=404, headers=_headers())
            row = await publisher._active_row(await publisher.store.by_digest(
                hashlib.sha256(token.encode()).hexdigest()))
            if not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), row.token_digest):
                return web.Response(status=404, headers=_headers())
            # Public HTTP access uses only the digest, never the credential vault.
            # Header tokens/cookies/referrers grant no additional authority.
            if "Range" in request.headers or "If-Range" in request.headers:
                return web.Response(status=416, headers=_headers())
            headers = _headers()
            headers["Content-Type"] = row.artifact.mime
            headers["Content-Disposition"] = 'attachment; filename="download"; filename*=UTF-8\'\'' + quote(row.artifact.filename, safe="")
            headers["Content-Length"] = str(row.artifact.size)
            if request.method == "HEAD":
                return web.Response(status=200, headers=headers)
            if publisher._active >= publisher.max_concurrent:
                return web.Response(status=503, headers=_headers())
            publisher._active += 1
            acquired = True
            async with asyncio.timeout(max(0, row.expires_at - publisher.now())), publisher.open_artifact(row.artifact) as source:
                if (not isinstance(source, VerifiedStream) or source.size != row.artifact.size
                        or source.sha256 != row.artifact.sha256):
                    raise LinkError("artifact_source_unavailable")
                await publisher._active_row(await publisher.store.get(row.publication_ref))
                response = web.StreamResponse(status=200, headers=headers)
                await response.prepare(request)
                digest, size = hashlib.sha256(), 0
                async for block in source.chunks:
                    current = await publisher._active_row(await publisher.store.get(row.publication_ref))
                    if (current.artifact != row.artifact or current.token_digest != row.token_digest
                            or current.expires_at != row.expires_at):
                        raise LinkError("publication_integrity_failure")
                    if type(block) is not bytes or not 0 < len(block) <= MAX_CHUNK:
                        raise LinkError("invalid_artifact_stream")
                    size += len(block)
                    if size > row.artifact.size:
                        raise LinkError("invalid_artifact_stream")
                    digest.update(block)
                    await response.write(block)
                if size != row.artifact.size or digest.hexdigest() != row.artifact.sha256:
                    raise LinkError("artifact_integrity_failure")
                await publisher._active_row(await publisher.store.get(row.publication_ref))
                await response.write_eof()
                if request.transport is not None and not request.transport.is_closing():
                    await publisher.store.record_served(row.publication_ref, now=publisher.now(),
                        sha256=digest.hexdigest(), size=size)
                return response
        except Exception:  # noqa: BLE001 - transport boundary must never log credential-bearing exceptions.
            # No raw exception, URL, artifact path, provider data or traceback logs.
            if response is not None and response.prepared:
                response.force_close()
                if request.transport is not None:
                    request.transport.close()
                return response
            return web.Response(status=404, headers=_headers())
        finally:
            if acquired:
                publisher._active -= 1

    @web.middleware
    async def safe_errors(request, handler):
        try:
            return await handler(request)
        except web.HTTPException as error:
            return web.Response(status=error.status, headers=_headers())
        except Exception:  # noqa: BLE001 - keep framework error responses opaque.
            return web.Response(status=404, headers=_headers())

    # Isolated instance: never mutate shared application/global logger settings.
    safe_logger = logging.Logger("attachment_download", level=logging.CRITICAL + 1)  # noqa: LOG001
    safe_logger.propagate = False
    safe_logger.addHandler(logging.NullHandler())
    app = web.Application(middlewares=[safe_errors], client_max_size=1024, logger=safe_logger,
                          handler_args={"logger": safe_logger, "debug": False})
    app.router.add_get("/files/{token}", download, allow_head=True)
    return app


def create_download_runner(app: web.Application) -> web.AppRunner:
    return web.AppRunner(app, access_log=None, handle_signals=False, shutdown_timeout=5)
