from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from types import SimpleNamespace
from typing import Any, Callable, Protocol

import httpx
from openai import APIConnectionError, APIError, APIStatusError, AsyncOpenAI, RateLimitError
from src.llm_options import anthropic_messages_url, chat_options, thinking_body

from .model_catalog import ModelCatalog, ModelProfile
from .local_model import LocalModelRuntime
from .observability import telemetry


_logger = logging.getLogger(__name__)
_profile_scope: ContextVar[frozenset[str] | None] = ContextVar("completion_profile_scope", default=None)


@contextmanager
def completion_profile_scope(names: frozenset[str]):
    """Nested callers may narrow routing, never widen their parent's scope."""
    parent = _profile_scope.get()
    token = _profile_scope.set(names if parent is None else names & parent)
    try:
        yield
    finally:
        _profile_scope.reset(token)


class LLMError(RuntimeError):
    pass


class LLMConfigError(LLMError):
    pass


class LLMRateLimitError(LLMError):
    pass


class LLMConnectionError(LLMError):
    pass


class LLMProviderError(LLMError):
    def __init__(self, provider: str, status_code: int, detail: str = "") -> None:
        self.provider = provider
        self.status_code = int(status_code)
        self.detail = detail
        message = f"{provider} API error: HTTP {self.status_code}"
        if detail:
            message += f" ({detail})"
        super().__init__(message)


class LLMUnavailableError(LLMError):
    pass


class LLMEmptyResponseError(LLMError):
    pass


class LLMFailureKind(str, Enum):
    NETWORK = "network"
    RATE_LIMIT = "rate_limit"
    AUTH = "auth"
    BILLING = "billing"
    MODEL_UNAVAILABLE = "model_unavailable"
    PROVIDER = "provider"
    INVALID_REQUEST = "invalid_request"
    REFUSAL = "refusal"
    CONFIG = "config"


@dataclass(frozen=True)
class CompletionResult:
    response: Any
    profile: ModelProfile
    fallback_count: int = 0
    routing: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ClassifiedFailure:
    kind: LLMFailureKind
    retryable: bool
    open_immediately: bool = False
    long_cooldown: bool = False


@dataclass
class ModelCircuit:
    circuit_breaker_enabled: bool = True
    consecutive_failures: int = 0
    total_failures: int = 0
    total_successes: int = 0
    fallback_uses: int = 0
    opened_until: float = 0.0
    probe_in_flight: bool = False
    last_error_kind: str = ""
    last_error: str = ""
    last_failure_at: int = 0
    last_success_at: int = 0
    request_count: int = 0
    request_seconds: float = 0.0


