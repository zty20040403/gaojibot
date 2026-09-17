"""Transport-independent operations used by authorization and reconciliation."""
from __future__ import annotations

from typing import Any, Protocol

from .ops import OpsOperation, OpsResponse


class OperationsBackend(Protocol):
    backend_name: str

    @property
    def catalog_version(self) -> int | None: ...

    def authorization_binding(self) -> dict[str, Any]: ...

    async def catalog(self) -> dict[str, Any]: ...

    async def operations(self) -> tuple[OpsOperation, ...]: ...

    async def execute(self, operation: str, params: dict[str, Any]) -> OpsResponse: ...

    async def call(self, operation: str, params: dict[str, Any], *,
                   idempotency_key: str | None = None) -> OpsResponse: ...

    async def close(self) -> None: ...
