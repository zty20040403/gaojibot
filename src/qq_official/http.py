"""Bounded QQ REST requests with explicit acceptance evidence and no send retry."""
from __future__ import annotations

import json as json_module
import math
import re
import weakref
import unicodedata
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from urllib.parse import quote, unquote_to_bytes
from typing import TYPE_CHECKING, Any

import httpx

from .credentials import validate_api_base
from .errors import QQAPIError, classify_error

if TYPE_CHECKING:
    from .token import TokenManager

_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_TARGET = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")


@dataclass(frozen=True)
class ProtocolResponse:
    """Internal synchronous-response evidence; raw bodies never enter repr/logs."""
    status: int
    body: dict[str, Any] = field(repr=False)


_request_guard: ContextVar[Callable[[str], Awaitable[None]] | None] = ContextVar(
    "qq_physical_request_guard", default=None)


@contextmanager
def scoped_request_guard(guard):
    """Host-only per-task guard, including every physical token refresh retry."""
    if not callable(guard):
        raise ValueError("Invalid physical request guard")
    token = _request_guard.set(guard)
    try:
        yield
    finally:
        _request_guard.reset(token)


class PreparedUpload:
    """Single-use token snapshot for an exact validated media endpoint."""
    __slots__ = ("__weakref__",)


class PreparedSend:
    """Single-use opaque handle issued by one RestClient for one exact route."""
    __slots__ = ("__weakref__",)