class ModelHealthRegistry:
    def __init__(
        self,
        *,
        failure_threshold: int = 2,
        cooldown_seconds: float = 120.0,
        long_cooldown_seconds: float = 3600.0,
    ) -> None:
        self.failure_threshold = max(int(failure_threshold), 1)
        self.cooldown_seconds = max(float(cooldown_seconds), 1.0)
        self.long_cooldown_seconds = max(float(long_cooldown_seconds), 1.0)
        self._circuits: dict[str, ModelCircuit] = {}

    def acquire(
        self,
        profile_name: str,
        *,
        now: float | None = None,
        circuit_breaker_enabled: bool = True,
    ) -> bool:
        current = time.monotonic() if now is None else now
        circuit = self._circuits.setdefault(profile_name, ModelCircuit())
        circuit.circuit_breaker_enabled = circuit_breaker_enabled
        if not circuit_breaker_enabled:
            circuit.opened_until = 0.0
            circuit.probe_in_flight = False
            return True
        if circuit.opened_until <= 0:
            return True
        if circuit.opened_until > current:
            return False
        if circuit.probe_in_flight:
            return False
        circuit.probe_in_flight = True
        return True

    def record_success(
        self,
        profile_name: str,
        *,
        used_as_fallback: bool = False,
    ) -> None:
        circuit = self._circuits.setdefault(profile_name, ModelCircuit())
        circuit.consecutive_failures = 0
        circuit.opened_until = 0.0
        circuit.probe_in_flight = False
        circuit.last_error_kind = ""
        circuit.last_error = ""
        circuit.last_success_at = int(time.time())
        circuit.total_successes += 1
        circuit.fallback_uses += int(used_as_fallback)

    def record_latency(self, profile_name: str, seconds: float) -> None:
        circuit = self._circuits.setdefault(profile_name, ModelCircuit())
        circuit.request_count += 1
        circuit.request_seconds += max(seconds, 0.0)

    def release(self, profile_name: str) -> None:
        circuit = self._circuits.get(profile_name)
        if circuit is not None:
            circuit.probe_in_flight = False

    def readiness_recovered(self, profile_name: str) -> None:
        circuit = self._circuits.get(profile_name)
        if circuit is not None and circuit.last_error_kind in {"network", "model_unavailable"}:
            circuit.opened_until = 0
            circuit.consecutive_failures = 0
            circuit.probe_in_flight = False

    def record_failure(
        self,
        profile_name: str,
        failure: ClassifiedFailure,
        exc: BaseException,
    ) -> None:
        circuit = self._circuits.setdefault(profile_name, ModelCircuit())
        circuit.probe_in_flight = False
        circuit.total_failures += 1
        circuit.last_error_kind = failure.kind.value
        circuit.last_error = _safe_error_text(exc)
        circuit.last_failure_at = int(time.time())
        if not failure.retryable:
            return
        circuit.consecutive_failures += 1
        should_open = (
            failure.open_immediately
            or circuit.consecutive_failures >= self.failure_threshold
        )
        if should_open and circuit.circuit_breaker_enabled:
            duration = (
                self.long_cooldown_seconds
                if failure.long_cooldown
                else self.cooldown_seconds
            )
            circuit.opened_until = time.monotonic() + duration

    def snapshot(self, profile_names: list[str]) -> dict[str, dict[str, object]]:
        monotonic_now = time.monotonic()
        result: dict[str, dict[str, object]] = {}
        for name in profile_names:
            circuit = self._circuits.get(name)
            if circuit is None:
                result[name] = {
                    "status": "unknown",
                    "consecutive_failures": 0,
                    "retry_after_seconds": 0,
                }
                continue
            retry_after = max(int(circuit.opened_until - monotonic_now), 0)
            if circuit.opened_until > monotonic_now:
                status = "open"
            elif circuit.opened_until > 0:
                status = "half_open"
            elif circuit.consecutive_failures:
                status = "degraded"
            else:
                status = "healthy"
            result[name] = {
                "status": status,
                "circuit_breaker_enabled": circuit.circuit_breaker_enabled,
                "consecutive_failures": circuit.consecutive_failures,
                "total_failures": circuit.total_failures,
                "total_successes": circuit.total_successes,
                "fallback_uses": circuit.fallback_uses,
                "retry_after_seconds": retry_after,
                "last_error_kind": circuit.last_error_kind,
                "last_error": circuit.last_error,
                "last_failure_at": circuit.last_failure_at,
                "last_success_at": circuit.last_success_at,
                "request_count": circuit.request_count,
                "average_latency_ms": round(circuit.request_seconds * 1000 / circuit.request_count, 1) if circuit.request_count else None,
            }
        return result


def _completion_error(profile: ModelProfile, exc: Exception) -> Exception:
    if isinstance(exc, RateLimitError):
        return LLMRateLimitError(f"{profile.provider} rate limit reached")
    if isinstance(exc, (APIConnectionError, httpx.TransportError)):
        return LLMConnectionError(f"could not read response from {profile.provider}")
    if isinstance(exc, APIStatusError):
        return LLMProviderError(profile.provider, exc.status_code, _openai_error_detail(exc))
    if isinstance(exc, APIError):
        return LLMProviderError(profile.provider, 502, "invalid streaming response")
    return exc


async def _close_completion_stream(stream: Any) -> None:
    close = getattr(stream, "close", None) or getattr(stream, "aclose", None)
    if callable(close):
        try:
            await close()
        except Exception:
            _logger.warning("Could not close model stream transport", exc_info=True)


class _CompletionStream:
    """Keep the request alive until consumption finishes or the caller closes it."""

    def __init__(
        self,
        stream: Any,
        profile: ModelProfile,
        finish: Callable[[BaseException | None], None],
    ) -> None:
        self._stream = stream
        self._iterator = stream.__aiter__()
        self._profile = profile
        self._finish = finish
        self._finished = False
        self._closed = False
        self._saw_finish = False
        self._first: deque[Any] = deque()

    async def prime(self) -> None:
        # Fail before handing any chunks to the caller so the gateway can fall back.
        try:
            while True:
                chunk = await self._next()
                self._first.append(chunk)
                for choice in (getattr(chunk, "choices", None) or []):
                    delta = getattr(choice, "delta", None)
                    if getattr(choice, "finish_reason", None) is not None or any(
                        getattr(delta, name, None)
                        for name in ("content", "reasoning_content", "tool_calls", "refusal")
                    ):
                        return
        except StopAsyncIteration:
            raise LLMConnectionError("Model stream returned no chunks") from None

    async def _next(self) -> Any:
        try:
            chunk = await self._iterator.__anext__()
            self._saw_finish = self._saw_finish or any(
                getattr(choice, "finish_reason", None) is not None
                for choice in (getattr(chunk, "choices", None) or [])
            )
            return chunk
        except Exception as exc:
            normalized = _completion_error(self._profile, exc)
            if normalized is exc:
                raise
            raise normalized from exc

    def __aiter__(self) -> _CompletionStream:
        return self

    def _complete(self, exc: BaseException | None) -> None:
        if not self._finished:
            self._finished = True
            self._finish(exc)

    async def __anext__(self) -> Any:
        if self._finished:
            raise StopAsyncIteration
        if self._first:
            return self._first.popleft()
        try:
            return await self._next()
        except StopAsyncIteration:
            if not self._saw_finish:
                exc = LLMConnectionError("Model stream ended without a finish reason")
                self._complete(exc)
                await self.close()
                raise exc from None
            self._complete(None)
            await self.close()
            raise
        except BaseException as exc:
            self._complete(exc)
            await self.close()
            raise

    async def close(self) -> None:
        try:
            self._complete(asyncio.CancelledError())
        finally:
            if not self._closed:
                self._closed = True
                await _close_completion_stream(self._stream)


