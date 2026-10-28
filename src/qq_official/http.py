"""Bounded QQ REST requests with explicit acceptance evidence and no send retry."""
from __future__ import annotations

import json as json_module
import math
import re
from typing import TYPE_CHECKING, Any

import httpx

from .credentials import validate_api_base
from .errors import QQAPIError, classify_error

if TYPE_CHECKING:
    from .token import TokenManager

_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_TARGET = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")


def _code(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d{1,12}", value):
        return int(value)
    return None


async def request_json(
    client: httpx.AsyncClient, method: str, url: str, *,
    headers: dict[str, str] | None = None, json: dict[str, Any] | None = None,
    stage: str = "request", timeout: float = 30.0,
    secrets: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Internal transport helper. Callers must validate URL before this call.

    Do not call raise_for_status: its exception contains request URLs and may
    retain credentials. Do not follow redirects, including on injected clients.
    """
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
        raise classify_error(status, code, stage=stage, request_id=request_id, retry_after=retry_after)
    return data


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

    async def request(self, method: str, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        if (
            not path.startswith("/") or path.startswith("//") or "\\" in path
            or "?" in path or "#" in path or re.search(r"\s", path)
            or any(part in {".", ".."} for part in path.split("/"))
            or "%" in path
        ):
            raise ValueError("Request requires a canonical relative platform path")
        token = await self.token_manager.get()
        try:
            return await request_json(
                self._client, method, self.base_url + path,
                headers={"Authorization": "QQBot " + token}, json=json,
                stage="upload" if path.endswith("/files") else "request", timeout=self._timeout,
                secrets=(token, self.token_manager.credentials.app_id, self.token_manager.credentials.app_secret),
            )
        except QQAPIError as error:
            if error.category == "authentication" and error.confirmed_not_accepted:
                # The response can arrive after a concurrent refresh. Invalidate
                # only this request's rejected token; never a newer cached token.
                self.token_manager.invalidate(token)
            # Policy/sender decides what to do next; no automatic message retry.
            raise

    async def gateway(self) -> dict[str, Any]:
        return await self.request("GET", "/gateway/bot")

    async def send_message(self, kind: str, target: str, payload: dict[str, Any]) -> dict[str, Any]:
        if kind not in {"group", "c2c", "user", "private"} or not _TARGET.fullmatch(target):
            raise ValueError("Invalid message endpoint")
        route = "groups" if kind == "group" else "users"
        result = await self.request("POST", f"/v2/{route}/{target}/messages", json=payload)
        if not isinstance(result.get("id"), str) or not result["id"]:
            raise QQAPIError("missing_receipt", stage="send")
        return result

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
