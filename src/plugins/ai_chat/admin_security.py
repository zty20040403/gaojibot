from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from src.bot_security.passwords import hash_password
from src.bot_security.service import current_principal, principal
from src.bot_security.store import SecurityError, SecurityStore

SESSION_COOKIE = "gaoji_session"
CSRF_COOKIE = "gaoji_csrf"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=128)


class AccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=12, max_length=128)
    role: Literal["admin", "member"] = "member"
    qq_id: str | None = None


class AccountUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    password: str | None = Field(default=None, min_length=12, max_length=128)
    role: Literal["admin", "member"] | None = None
    qq_id: str | None = None
    enabled: bool | None = None


def require_admin() -> dict[str, Any]:
    account = principal()
    if account["role"] != "admin":
        raise SecurityError("普通成员仅可查看机器人基础状态")
    return account


def api_suffix(path: str, prefix: str) -> str:
    suffix = path.removeprefix(prefix + "/api")
    if suffix == "/v1" or suffix.startswith("/v1/"):
        suffix = suffix[3:]
    return suffix


def _origin(request: Request, configured: str) -> str:
    return configured.rstrip("/") or str(request.base_url).rstrip("/")


def secure_route(store: SecurityStore | None, *, prefix: str, origin: str = "") -> type[APIRoute]:
    class AccountRoute(APIRoute):
        def get_route_handler(self):
            original = super().get_route_handler()

            async def handler(request: Request):
                if not request.url.path.startswith(prefix + "/api/"):
                    return await original(request)
                identity_token = None
                try:
                    if store is None:
                        raise SecurityError("账户认证尚未初始化，控制台已锁定；请按部署说明初始化管理员", 503)
                    suffix = api_suffix(request.url.path, prefix)
                    write = request.method not in SAFE_METHODS
                    if write and request.headers.get("origin", "") != _origin(request, origin):
                        raise SecurityError("请求来源校验失败")
                    if suffix != "/auth/login":
                        account = await asyncio.to_thread(store.session, request.cookies.get(SESSION_COOKIE, ""),
                            request.headers.get("x-csrf-token", "") if write else None)
                        identity_token = current_principal.set(account)
                        if suffix not in {"/me", "/status", "/auth/logout"}:
                            require_admin()
                    handled_security_action = (
                        (request.method == "POST" and suffix in {"/auth/login", "/auth/logout", "/accounts"})
                        or (request.method == "PUT" and re.fullmatch(r"/accounts/[^/]+", suffix))
                    )
                    if write and not handled_security_action:
                        body_bytes = await request.body()
                        if len(body_bytes) > 128 * 1024:
                            raise SecurityError("操作内容过长", 413)
                        try:
                            if body_bytes:
                                json.loads(body_bytes)
                        except (ValueError, UnicodeError):
                            raise SecurityError("操作参数必须是 JSON", 400) from None
                    response = await original(request)
                    response.headers["Cache-Control"] = "no-store"
                    return response
                except SecurityError as exc:
                    return JSONResponse({"detail": str(exc)}, status_code=exc.status, headers={"Cache-Control": "no-store"})
                finally:
                    if identity_token is not None:
                        current_principal.reset(identity_token)

            return handler

    return AccountRoute


def register_security_routes(router: APIRouter, store: SecurityStore | None, services: Any, *, prefix: str, origin: str) -> None:

    @router.post("/api/auth/login")
    async def login(body: LoginRequest, request: Request):
        secure = _origin(request, origin).startswith("https://")
        if not secure and request.url.hostname not in {"localhost", "127.0.0.1", "::1", "test"}:
            raise SecurityError("账户登录需要 HTTPS；本机调试可用 localhost", 400)
        credentials = await asyncio.to_thread(store.login, body.username, body.password,
                                              request.client.host if request.client else "unknown")
        response = JSONResponse({"account": credentials["account"]})
        response.set_cookie(SESSION_COOKIE, credentials["session"], max_age=28800, httponly=True, secure=secure, samesite="strict", path=prefix)
        response.set_cookie(CSRF_COOKIE, credentials["csrf"], max_age=28800, httponly=False, secure=secure, samesite="strict", path=prefix)
        return response

    @router.post("/api/auth/logout")
    async def logout(request: Request):
        await asyncio.to_thread(store.logout, request.cookies.get(SESSION_COOKIE, ""))
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path=prefix)
        response.delete_cookie(CSRF_COOKIE, path=prefix)
        return response

    @router.get("/api/me")
    async def me():
        account = {key: value for key, value in principal().items() if key != "session_hash"}
        return {"account": account}

    @router.get("/api/status")
    async def status():
        import time
        getter = services.official_observability
        diagnostics = getter() if callable(getter) else None
        connected = diagnostics is not None and diagnostics.snapshot()['healthy']
        return {"version": services.version, "uptime_seconds": max(0, int(time.time()) - services.started_at),
                "process": "running", "qq_connected": bool(connected)}

    @router.get("/api/accounts")
    async def accounts():
        require_admin()
        return {"items": await asyncio.to_thread(store.accounts)}

    @router.post("/api/accounts")
    async def create_account(body: AccountCreate):
        account = require_admin()
        username = body.username.strip().lower()
        store.validate_account(username, body.role, body.qq_id)
        encoded = await asyncio.to_thread(hash_password, body.password)
        result = await asyncio.to_thread(store.apply_account_change, account,
            {"action": "create", "username": username, "password_hash": encoded, "role": body.role, "qq_id": body.qq_id})
        return {"ok": True, "account": result}

    @router.put("/api/accounts/{account_id}")
    async def update_account(account_id: str, body: AccountUpdate):
        account = require_admin()
        changes = body.model_dump(exclude_unset=True, exclude={"expected_version"})
        password = changes.pop("password", None)
        if any(changes.get(key, "absent") is None for key in ("role", "enabled")):
            raise SecurityError("角色和启用状态不能为空", 400)
        if password:
            changes["password_hash"] = await asyncio.to_thread(hash_password, password)
        if not changes:
            raise SecurityError("没有账户变更", 400)
        target = next((item for item in await asyncio.to_thread(store.accounts) if item["account_id"] == account_id), None)
        if not target:
            raise SecurityError("账户不存在", 404)
        store.validate_account(target["username"], changes.get("role", target["role"]), changes.get("qq_id", target["qq_id"]))
        result = await asyncio.to_thread(store.apply_account_change, account,
            {"action": "update", "account_id": account_id, "expected_version": body.expected_version, "changes": changes})
        return {"ok": True, "account": result}

    @router.get("/api/security-audit")
    async def audit():
        require_admin()
        return {"items": await asyncio.to_thread(store.audit)}
