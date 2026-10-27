"""Scoped model-call limits, including fallback attempts and streaming receipts.

Text input is conservatively estimated from UTF-8 bytes plus framing overhead;
it is not a promise about an upstream provider's tokenizer or hidden prompts.
Opaque media reserves the configured full input window instead of treating a
short image URL as its token cost. Missing receipts never refund a reservation.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
import inspect
import json
import threading
from typing import Any

from .model_catalog import ModelProfile


class ModelBudgetExceeded(RuntimeError):
    pass


_active: ContextVar[ModelCallBudget | None] = ContextVar(
    "model_call_budget", default=None
)


def active_model_budget() -> ModelCallBudget | None:
    return _active.get()


@contextmanager
def model_budget_scope(budget: ModelCallBudget):
    parent = _active.get()
    if parent is not None and parent is not budget:
        raise ValueError("Nested calls must share the parent model budget")
    token = _active.set(budget)
    try:
        yield budget
    finally:
        _active.reset(token)


def _field(value: object, key: str, default=None):
    return (
        value.get(key, default)
        if isinstance(value, dict)
        else getattr(value, key, default)
    )


def _usage(value: object) -> tuple[int, int, int] | None:
    if value is None:
        return None
    incoming = _field(value, "prompt_tokens", _field(value, "input_tokens"))
    outgoing = _field(value, "completion_tokens", _field(value, "output_tokens"))
    if any(type(number) is not int or number < 0 for number in (incoming, outgoing)):
        return None
    details = _field(
        value, "prompt_tokens_details", _field(value, "input_tokens_details")
    )
    cached = _field(
        details,
        "cached_tokens",
        _field(
            value,
            "prompt_cache_hit_tokens",
            _field(value, "cache_read_input_tokens", 0),
        ),
    )
    cached = min(cached, incoming) if type(cached) is int and cached >= 0 else 0
    return incoming, outgoing, cached


def _opaque_media(value: object) -> bool:
    if isinstance(value, dict):
        if value.get("type") in {
            "image_url",
            "image",
            "input_image",
            "input_audio",
            "audio",
            "video",
            "file",
        }:
            return True
        return any(_opaque_media(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_opaque_media(item) for item in value)
    return False


@dataclass
class CallReservation:
    budget: ModelCallBudget
    provider: str
    model: str
    reserved: int
    counts: tuple[int, int, int] | None = None
    finished: bool = False

    def finish(self, usage: object = None) -> None:
        with self.budget._lock:
            if self.finished:
                return
            self.finished = True
            self.counts = _usage(usage)
            if self.counts is not None:
                self.budget.charged_tokens += sum(self.counts[:2]) - self.reserved


class ModelCallBudget:
    def __init__(
        self,
        max_tokens: int,
        *,
        max_requests: int = 16,
        before_request: Callable[[], Awaitable[None]] | None = None,
    ):
        self.before_request = before_request
        if (
            type(max_tokens) is not int
            or max_tokens < 1
            or not 1 <= max_requests <= 100
        ):
            raise ValueError("Invalid model execution budget")
        self.max_tokens = max_tokens
        self.max_requests = max_requests
        self.charged_tokens = 0
        self.calls: list[CallReservation] = []
        self._lock = threading.RLock()

    @property
    def remaining_tokens(self) -> int:
        with self._lock:
            return max(0, self.max_tokens - self.charged_tokens)

    def prepare(
        self, profile: ModelProfile, request: dict[str, Any]
    ) -> tuple[dict[str, Any], CallReservation]:
        prepared = dict(request)
        if request.get("n", 1) != 1:
            raise ModelBudgetExceeded(
                "Budgeted calls support one completion per request"
            )
        extra = request.get("extra_body") or {}
        if not isinstance(extra, dict) or set(extra) & {
            "messages",
            "tools",
            "max_tokens",
            "max_completion_tokens",
            "n",
        }:
            raise ModelBudgetExceeded(
                "Extra request body cannot override budgeted fields"
            )
        content = {
            key: request[key]
            for key in ("messages", "tools", "response_format")
            if key in request
        }
        encoded = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
        input_bound = len(encoded.encode("utf-8")) + 512
        if _opaque_media(request.get("messages")):
            if profile.max_input_tokens <= 0:
                raise ModelBudgetExceeded(
                    "Media calls need a configured input-token bound"
                )
            input_bound = max(input_bound, profile.max_input_tokens)
        limits = [
            request.get(key)
            for key in ("max_tokens", "max_completion_tokens")
            if request.get(key) is not None
        ]
        if any(type(limit) is not int or limit < 1 for limit in limits):
            raise ModelBudgetExceeded("Completion limit must be a positive integer")
        if profile.max_output_tokens > 0:
            limits.append(profile.max_output_tokens)
        if not limits:
            limits.append(4096)
        with self._lock:
            remaining = self.max_tokens - self.charged_tokens
            if len(self.calls) >= self.max_requests or remaining <= input_bound:
                raise ModelBudgetExceeded(
                    "Routine model budget exhausted before the next request"
                )
            output_limit = min(*limits, remaining - input_bound)
            key = (
                "max_completion_tokens"
                if "max_completion_tokens" in request
                and profile.protocol == "openai-chat"
                else "max_tokens"
            )
            prepared.pop("max_tokens", None)
            prepared.pop("max_completion_tokens", None)
            prepared[key] = output_limit
            if request.get("stream"):
                prepared["stream_options"] = {
                    **(request.get("stream_options") or {}),
                    "include_usage": True,
                }
            reservation = CallReservation(
                self,
                profile.provider_identity,
                profile.model,
                input_bound + output_limit,
            )
            self.charged_tokens += reservation.reserved
            self.calls.append(reservation)
        return prepared, reservation

    def summary(self) -> dict[str, Any]:
        with self._lock:
            counts = [call.counts for call in self.calls if call.counts is not None]
            return {
                "requests": len(self.calls),
                "charged_tokens": self.charged_tokens,
                "input_tokens": sum(item[0] for item in counts),
                "output_tokens": sum(item[1] for item in counts),
                "cached_tokens": sum(item[2] for item in counts),
                "usage_known": all(
                    call.finished and call.counts is not None for call in self.calls
                ),
                "models": sorted({call.model for call in self.calls}),
                "providers": sorted({call.provider for call in self.calls}),
                "over_budget": self.charged_tokens > self.max_tokens,
            }


class BudgetedStream:
    def __init__(self, stream: Any, reservation: CallReservation):
        self.stream = stream
        self.iterator = stream.__aiter__()
        self.reservation = reservation
        self.usage = None
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration
        try:
            item = await self.iterator.__anext__()
        except StopAsyncIteration:
            self.reservation.finish(self.usage)
            raise
        except BaseException:
            self.reservation.finish()
            raise
        usage = _field(item, "usage")
        if usage is not None:
            self.usage = usage
        return item

    async def close(self):
        self.reservation.finish()
        if not self.closed:
            self.closed = True
            close = getattr(self.stream, "aclose", None) or getattr(
                self.stream, "close", None
            )
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result
