"""Functional API fixture: real password login + simulated private QQ confirmation.

Used by older resource tests to exercise their original assertions through the
new auth flow. Authentication rejection tests use ordinary httpx clients instead.
"""
import re
import time
from dataclasses import replace

import httpx

from src.bot_security.service import MobileAuthorization
from src.bot_security.store import SecurityStore
from src.plugins.ai_chat.admin import register_admin as real_register_admin
from tests.postgres_fixture import isolated_postgres_for_test

PASSWORD = "fixture-password-only"


def register_admin(app, services, *, test_case, token="", **kwargs):
    store = SecurityStore(
        isolated_postgres_for_test(test_case), b"fixture-secret-for-test-only-0000"
    )
    test_case.addCleanup(store.close)
    store.bootstrap("kenneth", PASSWORD, "123456789")
    app.state.auth_clock = time.time()
    store.clock = lambda: app.state.auth_clock
    app.state.auth_messages = []
    async def sender(_bot, _qq, message):
        app.state.auth_messages.append(message)
    mobile = MobileAuthorization(store, sender=sender, bot_selector=lambda: "987654321")
    services = replace(services, mobile_authorization=mobile)
    app.state.mobile = mobile
    real_register_admin(app, services, **kwargs)


class ApprovedClient(httpx.AsyncClient):
    async def request(self, method, url, **kwargs):
        headers = dict(kwargs.pop("headers", None) or {})
        login_requested = headers.pop("X-Test-Login", "")
        app = self._transport.app
        if login_requested and not self.cookies.get("gaoji_session"):
            response = await super().request("POST", "/bot-admin/api/v1/auth/login",
                headers={"Origin": str(self.base_url).rstrip("/")},
                json={"username": "kenneth", "password": PASSWORD})
            assert response.status_code == 200, response.text
        headers["Origin"] = str(self.base_url).rstrip("/")
        if self.cookies.get("gaoji_csrf"):
            headers["X-CSRF-Token"] = self.cookies["gaoji_csrf"]
        # Each functional scenario represents a separate user action, not a burst.
        app.state.auth_clock += 61
        response = await super().request(method, url, headers=headers, **kwargs)
        if response.status_code != 202 or "approval_id" not in response.json():
            return response
        identifier = response.json()["approval_id"]
        code = re.search(r"一次性口令：([0-9]{6})", app.state.auth_messages[-1])[1]
        reply = await app.state.mobile.handle_message(qq_id="123456789", bot_id="987654321",
            text=f"确认 {identifier} {code}", private=True)
        assert "已确认" in reply, reply
        assert await app.state.mobile.run_once()
        account = app.state.mobile.store.account_for_qq("123456789")
        result = app.state.mobile.store.get(identifier, account["account_id"])["result"]
        return httpx.Response(result.get("status_code", 200), json=result, request=response.request)

    async def __aexit__(self, *args):
        try:
            return await super().__aexit__(*args)
        finally:
            self._transport.app.state.mobile.store.close()
