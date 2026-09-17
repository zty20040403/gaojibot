from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urlsplit

import httpx


MAX_REQUEST_BYTES = 4096
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TOKEN_FILE_BYTES = 515
SUPPORTED_CATALOG_VERSIONS = frozenset({1, 2})


class OpsError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class OpsOperation:
    name: str
    params_schema: dict[str, Any]
    read_only: bool = True


@dataclass(frozen=True)
class OpsResponse:
    data: Any
    elapsed_ms: int


class OpsClient:
    backend_name = "ops"

    def authorization_binding(self) -> dict[str, Any]:
        return {"url": self.base_url, "identity": hashlib.sha256(self._credential()).hexdigest()}

    async def catalog(self) -> dict[str, Any]:
        response = await self._request("GET", "/v1/operations")
        if not isinstance(response.data, dict):
            raise OpsError("invalid_catalog", "Operations catalog must be an object")
        return response.data

    async def call(self, operation: str, params: dict[str, Any], *,
                   idempotency_key: str | None = None) -> OpsResponse:
        body = json.dumps({"op": operation, "params": params}, ensure_ascii=False,
                          sort_keys=True, separators=(",", ":")).encode()
        return await self._request("POST", "/v1/execute", body=body,
                                   idempotency_key=idempotency_key)

    def __init__(
        self,
        base_url: str,
        token_file: str | Path,
        *,
        timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
        catalog_cache_seconds: float = 20.0,
    ) -> None:
        self.base_url = base_url.strip().rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Ops URL must be absolute HTTP(S)")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Ops URL contains unsupported components")
        self.token_file = Path(token_file)
        self.timeout_seconds = min(max(float(timeout_seconds), 1.0), 30.0)
        self.catalog_cache_seconds = min(max(float(catalog_cache_seconds), 1.0), 30.0)
        self._catalog: tuple[OpsOperation, ...] | None = None
        self._catalog_version: int | None = None
        self._catalog_at = 0.0
        self._catalog_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def catalog_version(self) -> int | None:
        return self._catalog_version

    def _credential(self) -> bytes:
        try:
            raw = self.token_file.read_bytes()
        except OSError as exc:
            raise OpsError(
                "credential_unavailable", "Ops credential is unavailable"
            ) from exc
        token = raw.rstrip(b"\r\n")
        if (
            len(raw) >= MAX_TOKEN_FILE_BYTES
            or not 32 <= len(token) <= 512
            or any(byte < 33 or byte > 126 for byte in token)
        ):
            raise OpsError("credential_invalid", "Ops credential is invalid")
        return token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        deadline: float | None = None,
        idempotency_key: str | None = None,
    ) -> OpsResponse:
        token = self._credential()
        started = monotonic()
        remaining = self.timeout_seconds
        if deadline is not None:
            remaining = min(remaining, deadline - started)
            if remaining <= 0:
                raise OpsError("timeout", "Ops request timed out", retryable=True)
        try:
            async with self._client.stream(
                method,
                f"{self.base_url}{path}",
                headers={
                    "Authorization": "Bearer " + token.decode("ascii"),
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    **({"Idempotency-Key": idempotency_key} if idempotency_key else {}),
                },
                content=body,
                timeout=httpx.Timeout(max(remaining, 0.05)),
            ) as response:
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise OpsError(
                            "response_too_large",
                            "Ops response exceeds 2 MiB",
                        )
                    chunks.append(chunk)
                payload = b"".join(chunks)
                if response.status_code == 401:
                    raise OpsError("unauthorized", "Ops rejected the credential")
                if response.status_code == 403:
                    raise OpsError("forbidden", "Ops denied this operation")
                if response.status_code == 404:
                    raise OpsError("unsupported", "Ops operation is unavailable")
                if response.status_code in {408, 429}:
                    raise OpsError(
                        "upstream_busy",
                        "Ops is temporarily busy",
                        retryable=True,
                    )
                if response.status_code >= 500:
                    raise OpsError(
                        "upstream_error",
                        f"Ops returned HTTP {response.status_code}",
                        retryable=True,
                    )
                if response.status_code >= 400:
                    raise OpsError(
                        "invalid_request",
                        f"Ops returned HTTP {response.status_code}",
                    )
        except OpsError:
            raise
        except httpx.TimeoutException as exc:
            raise OpsError(
                "timeout", "Ops request timed out", retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise OpsError(
                "transport_unavailable",
                "Ops transport is unavailable",
                retryable=True,
            ) from exc
        try:
            decoded = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OpsError("invalid_response", "Ops returned invalid JSON") from exc
        return OpsResponse(
            data=decoded,
            elapsed_ms=max(int((monotonic() - started) * 1000), 0),
        )

    async def operations(
        self,
        *,
        deadline: float | None = None,
    ) -> tuple[OpsOperation, ...]:
        if (
            self._catalog is not None
            and monotonic() - self._catalog_at < self.catalog_cache_seconds
        ):
            return self._catalog
        async with self._catalog_lock:
            if (
                self._catalog is not None
                and monotonic() - self._catalog_at < self.catalog_cache_seconds
            ):
                return self._catalog
            response = await self._request("GET", "/v1/operations", deadline=deadline)
            version, operations = self._decode_catalog(response.data)
            self._catalog = operations
            self._catalog_version = version
            self._catalog_at = monotonic()
            return operations

    @staticmethod
    def _decode_catalog(payload: Any) -> tuple[int, tuple[OpsOperation, ...]]:
        if not isinstance(payload, dict):
            raise OpsError(
                "incompatible_catalog", "Ops returned an unsupported catalog"
            )
        version = payload.get("version")
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version not in SUPPORTED_CATALOG_VERSIONS
        ):
            raise OpsError(
                "incompatible_catalog", "Ops returned an unsupported catalog"
            )
        raw_operations = payload.get("operations")
        if not isinstance(raw_operations, list):
            raise OpsError("invalid_catalog", "Ops returned an invalid catalog")
        operations: list[OpsOperation] = []
        for item in raw_operations:
            if not isinstance(item, dict) or item.get("read_only") is not True:
                continue
            name = item.get("name")
            schema = item.get("params_schema")
            if not isinstance(name, str) or not name or not isinstance(schema, dict):
                raise OpsError("invalid_catalog", "Ops returned an invalid catalog")
            operations.append(OpsOperation(name=name, params_schema=schema))
        return version, tuple(operations)

    async def execute(self, operation: str, params: dict[str, Any]) -> OpsResponse:
        if not isinstance(params, dict):
            raise OpsError("invalid_request", "Ops params must be an object")
        request = {"op": str(operation), "params": params}
        body = json.dumps(
            request, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise OpsError(
                "request_too_large", "Ops request exceeds 4096 bytes"
            )
        deadline = monotonic() + self.timeout_seconds
        catalog = await self.operations(deadline=deadline)
        if operation not in {item.name for item in catalog}:
            raise OpsError(
                "unsupported",
                "Ops operation is unavailable or not read-only",
            )
        return await self._request(
            "POST", "/v1/execute", body=body, deadline=deadline
        )
