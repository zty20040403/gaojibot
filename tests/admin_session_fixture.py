"""Run resource tests through the same password and CSRF checks as the console."""
import time
from dataclasses import replace

import httpx

from src.bot_security.store import SecurityStore
from src.plugins.ai_chat.admin import register_admin as real_register_admin
from tests.postgres_fixture import isolated_postgres_for_test

PASSWORD = "fixture-password-only"


def register_admin(app, services, *, test_case, token="", **kwargs):
    store = SecurityStore(
        isolated_postgres_for_test(test_case), b"fixture-secret-for-test-only-0000"
    )
    test_case.addCleanup(store.close)
    store.bootstrap_local_admin("kenneth", PASSWORD)
    app.state.auth_clock = time.time()
    store.clock = lambda: app.state.auth_clock
    app.state.console_store = store
    real_register_admin(app, replace(services, console_store=store), **kwargs)


class ApprovedClient(httpx.AsyncClient):
    async def request(self, method, url, **kwargs):
        headers = dict(kwargs.pop("headers", None) or {})
        if headers.pop("X-Test-Login", "") and not self.cookies.get("gaoji_session"):
            response = await super().request("POST", "/bot-admin/api/v1/auth/login",
                headers={"Origin": str(self.base_url).rstrip("/")},
                json={"username": "kenneth", "password": PASSWORD})
            assert response.status_code == 200, response.text
        headers["Origin"] = str(self.base_url).rstrip("/")
        if self.cookies.get("gaoji_csrf"):
            headers["X-CSRF-Token"] = self.cookies["gaoji_csrf"]
        self._transport.app.state.auth_clock += 61
        return await super().request(method, url, headers=headers, **kwargs)
