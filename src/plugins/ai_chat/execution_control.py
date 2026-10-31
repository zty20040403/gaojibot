"""Execution fences shared by tools, models and delivery workers."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class JobFence:
    job_id: int
    owner: str
    attempt: int
    check: Callable[[], bool]
    lease_generation: int | None = None
    transaction_guard: Callable[[Any], None] | None = None

    def assert_owned(self) -> None:
        if not self.check():
            raise LeaseLost("Worker no longer owns its job lease")


class LeaseLost(BaseException):
    """Must escape ordinary model/tool retry handlers."""


class RouteAuthorityLost(BaseException):
    """A captured source route was frozen or changed; never retry a model call."""


@dataclass(frozen=True, repr=False)
class RouteFence:
    check: Callable[[], bool]
    transaction_guard: Callable[[Any], None] | None = None

    def assert_current(self) -> None:
        try:
            current = self.check()
        except Exception:
            raise RouteAuthorityLost("source_route_unavailable") from None
        if current is not True:
            raise RouteAuthorityLost("source_route_unavailable")


active_job_fence: ContextVar[JobFence | None] = ContextVar("subagent_job_fence", default=None)
active_route_fence: ContextVar[RouteFence | None] = ContextVar("source_route_fence", default=None)
active_task_id: ContextVar[int | None] = ContextVar("subagent_task_id", default=None)
active_model_policy: ContextVar[dict[str, Any]] = ContextVar("subagent_model_policy", default={})


def assert_job_owned(*, transaction_cursor=None) -> None:
    route = active_route_fence.get()
    if route is not None:
        if transaction_cursor is not None and route.transaction_guard is not None:
            route.transaction_guard(transaction_cursor)
        else:
            route.assert_current()
    fence = active_job_fence.get()
    if fence:
        if transaction_cursor is not None and fence.transaction_guard is not None:
            fence.transaction_guard(transaction_cursor)
        else:
            fence.assert_owned()


