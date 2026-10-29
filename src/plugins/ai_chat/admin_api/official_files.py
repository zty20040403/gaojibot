"""Authenticated console retrieval of source-owned official artifacts.

This route never signs a public link, sends a platform message, or records a
download/acceptance claim. The live FileServices port owns all source checks.
"""
from __future__ import annotations

from contextlib import AsyncExitStack
import hashlib
import re
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from src.bot_security.service import principal
from src.bot_security.store import SecurityError
from ..admin_security import SESSION_COOKIE
from ..adapters.qq_official.file_links import ArtifactRef, LinkError, VerifiedStream
from .context import AdminRouteContext, authorize


_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "Content-Security-Policy": "default-src 'none'; sandbox"}
_MAX_CHUNK = 64 * 1024


class _ConsoleResponse(StreamingResponse):
    def __init__(self, *args, stack, **kwargs):
        super().__init__(*args, **kwargs)
        self._stack = stack

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        except Exception:
            # If headers have already been sent the transport must terminate;
            # no exception from a source, database or credential port is logged.
            raise RuntimeError("console_download_unavailable") from None
        finally:
            await self._stack.aclose()


def create_router(context: AdminRouteContext):
    router = context.new_router()
    services = context.services

    @router.get("/api/qq-official/artifacts/{artifact_id}/download", name="official_artifact_download")
    @router.head("/api/qq-official/artifacts/{artifact_id}/download", name="official_artifact_head")
    async def download(artifact_id: str, request: Request):
        authorize()
        # Exact metadata ID only. Paths, arbitrary account selectors and public
        # bearer tokens are not part of this authenticated endpoint's contract.
        if not re.fullmatch(r"[a-f0-9]{32}", artifact_id) or request.query_params:
            raise HTTPException(404, "Artifact unavailable", headers=_HEADERS)
        try:
            getter = services.official_file_service
            service = getter() if callable(getter) else None
        except Exception:
            service = None
        if service is None:
            raise HTTPException(404, "Artifact unavailable", headers=_HEADERS)
        if "range" in request.headers or "if-range" in request.headers:
            raise HTTPException(416, "Partial retrieval unavailable", headers=_HEADERS)
        account = dict(principal())
        cookie = request.cookies.get(SESSION_COOKIE, "")
        mobile = services.mobile_authorization

        async def authenticated():
            if mobile is None or not cookie or len(cookie) > 256:
                raise LinkError("console_session_unavailable")
            try:
                current = await service.calls(mobile.store.session, cookie)
            except SecurityError:
                raise LinkError("console_session_unavailable") from None
            if (current.get("role") != "admin" or any(current.get(key) != account.get(key)
                    for key in ("account_id", "version", "session_hash"))):
                raise LinkError("console_session_unavailable")

        stack = AsyncExitStack()
        try:
            await authenticated()
            artifact, source = await stack.enter_async_context(service.console_artifact(artifact_id))
            if (not isinstance(artifact, ArtifactRef) or artifact.artifact_id != artifact_id
                    or artifact.mime not in {"application/pdf", "application/zip"}
                    or not isinstance(source, VerifiedStream)
                    or (source.sha256, source.size) != (artifact.sha256, artifact.size)):
                raise LinkError("console_artifact_unavailable")
            await authenticated()
            headers = {**_HEADERS, "Content-Type": artifact.mime, "Content-Length": str(artifact.size),
                "Content-Disposition": 'attachment; filename="artifact"; filename*=UTF-8\'\'' + quote(artifact.filename, safe=""),
                "X-Content-SHA256": artifact.sha256}
            if request.method == "HEAD":
                await stack.aclose()
                return Response(headers=headers)

            async def chunks():
                digest, size = hashlib.sha256(), 0
                async for block in source.chunks:
                    await authenticated()
                    if type(block) is not bytes or not 0 < len(block) <= _MAX_CHUNK:
                        raise LinkError("console_artifact_unavailable")
                    size += len(block)
                    if size > artifact.size:
                        raise LinkError("console_artifact_unavailable")
                    digest.update(block)
                    yield block
                await authenticated()
                if size != artifact.size or digest.hexdigest() != artifact.sha256:
                    raise LinkError("console_artifact_unavailable")

            return _ConsoleResponse(chunks(), headers=headers, stack=stack)
        except BaseException as exc:
            await stack.aclose()
            if not isinstance(exc, Exception):
                raise
            raise HTTPException(404, "Artifact unavailable", headers=_HEADERS) from None

    return router