class CompletionProvider(Protocol):
    async def create_completion(
        self,
        profile: ModelProfile,
        **kwargs: Any,
    ) -> Any: ...

    async def list_models(self, profile: ModelProfile) -> list[str]: ...

    async def close(self) -> None: ...


class OpenAIChatProvider:
    def __init__(self) -> None:
        self._clients: dict[str, AsyncOpenAI] = {}

    def _client(self, profile: ModelProfile) -> AsyncOpenAI:
        _require_configured(profile)
        cache_key = profile.client_cache_key
        client = self._clients.get(cache_key)
        if client is None:
            client = AsyncOpenAI(
                api_key=profile.api_key or "local-no-key",
                base_url=profile.base_url,
                timeout=profile.timeout_seconds,
            )
            self._clients[cache_key] = client
        return client

    async def create_completion(
        self,
        profile: ModelProfile,
        **kwargs: Any,
    ) -> Any:
        client = self._client(profile)
        request = {key: value for key, value in kwargs.items() if value is not None}
        if profile.max_output_tokens > 0 and "max_tokens" not in request:
            request["max_tokens"] = profile.max_output_tokens
        try:
            return await client.chat.completions.create(**request)
        except RateLimitError as exc:
            raise LLMRateLimitError(
                f"{profile.provider} rate limit reached"
            ) from exc
        except APIConnectionError as exc:
            raise LLMConnectionError(
                f"could not connect to {profile.provider}"
            ) from exc
        except APIStatusError as exc:
            raise LLMProviderError(
                profile.provider,
                exc.status_code,
                _openai_error_detail(exc),
            ) from exc

    async def list_models(self, profile: ModelProfile) -> list[str]:
        client = self._client(profile)
        try:
            response = await client.models.list()
        except RateLimitError as exc:
            raise LLMRateLimitError(
                f"{profile.provider} rate limit reached"
            ) from exc
        except APIConnectionError as exc:
            raise LLMConnectionError(
                f"could not connect to {profile.provider}"
            ) from exc
        except APIStatusError as exc:
            raise LLMProviderError(
                profile.provider,
                exc.status_code,
                _openai_error_detail(exc),
            ) from exc
        return sorted(
            item.id
            for item in response.data
            if isinstance(getattr(item, "id", None), str) and item.id.strip()
        )

    async def close(self) -> None:
        clients = list(self._clients.values())
        self._clients.clear()
        for client in clients:
            await client.close()


