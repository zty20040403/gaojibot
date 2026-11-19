from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from .store import SecurityError

current_principal: ContextVar[dict[str, Any] | None] = ContextVar("admin_principal", default=None)


def principal() -> dict[str, Any]:
    value = current_principal.get()
    if not value:
        raise SecurityError("需要账户登录", 401)
    return value


def audit_actor() -> str:
    return "account:" + principal()["account_id"] + ":" + principal()["username"]
