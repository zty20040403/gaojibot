from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException
from fastapi.routing import APIRoute

from src.bot_security.service import audit_actor

from ..admin_control import (
    AdminControlStore,
    AdminVersionConflict,
    MutationResult,
    parse_expected_version,
)
from ..admin_security import require_admin
from .contracts import AdminMutationContext, AdminServices
from .realtime import AdminEventBroker, AdminRealtimeMonitor


def authorize(authorization: Optional[str] = Header(default=None)) -> None:
    require_admin()


def authorize_management(authorization: Optional[str] = None) -> None:
    authorize(authorization)


def mutation_context(
    if_match: Optional[str] = Header(default=None, alias="If-Match"),
    admin_actor: Optional[str] = Header(default=None, alias="X-Admin-Actor"),
) -> AdminMutationContext:
    try:
        expected_version = parse_expected_version(if_match)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return AdminMutationContext(
        expected_version=expected_version,
        actor=audit_actor(),
    )


def mutation_payload(result: MutationResult, **payload: object) -> dict[str, object]:
    return {
        "ok": True,
        **payload,
        "resource": result.resource_key,
        "resource_version": result.resource_version,
    }


def require_changed(changed: bool, status_code: int, detail: str) -> bool:
    if not changed:
        raise HTTPException(status_code=status_code, detail=detail)
    return True


@dataclass(frozen=True)
class AdminRouteContext:
    services: AdminServices
    prefix: str
    route_class: type[APIRoute]
    control_store: AdminControlStore
    event_broker: AdminEventBroker
    realtime_monitor: AdminRealtimeMonitor

    def new_router(self) -> APIRouter:
        # FastAPI retains the child's route class when including a router.
        return APIRouter(prefix=self.prefix, route_class=self.route_class)

    def mutate(
        self,
        context: AdminMutationContext,
        resource_key: str,
        *,
        action: str,
        target: str = "",
        before: object = None,
        operation: Callable[[int], Any],
    ) -> MutationResult:
        try:
            return self.control_store.mutate(
                resource_key,
                expected_version=context.expected_version,
                actor=context.actor,
                action=action,
                target=target,
                before=before,
                operation=operation,
            )
        except AdminVersionConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "resource_version_conflict",
                    "resource": exc.resource_key,
                    "expected_version": exc.expected,
                    "current_version": exc.current,
                },
            ) from None

    def versioned(self, resource_key: str, payload: dict[str, object]) -> dict[str, object]:
        return {
            **payload,
            "api_version": "v1",
            "resource": resource_key,
            "resource_version": self.control_store.version(resource_key),
        }
