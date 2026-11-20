from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import httpx

from .model_catalog import ModelProfile


class LocalModelRuntime:
    """Background readiness cache; chat requests never perform health I/O."""

    def __init__(
        self,
        profile: ModelProfile,
        *,
        interval_seconds: float = 15,
        timeout_seconds: float = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.profile = profile
        self.interval = max(interval_seconds, 1)
        self.timeout = max(timeout_seconds, 0.1)
        self._client = client or httpx.AsyncClient(
            timeout=self.timeout, trust_env=False, follow_redirects=False,
        )
        self._lock = threading.RLock()
        self._ready_generation = 0
        self._checked_monotonic = 0.0
        self._state: dict[str, Any] = {
            "state": "unknown", "ready": False, "checked_at": None,
            "reason_code": "health_pending", "reason": "等待首次健康探测",
            "latency_ms": None,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
            if self._checked_monotonic and time.monotonic() - self._checked_monotonic > self.interval * 3 + self.timeout * 2:
                state.update(state="unknown", ready=False, reason_code="health_stale", reason="健康探测结果已过期，暂不使用千问")
            return {
                **state, "profile": self.profile.name, "model": self.profile.model,
                "probe_interval_seconds": self.interval,
                "ready_generation": self._ready_generation,
            }

    def unavailable_reason(self, profile_name: str) -> tuple[str, str] | None:
        if profile_name != self.profile.name:
            return None
        state = self.snapshot()
        return None if state["ready"] else (state["reason_code"], state["reason"])

    async def probe_once(self) -> None:
        started = time.monotonic()
        state: dict[str, Any] = {"ready": False, "state": "unreachable", "reason_code": "health_unreachable", "reason": "千问推理接口不可达，服务可能未启动或网络异常"}
        try:
            headers = {"Authorization": f"Bearer {self.profile.api_key}"} if self.profile.api_key else {}
            async with asyncio.timeout(self.timeout):
                response = await self._client.get(self.profile.base_url.rstrip("/") + "/models", headers=headers, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
            items = payload.get("data", []) if isinstance(payload, dict) else []
            if not isinstance(items, list):
                raise ValueError("invalid models response")
            if any(isinstance(item, dict) and item.get("id") == self.profile.model for item in items):
                state.update(ready=True, state="ready", reason_code="ready", reason="模型已就绪")
            else:
                state.update(state="loading", reason_code="model_not_loaded", reason="接口在线，但目标模型尚未加载")
        except httpx.HTTPStatusError as exc:
            state.update(state="unavailable", reason_code="health_http_error", reason=f"千问健康接口返回 HTTP {exc.response.status_code}")
        except (TimeoutError, httpx.HTTPError, ValueError, TypeError):
            pass
        state.update(checked_at=int(time.time()), latency_ms=round((time.monotonic() - started) * 1000, 1))
        with self._lock:
            if state["ready"] and not self.snapshot()["ready"]:
                self._ready_generation += 1
            self._state = state
            self._checked_monotonic = time.monotonic()

    async def run_forever(self) -> None:
        while True:
            await self.probe_once()
            await asyncio.sleep(self.interval)

    async def close(self) -> None:
        await self._client.aclose()
