from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query

from ..conversation_scope import ConversationScope
from .context import AdminRouteContext, authorize


def create_router(context: AdminRouteContext) -> APIRouter:
    router = context.new_router()
    services = context.services
    control_store = context.control_store
    versioned = context.versioned

    @router.get("/api/traces")
    def traces(
        limit: int = Query(default=100, ge=1, le=500),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.turn_journal is None:
            return versioned(
                "traces",
                {"items": [], "configured": False, "available": False},
            )
        try:
            items = services.turn_journal.recent_trace_summaries(limit=limit)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return versioned(
                "traces",
                {
                    "items": [],
                    "configured": True,
                    "available": False,
                    "error": str(exc)[:500],
                },
            )
        return versioned(
            "traces",
            {"items": items, "configured": True, "available": True},
        )

    @router.get("/api/audit")
    def audit_log(
        limit: int = Query(default=200, ge=1, le=500),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        return {
            "api_version": "v1",
            "persistent": control_store.persistent,
            "items": control_store.audit(limit=limit),
            "versions": control_store.versions(),
        }

    @router.get(
        "/api/turn-replay/{scope_kind}/{scope_native_id}/{turn_ordinal}"
    )
    def turn_replay(
        scope_kind: str,
        scope_native_id: str,
        turn_ordinal: int,
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.turn_journal is None:
            raise HTTPException(status_code=503, detail="回合日志不可用")
        if scope_kind not in {"group", "private"} or turn_ordinal <= 0:
            raise HTTPException(status_code=422, detail="回放范围或回合号无效")
        try:
            scope = ConversationScope(
                "onebot-v11",
                scope_kind,  # type: ignore[arg-type]
                scope_native_id,
            )
            steps = services.turn_journal.replay_steps(scope, turn_ordinal)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)[:500]) from None
        if not steps:
            raise HTTPException(status_code=404, detail="当前范围看不到这个回合")
        return {
            "scope_key": scope.key,
            "turn_handle": f"t#{turn_ordinal}",
            "mode": "audit-only",
            "steps": list(steps),
        }

    return router