class AnthropicMessagesProvider:
    def __init__(self) -> None:
        self._clients: dict[str, httpx.AsyncClient] = {}

    def _client(self, profile: ModelProfile) -> httpx.AsyncClient:
        _require_configured(profile)
        cache_key = profile.client_cache_key
        client = self._clients.get(cache_key)
        if client is None:
            client = httpx.AsyncClient(timeout=profile.timeout_seconds)
            self._clients[cache_key] = client
        return client

    async def create_completion(
        self,
        profile: ModelProfile,
        **kwargs: Any,
    ) -> Any:
        if kwargs.get("stream"):
            raise LLMConfigError(
                f"model profile {profile.name!r} does not enable streaming"
            )
        body = _anthropic_request(profile, kwargs)
        headers = {
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        if profile.api_key:
            headers["x-api-key"] = profile.api_key
        try:
            response = await self._client(profile).post(
                _anthropic_messages_url(profile.base_url),
                headers=headers,
                json=body,
            )
        except httpx.TimeoutException as exc:
            raise LLMConnectionError(
                f"{profile.provider} request timed out"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMConnectionError(
                f"could not connect to {profile.provider}"
            ) from exc
        _raise_for_anthropic_status(profile, response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise LLMProviderError(
                profile.provider,
                response.status_code,
                "invalid JSON response",
            ) from exc
        if not isinstance(payload, dict):
            raise LLMProviderError(
                profile.provider,
                response.status_code,
                "invalid response object",
            )
        return _anthropic_response(payload)

    async def list_models(self, profile: ModelProfile) -> list[str]:
        raise LLMConfigError(
            f"model listing is not enabled for profile {profile.name!r}"
        )

    async def close(self) -> None:
        clients = list(self._clients.values())
        self._clients.clear()
        for client in clients:
            await client.aclose()


class LLMGateway:
    def __init__(
        self,
        providers: dict[str, CompletionProvider] | None = None,
        *,
        catalog: ModelCatalog | None = None,
        fallback_enabled: bool = True,
        failure_threshold: int = 2,
        cooldown_seconds: float = 120.0,
        long_cooldown_seconds: float = 3600.0,
        local_model: LocalModelRuntime | None = None,
    ) -> None:
        self._providers: dict[str, CompletionProvider] = (
            {
                "openai-chat": OpenAIChatProvider(),
                "anthropic-messages": AnthropicMessagesProvider(),
            }
            if providers is None
            else dict(providers)
        )
        self._catalog = catalog
        self.local_model = local_model
        self._ready_generation = 0
        self._fallback_enabled = bool(fallback_enabled)
        self.health = ModelHealthRegistry(
            failure_threshold=failure_threshold,
            cooldown_seconds=cooldown_seconds,
            long_cooldown_seconds=long_cooldown_seconds,
        )
        self._closed = False

    async def create_completion(
        self,
        profile: ModelProfile,
        **kwargs: Any,
    ) -> Any:
        result = await self.create_completion_with_profile(profile, **kwargs)
        return result.response

    async def create_completion_with_profile(
        self,
        profile: ModelProfile,
        *,
        route_sink: Callable[[dict[str, Any]], None] | None = None,
        excluded_profiles: frozenset[str] = frozenset(),
        **kwargs: Any,
    ) -> CompletionResult:
        candidates = self._completion_candidates(profile, kwargs)
        attempted = 0
        last_error: BaseException | None = None
        outcomes: list[dict[str, Any]] = []

        def finish_route(
            actual: ModelProfile | None, *, publish: bool = True,
        ) -> dict[str, Any]:
            first = outcomes[0] if outcomes else {}
            routing = {
                "requested_profile": profile.name, "requested_model": profile.model,
                "actual_profile": actual.name if actual else "",
                "actual_model": actual.model if actual else "",
                "reason_code": first.get("reason_code", "direct"),
                "reason": first.get("reason", "直接使用首选模型"),
                "fallback": actual is not None and actual.name != profile.name,
                "outcomes": list(outcomes), "created_at": int(time.time()),
            }
            if publish and route_sink is not None:
                route_sink(routing)
            return routing

        for candidate in candidates:
            if candidate.name in excluded_profiles:
                outcomes.append({
                    "profile": candidate.name,
                    "status": "skipped",
                    "reason_code": "empty_response",
                    "reason": "本轮该模型未生成正文，改用备用模型",
                })
                continue
            if self.local_model is not None:
                unavailable = self.local_model.unavailable_reason(candidate.name)
                if unavailable is not None:
                    code, reason = unavailable
                    outcomes.append({"profile": candidate.name, "status": "skipped", "reason_code": code, "reason": reason})
                    telemetry.observe_model(requested_profile=profile.name, actual_profile=candidate.name, provider=candidate.provider_identity, status="health_skipped", duration=0)
                    continue
                if candidate.name == self.local_model.profile.name:
                    generation = self.local_model.snapshot()["ready_generation"]
                    if generation != self._ready_generation:
                        self.health.readiness_recovered(candidate.name)
                        self._ready_generation = generation
            if not self.health.acquire(
                candidate.name,
                circuit_breaker_enabled=candidate.circuit_breaker_enabled,
            ):
                outcomes.append({"profile": candidate.name, "status": "skipped", "reason_code": "circuit_open", "reason": "模型熔断冷却中"})
                telemetry.observe_model(
                    requested_profile=profile.name,
                    actual_profile=candidate.name,
                    provider=candidate.provider_identity,
                    status="circuit_open",
                    duration=0.0,
                )
                continue
            attempted += 1
            request_started = time.monotonic()
            managed_stream = False
            raw_stream = None
            routing: dict[str, Any] = {}
            tool_choice_compatibility_applied = False

            def finish_stream(exc: BaseException | None) -> None:
                duration = time.monotonic() - request_started
                try:
                    if exc is None:
                        status = "succeeded"
                        if tool_choice_compatibility_applied:
                            code = "tool_choice_compatibility"
                            reason = "模型不支持指定工具，已自动改用工具自动选择"
                        else:
                            code, reason = "direct", "模型响应接收完成"
                        self.health.record_success(
                            candidate.name, used_as_fallback=candidate.name != profile.name,
                        )
                    elif isinstance(exc, (LLMError, asyncio.TimeoutError, httpx.TimeoutException)):
                        failure = classify_llm_failure(exc)
                        self.health.record_failure(candidate.name, failure, exc)
                        status = code = failure.kind.value
                        reason = "模型响应流失败：" + code
                    else:
                        status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "unexpected_error"
                        code, reason = status, "模型响应流被中断"
                    outcomes[-1].update(
                        status="succeeded" if exc is None else "failed",
                        reason_code=code, reason=reason,
                    )
                    telemetry.observe_model(
                        requested_profile=profile.name, actual_profile=candidate.name,
                        provider=candidate.provider_identity, status=status, duration=duration,
                    )
                    routing.update(finish_route(candidate if exc is None else None))
                finally:
                    self.health.release(candidate.name)
                    self.health.record_latency(candidate.name, duration)

            try:
                request = _request_for_profile(candidate, kwargs)
                with telemetry.stage("model.connect" if request.get("stream") else "model.request"):
                    try:
                        response = await self._create_single(candidate, **request)
                    except LLMProviderError as exc:
                        if not _should_retry_tool_choice_as_auto(request, exc):
                            raise
                        request["tool_choice"] = "auto"
                        tool_choice_compatibility_applied = True
                        _logger.warning(
                            "Model profile %s rejected forced tool_choice; retrying once with auto",
                            candidate.name,
                        )
                        response = await self._create_single(candidate, **request)
                    if request.get("stream"):
                        raw_stream = response
                        response = _CompletionStream(raw_stream, candidate, finish_stream)
                        await response.prime()
                if request.get("stream"):
                    outcomes.append(
                        {
                            "profile": candidate.name,
                            "status": "streaming",
                            "reason_code": (
                                "tool_choice_compatibility"
                                if tool_choice_compatibility_applied
                                else "direct"
                            ),
                            "reason": (
                                "模型不支持指定工具，已自动改用工具自动选择"
                                if tool_choice_compatibility_applied
                                else "正在接收模型响应"
                            ),
                        }
                    )
                    routing.update(finish_route(candidate, publish=False))
                    managed_stream = True
                    return CompletionResult(
                        response=response, profile=candidate,
                        fallback_count=max(attempted - 1, 1) if candidate.name != profile.name else 0,
                        routing=routing,
                    )
            except (LLMError, asyncio.TimeoutError, httpx.TimeoutException) as exc:
                failure = classify_llm_failure(exc)
                safe_error = _safe_error_text(exc)
                reason = "模型请求失败：" + failure.kind.value
                if safe_error:
                    reason += "；" + safe_error
                outcomes.append({"profile": candidate.name, "status": "failed", "reason_code": failure.kind.value, "reason": reason})
                telemetry.observe_model(
                    requested_profile=profile.name,
                    actual_profile=candidate.name,
                    provider=candidate.provider_identity,
                    status=failure.kind.value,
                    duration=time.monotonic() - request_started,
                )
                self.health.record_failure(candidate.name, failure, exc)
                last_error = exc
                _logger.warning(
                    "Model profile %s failed (%s); fallback=%s; detail=%s",
                    candidate.name,
                    failure.kind.value,
                    failure.retryable and self._fallback_enabled,
                    safe_error or "unavailable",
                )
                if not failure.retryable or not self._fallback_enabled:
                    finish_route(None)
                    raise
                continue
            except BaseException:
                outcomes.append({"profile": candidate.name, "status": "failed", "reason_code": "interrupted", "reason": "模型请求被中断"})
                finish_route(None)
                telemetry.observe_model(
                    requested_profile=profile.name,
                    actual_profile=candidate.name,
                    provider=candidate.provider_identity,
                    status="unexpected_error",
                    duration=time.monotonic() - request_started,
                )
                raise
            finally:
                if not managed_stream:
                    self.health.release(candidate.name)
                    self.health.record_latency(candidate.name, time.monotonic() - request_started)
                    if raw_stream is not None:
                        await _close_completion_stream(raw_stream)
            outcomes.append(
                {
                    "profile": candidate.name,
                    "status": "succeeded",
                    "reason_code": (
                        "tool_choice_compatibility"
                        if tool_choice_compatibility_applied
                        else "direct"
                    ),
                    "reason": (
                        "模型不支持指定工具，已自动改用工具自动选择"
                        if tool_choice_compatibility_applied
                        else "直接使用首选模型"
                    ),
                }
            )
            telemetry.observe_model(
                requested_profile=profile.name,
                actual_profile=candidate.name,
                provider=candidate.provider_identity,
                status="succeeded",
                duration=time.monotonic() - request_started,
            )
            self.health.record_success(
                candidate.name,
                used_as_fallback=candidate.name != profile.name,
            )
            if candidate.name != profile.name:
                _logger.warning(
                    "Model request routed from profile %s to fallback %s",
                    profile.name,
                    candidate.name,
                )
            return CompletionResult(
                response=response,
                profile=candidate,
                fallback_count=max(attempted - 1, 1)
                if candidate.name != profile.name
                else 0,
                routing=finish_route(candidate),
            )
        finish_route(None)
        if last_error is not None:
            raise last_error
        raise LLMUnavailableError(
            f"all compatible fallback profiles for {profile.name!r} are cooling down"
        )

    async def _create_single(
        self,
        profile: ModelProfile,
        **kwargs: Any,
    ) -> Any:
        provider = self._provider(profile)
        if kwargs.get("tools") and not profile.capabilities.tools:
            raise LLMConfigError(
                f"model profile {profile.name!r} does not support tool calls"
            )
        if kwargs.get("stream") and not profile.capabilities.streaming:
            raise LLMConfigError(
                f"model profile {profile.name!r} does not support streaming"
            )
        if kwargs.get("response_format") and not profile.capabilities.json_mode:
            raise LLMConfigError(
                f"model profile {profile.name!r} does not support native JSON mode"
            )
        return await provider.create_completion(profile, **kwargs)

    def health_snapshot(self) -> dict[str, dict[str, object]]:
        names = (
            [profile.name for profile in self._catalog.profiles]
            if self._catalog is not None
            else list(self.health._circuits)
        )
        snapshot = self.health.snapshot(names)
        if self._catalog is not None:
            for profile in self._catalog.profiles:
                snapshot[profile.name]["circuit_breaker_enabled"] = profile.circuit_breaker_enabled
        if self.local_model is not None:
            snapshot.setdefault(self.local_model.profile.name, {})["readiness"] = self.local_model.snapshot()
        return snapshot

    def _completion_candidates(
        self,
        primary: ModelProfile,
        kwargs: dict[str, Any],
    ) -> list[ModelProfile]:
        scope = _profile_scope.get()
        if scope is not None and primary.name not in scope:
            raise LLMConfigError(f"profile {primary.name!r} is outside this task's model policy")
        candidates = [primary]
        if not self._fallback_enabled or self._catalog is None:
            return candidates

        preferred: list[ModelProfile] = []
        for name in primary.fallback_profiles:
            selected = self._catalog.try_resolve(name)
            if selected is not None:
                preferred.append(selected)
        automatic = [
            candidate
            for candidate in self._catalog.profiles
            if candidate.name != primary.name
        ]
        automatic.sort(key=lambda item: item.provider != primary.provider)
        seen = {primary.name}
        for candidate in (*preferred, *automatic):
            if scope is not None and candidate.name not in scope:
                continue
            if candidate.name in seen or not candidate.configured:
                continue
            if not _profile_supports_request(candidate, kwargs):
                continue
            seen.add(candidate.name)
            candidates.append(candidate)
        return candidates

    async def list_models(self, profile: ModelProfile) -> list[str]:
        if not profile.capabilities.model_listing:
            raise LLMConfigError(
                f"model listing is not enabled for profile {profile.name!r}"
            )
        return await self._provider(profile).list_models(profile)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        seen: set[int] = set()
        for provider in self._providers.values():
            if id(provider) in seen:
                continue
            seen.add(id(provider))
            await provider.close()

    def _provider(self, profile: ModelProfile) -> CompletionProvider:
        if self._closed:
            raise LLMConfigError("LLM gateway is closed")
        provider = self._providers.get(profile.protocol)
        if provider is None:
            raise LLMConfigError(
                f"unsupported model protocol: {profile.protocol}"
            )
        return provider


def classify_llm_failure(exc: BaseException) -> ClassifiedFailure:
    if isinstance(exc, LLMRateLimitError):
        return ClassifiedFailure(
            LLMFailureKind.RATE_LIMIT,
            retryable=True,
            open_immediately=True,
        )
    if isinstance(
        exc,
        (LLMConnectionError, asyncio.TimeoutError, httpx.TimeoutException),
    ):
        return ClassifiedFailure(LLMFailureKind.NETWORK, retryable=True)
    if isinstance(exc, LLMConfigError):
        if "missing its API key" in str(exc):
            return ClassifiedFailure(
                LLMFailureKind.CONFIG,
                retryable=True,
                open_immediately=True,
                long_cooldown=True,
            )
        return ClassifiedFailure(LLMFailureKind.CONFIG, retryable=False)
    if isinstance(exc, LLMProviderError):
        status = exc.status_code
        detail = exc.detail.lower()
        if status == 429:
            return ClassifiedFailure(
                LLMFailureKind.RATE_LIMIT,
                retryable=True,
                open_immediately=True,
            )
        if status in {401, 403}:
            return ClassifiedFailure(
                LLMFailureKind.AUTH,
                retryable=True,
                open_immediately=True,
                long_cooldown=True,
            )
        if status == 402:
            return ClassifiedFailure(
                LLMFailureKind.BILLING,
                retryable=True,
                open_immediately=True,
                long_cooldown=True,
            )
        if status == 404:
            return ClassifiedFailure(
                LLMFailureKind.MODEL_UNAVAILABLE,
                retryable=True,
                open_immediately=True,
                long_cooldown=True,
            )
        if status == 400 and any(
            marker in detail
            for marker in (
                "content_filter",
                "content policy",
                "safety policy",
                "moderation",
            )
        ):
            return ClassifiedFailure(LLMFailureKind.REFUSAL, retryable=False)
        if status in {400, 404, 413, 422}:
            return ClassifiedFailure(
                LLMFailureKind.INVALID_REQUEST,
                retryable=False,
            )
        if status in {408, 409, 425} or status >= 500:
            return ClassifiedFailure(LLMFailureKind.PROVIDER, retryable=True)
        return ClassifiedFailure(LLMFailureKind.PROVIDER, retryable=False)
    return ClassifiedFailure(LLMFailureKind.PROVIDER, retryable=False)


def thinking_extra_body(profile: ModelProfile) -> dict[str, object] | None:
    return thinking_body(profile.provider, profile.protocol, profile.thinking)


def _request_for_profile(
    profile: ModelProfile,
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    request = dict(kwargs)
    request["model"] = profile.model
    if profile.temperature is None:
        request.pop("temperature", None)
    else:
        request["temperature"] = profile.temperature
    if profile.protocol == "openai-chat":
        options = chat_options(profile.provider, profile.protocol, profile.thinking, profile.reasoning_effort)
        for key in ("extra_body", "reasoning_effort"):
            if key in options:
                request[key] = options[key]
            else:
                request.pop(key, None)
        if (
            request.get("tools")
            and not profile.capabilities.forced_tool_choice
            and _is_forced_tool_choice(request.get("tool_choice"))
        ):
            request["tool_choice"] = "auto"
    else:
        request.pop("extra_body", None)
        request.pop("reasoning_effort", None)
    return request


def _is_forced_tool_choice(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in {"", "auto", "none"}
    if isinstance(value, dict):
        return str(value.get("type") or "").strip().lower() not in {
            "",
            "auto",
            "none",
        }
    return True


def _should_retry_tool_choice_as_auto(
    request: dict[str, Any],
    exc: LLMProviderError,
) -> bool:
    if not request.get("tools") or not _is_forced_tool_choice(
        request.get("tool_choice")
    ):
        return False
    if exc.status_code not in {400, 422}:
        return False
    detail = exc.detail.lower()
    return any(
        marker in detail
        for marker in (
            "tool_choice_not_supported",
            "named tool choice",
            "specific tool choice",
            "forced tool choice",
            "tool_choice for function",
            "tool choice for function",
        )
    )


def _profile_supports_request(
    profile: ModelProfile,
    kwargs: dict[str, Any],
) -> bool:
    if kwargs.get("tools") and not profile.capabilities.tools:
        return False
    if kwargs.get("stream") and not profile.capabilities.streaming:
        return False
    if kwargs.get("response_format") and not profile.capabilities.json_mode:
        return False
    if _request_contains_images(kwargs.get("messages")):
        return profile.capabilities.vision
    return True


def _request_contains_images(messages: object) -> bool:
    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") in {
                "image",
                "image_url",
            }:
                return True
    return False


def _openai_error_detail(exc: APIStatusError) -> str:
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    if not isinstance(error, dict):
        return ""
    parts = [
        str(error.get(key) or "").strip().replace("\n", " ")
        for key in ("type", "code", "message")
    ]
    return ": ".join(part for part in parts if part)[:300]


def _safe_error_text(exc: BaseException) -> str:
    if isinstance(exc, LLMProviderError):
        text = f"HTTP {exc.status_code}"
        if exc.detail:
            text += f": {exc.detail}"
    else:
        text = str(exc)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[redacted]", text)
    text = re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}",
        "Bearer [redacted]",
        text,
    )
    return " ".join(text.split())[:300]


def _require_configured(profile: ModelProfile) -> None:
    if profile.configured:
        return
    source = profile.api_key_env or "api_key"
    raise LLMConfigError(
        f"model profile {profile.name!r} is missing its API key ({source})"
    )


def _anthropic_request(
    profile: ModelProfile,
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    messages = kwargs.get("messages")
    if not isinstance(messages, list):
        raise LLMConfigError("Anthropic Messages requires a messages list")
    system, converted_messages = _convert_anthropic_messages(messages)
    body: dict[str, Any] = {
        "model": str(kwargs.get("model") or profile.model),
        "max_tokens": profile.max_output_tokens or 4096,
        "messages": converted_messages,
    }
    if system:
        body["system"] = system
    temperature = kwargs.get("temperature", profile.temperature)
    if temperature is not None:
        body["temperature"] = temperature

    tools = kwargs.get("tools")
    if tools:
        body["tools"] = _convert_anthropic_tools(tools)
        tool_choice = _convert_anthropic_tool_choice(
            kwargs.get("tool_choice", "auto")
        )
        body["tool_choice"] = tool_choice
    else:
        tool_choice = {"type": "none"}
    if (
        profile.thinking == "enabled"
        and tool_choice["type"] in {"auto", "none"}
    ):
        body["thinking"] = {"type": "adaptive"}
    return body


def _convert_anthropic_messages(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "")
        content = message.get("content")
        if role == "system":
            text = _content_text(content)
            if text:
                system_parts.append(text)
            continue
        if role == "tool":
            converted.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": str(message.get("tool_call_id") or ""),
                            "content": _content_text(content),
                        }
                    ],
                }
            )
            continue
        if role not in {"user", "assistant"}:
            continue

        if role == "assistant" and message.get("tool_calls"):
            blocks: list[dict[str, Any]] = []
            text = _content_text(content)
            if text:
                blocks.append({"type": "text", "text": text})
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                if not isinstance(function, dict):
                    continue
                raw_arguments = function.get("arguments")
                try:
                    arguments = json.loads(raw_arguments or "{}")
                except (TypeError, json.JSONDecodeError):
                    arguments = {}
                if not isinstance(arguments, dict):
                    arguments = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(call.get("id") or ""),
                        "name": str(function.get("name") or ""),
                        "input": arguments,
                    }
                )
            converted.append({"role": "assistant", "content": blocks})
            continue

        converted.append({"role": role, "content": _content_text(content)})
    return "\n\n".join(system_parts), converted