def _code(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d{1,12}", value):
        return int(value)
    return None


async def request_json_response(
    client: httpx.AsyncClient, method: str, url: str, *,
    headers: dict[str, str] | None = None, json: dict[str, Any] | None = None,
    stage: str = "request", timeout: float = 30.0,
    secrets: tuple[str, ...] = (),
    before_dispatch: Callable[[], None] | None = None,
) -> ProtocolResponse:
    """Internal transport helper. Callers must validate URL before this call.

    Do not call raise_for_status: its exception contains request URLs and may
    retain credentials. Do not follow redirects, including on injected clients.
    """
    # This is before transport and outside HTTPError classification: denied
    # local authority never becomes a misleading ambiguous provider outcome.
    guard = _request_guard.get()
    if guard is not None:
        await guard(stage)
    if before_dispatch is not None:
        before_dispatch()
    try:
        async with client.stream(
            method, url, headers=headers, json=json, timeout=timeout, follow_redirects=False,
        ) as response:
            status = response.status_code
            raw_id = response.headers.get("x-tps-trace-id", "")
            request_id = raw_id if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", raw_id) else None
            if request_id and any(secret and secret in request_id for secret in secrets):
                request_id = None
            retry_after = None
            try:
                value = float(response.headers.get("retry-after", ""))
                if math.isfinite(value) and 0 <= value <= 86400:
                    retry_after = value
            except ValueError:
                pass
            content = bytearray()
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > _MAX_RESPONSE_BYTES:
                    raise QQAPIError("response_too_large", status=status, stage=stage)
                content.extend(chunk)
    except QQAPIError:
        raise
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
        raise QQAPIError("connection_failed", accepted=False, stage=stage) from None
    except httpx.HTTPError:
        # Read/write timeout, disconnect, malformed transport and 5xx may happen
        # after acceptance. Preserve ambiguity; never retry here.
        raise QQAPIError("transport_ambiguous", stage=stage) from None
    try:
        data = json_module.loads(content)
    except (ValueError, UnicodeError):
        if status in {401, 429}:
            raise classify_error(status, None, stage=stage, request_id=request_id, retry_after=retry_after) from None
        raise QQAPIError("invalid_response", status=status, stage=stage, request_id=request_id) from None
    if not isinstance(data, dict):
        raise QQAPIError("invalid_response", status=status, stage=stage, request_id=request_id)
    codes = {_code(data[key]) for key in ("code", "err_code") if key in data}
    codes.discard(0)
    malformed_code = None in codes
    codes.discard(None)
    if status >= 300 or status < 200 or codes or malformed_code:
        code = next(iter(codes)) if len(codes) == 1 and not malformed_code else None
        if stage == "upload" and data.get("id") is not None and data.get("id") != "":
            # srv_send_msg=false forbids a message receipt. A simultaneous
            # error code cannot erase that conflicting acceptance evidence.
            raise QQAPIError("unexpected_upload_message_evidence", status=status,
                             code=code, stage=stage, request_id=request_id)
        raise classify_error(status, code, stage=stage, request_id=request_id, retry_after=retry_after)
    return ProtocolResponse(status, data)


async def request_json(
    client: httpx.AsyncClient, method: str, url: str, *,
    headers: dict[str, str] | None = None, json: dict[str, Any] | None = None,
    stage: str = "request", timeout: float = 30.0,
    secrets: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Compatibility wrapper: existing callers still receive the same dict."""
    result = await request_json_response(client, method, url, headers=headers,
        json=json, stage=stage, timeout=timeout, secrets=secrets)
    return result.body


class RestClient:
    """Account-bound REST transport.

    Consumers must keep httpx/httpcore request logging out of public logs:
    dependency INFO/DEBUG logging can include the endpoint URL (openid).
    This library itself never logs requests, payloads or remote error bodies.
    """
    def __init__(
        self, token_manager: TokenManager, base_url: str | None = None, *,
        client: httpx.AsyncClient | None = None, allow_test_server: bool = False,
        timeout: float = 30.0,
    ) -> None:
        self.token_manager = token_manager
        self.allow_test_server = allow_test_server
        self.base_url = validate_api_base(
            base_url or token_manager.base_url, allow_test_server=allow_test_server,
            credentials=token_manager.credentials,
        )
        if self.base_url != token_manager.base_url:
            raise ValueError("REST and token environments must match")
        self._client = client or httpx.AsyncClient(trust_env=False, follow_redirects=False)
        self._owns_client = client is None
        self._timeout = timeout
        self._prepared_sends = weakref.WeakKeyDictionary()
        self._prepared_uploads = weakref.WeakKeyDictionary()

    async def request(self, method: str, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        if (
            not path.startswith("/") or path.startswith("//") or "\\" in path
            or "?" in path or "#" in path or re.search(r"\s", path)
            or any(part in {".", ".."} for part in path.split("/"))
            or "%" in path
        ):
            raise ValueError("Request requires a canonical relative platform path")
        token = await self.token_manager.get()
        return await self._request_with_token(method, path, token, json)

    async def _request_with_token(self, method, path, token, json):
        """Already validated internal route; no await/auth refresh before HTTP."""
        response = await self._request_response_with_token(method, path, token, json,
            stage="upload" if path.endswith("/files") else "request")
        return response.body

    async def _request_response_with_token(self, method, path, token, json, *, stage,
                                           before_dispatch=None):
        try:
            return await request_json_response(
                self._client, method, self.base_url + path,
                headers={"Authorization": "QQBot " + token}, json=json,
                stage=stage, timeout=self._timeout, before_dispatch=before_dispatch,
                secrets=(token, self.token_manager.credentials.app_id, self.token_manager.credentials.app_secret),
            )
        except QQAPIError as error:
            if error.category == "authentication" and error.confirmed_not_accepted:
                # The response can arrive after a concurrent refresh. Invalidate
                # only this request's rejected token; never a newer cached token.
                self.token_manager.invalidate(token)
            # Policy/sender decides what to do next; no automatic message retry.
            raise

    @staticmethod
    def _upload_path(path):
        # Dedicated media route. Old message/request validation is unchanged.
        if not isinstance(path, str):
            raise ValueError("Invalid media route")
        pieces = path.split("/")
        if (len(pieces) != 5 or pieces[:2] != ["", "v2"]
                or pieces[2] not in {"groups", "users"}
                or pieces[4] not in {"files", "upload_prepare", "upload_part_finish"}):
            raise ValueError("Invalid media route")
        try:
            native = unquote_to_bytes(pieces[3]).decode("utf-8")
            if (not native.strip() or len(native.encode()) > 256 or native in {".", ".."}
                    or "/" in native or "\\" in native
                    or any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in native)
                    or quote(native, safe="") != pieces[3]):
                raise ValueError
        except (UnicodeError, ValueError):
            raise ValueError("Invalid media route") from None
        return path

    async def prepare_upload(self, path: str) -> PreparedUpload:
        from .token import PreparedToken
        path = self._upload_path(path)
        token = await self.token_manager.prepare()
        if not isinstance(token, PreparedToken) or not token.is_valid():
            raise QQAPIError("prepared_token_unavailable", accepted=False, stage="token")
        prepared = PreparedUpload()
        self._prepared_uploads[prepared] = (path, token)
        return prepared

    async def upload(self, path: str, payload: dict[str, Any], *, prepared: PreparedUpload,
                     before_dispatch: Callable[[], None]) -> ProtocolResponse:
        path = self._upload_path(path)
        # Consume before any await, including a validation failure.
        state = self._prepared_uploads.pop(prepared, None) if isinstance(prepared, PreparedUpload) else None
        if state is None or state[0] != path:
            raise QQAPIError("prepared_upload_unavailable", accepted=False, stage="token")
        if (not isinstance(payload, dict) or not callable(before_dispatch)
                or (path.endswith("/files") and payload.get("srv_send_msg") is not False)):
            raise ValueError("Invalid upload payload or guard")
        token = state[1].value_for_request()
        def final_check():
            # Recheck after the asynchronous physical source guard. No refresh.
            state[1].value_for_request()
            before_dispatch()
        return await self._request_response_with_token("POST", path, token, payload,
            stage="upload", before_dispatch=final_check)

    async def gateway(self) -> dict[str, Any]:
        return await self.request("GET", "/gateway/bot")

    @staticmethod
    def _message_path(kind, target):
        if kind not in {"group", "c2c", "user", "private"} or not _TARGET.fullmatch(target):
            raise ValueError("Invalid message endpoint")
        route = "groups" if kind == "group" else "users"
        return f"/v2/{route}/{target}/messages"

    async def prepare_send(self, kind: str, target: str) -> PreparedSend:
        """Do all token work before a caller's final rate/lease/deadline checks."""
        from .token import PreparedToken
        path = self._message_path(kind, target)
        token = await self.token_manager.prepare()
        if not isinstance(token, PreparedToken) or not token.is_valid():
            raise QQAPIError("prepared_token_unavailable", accepted=False, stage="token")
        prepared = PreparedSend()
        self._prepared_sends[prepared] = (path, token)
        return prepared

    def prepared_is_current(self, prepared: PreparedSend) -> bool:
        if not isinstance(prepared, PreparedSend):
            return False
        state = self._prepared_sends.get(prepared)
        return bool(state is not None and state[1].is_valid())

    async def send_message(self, kind: str, target: str, payload: dict[str, Any], *,
                           prepared: PreparedSend | None = None) -> dict[str, Any]:
        path = self._message_path(kind, target)
        if prepared is None:
            result = await self.request("POST", path, json=payload)
        else:
            # Pop before any network await, including rejection: a ticket cannot
            # authorize a second physical call, another RestClient or endpoint.
            state = self._prepared_sends.pop(prepared, None) if isinstance(prepared, PreparedSend) else None
            if state is None or state[0] != path:
                raise QQAPIError("prepared_send_unavailable", accepted=False, stage="token")
            token = state[1].value_for_request()
            result = await self._request_with_token("POST", path, token, payload)
        if not isinstance(result.get("id"), str) or not result["id"]:
            raise QQAPIError("missing_receipt", stage="send")
        return result

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
