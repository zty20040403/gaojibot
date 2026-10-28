"""Single-flight token refresh with monotonic deadlines and bounded backoff."""
from __future__ import annotations

import asyncio
import math
import random
import time
from collections.abc import Awaitable, Callable

import httpx

from .credentials import Credentials, DEFAULT_API_BASE, validate_api_base
from .errors import QQAPIError
from .http import request_json


class TokenManager:
    def __init__(
        self, credentials: Credentials, base_url: str = DEFAULT_API_BASE, *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
        allow_test_server: bool = False,
        refresh_margin: float = 50.0,
        max_attempts: int = 3,
    ) -> None:
        if not 1 <= max_attempts <= 10 or not 0 <= refresh_margin <= 60:
            raise ValueError("Invalid token retry/refresh policy")
        self.credentials = credentials
        self.base_url = validate_api_base(base_url, allow_test_server=allow_test_server, credentials=credentials)
        self.allow_test_server = allow_test_server
        self._client = client or httpx.AsyncClient(trust_env=False, follow_redirects=False)
        self._owns_client = client is None
        self._clock, self._sleep, self._jitter = clock, sleep, jitter
        self._refresh_margin, self._max_attempts = refresh_margin, max_attempts
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._invalidated_token: str | None = None
        self._expires_at = 0.0
        self._refresh_at = 0.0
        self._retry_at = 0.0
        self._last_error: QQAPIError | None = None

    def __repr__(self) -> str:
        return f"TokenManager(cached={self._token is not None})"

    @property
    def expires_at(self) -> float:
        """Monotonic deadline, never an identity or token value."""
        return self._expires_at

    def invalidate(self, token: str | None = None) -> None:
        """A stale 401 must not invalidate a newer token obtained concurrently."""
        if token is None or token == self._token:
            self._invalidated_token = self._token
            self._refresh_at = 0.0
            self._retry_at = 0.0

    async def get(self) -> str:
        async with self._lock:
            now = self._clock()
            valid = self._token is not None and self._token != self._invalidated_token and now < self._expires_at
            if valid and now < max(self._refresh_at, self._retry_at):
                return self._token
            if now < self._retry_at and self._last_error:
                raise self._last_error
            for attempt in range(self._max_attempts):
                try:
                    requested_at = self._clock()
                    data = await request_json(
                        self._client, "POST", self.base_url + "/app/getAppAccessToken",
                        json={"appId": self.credentials.app_id, "clientSecret": self.credentials.app_secret},
                        stage="token", timeout=15.0,
                        secrets=(self.credentials.app_id, self.credentials.app_secret, self._token or ""),
                    )
                    token = data.get("access_token")
                    try:
                        ttl = float(data["expires_in"])
                    except (KeyError, ValueError, TypeError):
                        ttl = 0.0
                    if (
                        not isinstance(token, str) or not token or len(token) > 16384
                        or any(char.isspace() for char in token) or not math.isfinite(ttl) or ttl <= 0
                        or isinstance(data.get("expires_in"), bool)
                    ):
                        raise QQAPIError("invalid_token_response", accepted=False, stage="token")
                    now = self._clock()
                    expires_at = requested_at + ttl
                    if token == self._token:
                        expires_at = min(expires_at, self._expires_at)
                    if token == self._invalidated_token or expires_at <= now:
                        raise QQAPIError("stale_token_response", accepted=False, stage="token")
                    same = token == self._token
                    self._token, self._expires_at = token, expires_at
                    # Returned old tokens do not get another TTL. Avoid a stampede
                    # if the server still returns one during its overlap period.
                    if same:
                        self._refresh_at = min(expires_at, now + 5.0)
                    else:
                        self._refresh_at = now + max(0.1, (expires_at - now) - min(self._refresh_margin, ttl / 2))
                    self._retry_at, self._last_error = 0.0, None
                    return token
                except QQAPIError as error:
                    # Save a clean copy, not a traceback retaining request locals.
                    self._last_error = QQAPIError(
                        error.category, accepted=False, status=error.status, code=error.code,
                        stage="token", request_id=error.request_id, retry_after=error.retry_after,
                    )
                    if error.category == "rate_limited" and error.retry_after is not None:
                        # Respect server cooldown without holding the token lock
                        # asleep for minutes; later callers share this deadline.
                        self._retry_at = self._clock() + error.retry_after
                        break
                    if error.category in {"authentication", "content_rejected"}:
                        break
                    if attempt + 1 < self._max_attempts:
                        await self._sleep(min(60.0, 2 ** attempt + max(0.0, min(1.0, self._jitter()))))
            self._retry_at = max(self._retry_at, self._clock() + 5.0)
            if self._token and self._token != self._invalidated_token and self._clock() < self._expires_at:
                return self._token
            raise self._last_error from None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