def _convert_anthropic_tools(tools: object) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        raise LLMConfigError("tools must be a list")
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        converted.append(
            {
                "name": str(function.get("name") or ""),
                "description": str(function.get("description") or ""),
                "input_schema": function.get("parameters")
                if isinstance(function.get("parameters"), dict)
                else {"type": "object", "properties": {}},
            }
        )
    return converted


def _convert_anthropic_tool_choice(choice: object) -> dict[str, Any]:
    if isinstance(choice, str):
        normalized = choice.strip().lower()
        if normalized == "required":
            return {"type": "any"}
        if normalized in {"auto", "none"}:
            return {"type": normalized}
        return {"type": "auto"}
    if isinstance(choice, dict):
        function = choice.get("function")
        if isinstance(function, dict) and function.get("name"):
            return {"type": "tool", "name": str(function["name"])}
    return {"type": "auto"}


def _anthropic_response(payload: dict[str, Any]) -> Any:
    text_parts: list[str] = []
    tool_calls: list[Any] = []
    for block in payload.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            text_parts.append(block["text"])
        elif block.get("type") == "tool_use":
            tool_calls.append(
                SimpleNamespace(
                    id=str(block.get("id") or ""),
                    type="function",
                    function=SimpleNamespace(
                        name=str(block.get("name") or ""),
                        arguments=json.dumps(
                            block.get("input")
                            if isinstance(block.get("input"), dict)
                            else {},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    ),
                )
            )
    raw_usage = payload.get("usage")
    usage_data = raw_usage if isinstance(raw_usage, dict) else {}
    input_tokens = _safe_int(usage_data.get("input_tokens"))
    output_tokens = _safe_int(usage_data.get("output_tokens"))
    usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )
    message = SimpleNamespace(
        role="assistant",
        content="".join(text_parts),
        tool_calls=tool_calls,
    )
    return SimpleNamespace(
        id=payload.get("id"),
        model=payload.get("model"),
        choices=[
            SimpleNamespace(
                index=0,
                message=message,
                finish_reason=payload.get("stop_reason"),
            )
        ],
        usage=usage,
    )


def _anthropic_messages_url(base_url: str) -> str:
    return anthropic_messages_url(base_url)


def _raise_for_anthropic_status(
    profile: ModelProfile,
    response: httpx.Response,
) -> None:
    if response.status_code < 400:
        return
    detail = _anthropic_error_detail(response)
    if response.status_code == 429:
        raise LLMRateLimitError(
            f"{profile.provider} rate limit reached"
        )
    raise LLMProviderError(profile.provider, response.status_code, detail)


def _anthropic_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if not isinstance(error, dict):
        return ""
    error_type = str(error.get("type") or "").strip()
    message = str(error.get("message") or "").strip().replace("\n", " ")
    combined = ": ".join(item for item in (error_type, message) if item)
    return combined[:300]


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


def _safe_int(value: object) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0
