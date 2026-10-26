from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from .context import AdminRouteContext, authorize, mutation_context, mutation_payload
from .contracts import (
    AdminMutationContext,
    CleanupConfirmationRequest,
    MediaReviewRequest,
)


def create_router(context: AdminRouteContext) -> APIRouter:
    router = context.new_router()
    services = context.services
    control_store = context.control_store
    event_broker = context.event_broker
    mutate = context.mutate

    @router.get("/api/stickers")
    def stickers(
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if not callable(services.sticker_inventory):
            return {
                "counts": {},
                "items": [],
                "configured": False,
                "available": False,
            }
        try:
            inventory = services.sticker_inventory()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return {
                "counts": {},
                "items": [],
                "configured": True,
                "available": False,
                "error": str(exc)[:500],
            }
        return {
            **inventory,
            "configured": True,
            "available": True,
        }

    @router.get("/api/media")
    def media(
        limit: int = Query(default=100, ge=1, le=500),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.media_library is None:
            return {
                "counts": {},
                "items": [],
                "jobs": [],
                "vision": {"counts": {}, "jobs": []},
                "configured": False,
                "available": False,
            }
        try:
            snapshot = services.media_library.admin_snapshot(limit=limit)
            vision = (
                services.vision_worker.admin_snapshot(limit=limit)
                if services.vision_worker is not None
                else {"counts": {}, "jobs": []}
            )
            cleanup = (
                services.media_cleanup.preview()
                if services.media_cleanup is not None
                else {
                    "candidate_count": 0,
                    "candidate_bytes": 0,
                    "ordinary_links": 0,
                    "confirmation_token": "",
                    "recent_runs": [],
                }
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return {
                "counts": {},
                "items": [],
                "jobs": [],
                "vision": {"counts": {}, "jobs": []},
                "configured": True,
                "available": False,
                "error": str(exc)[:500],
            }
        return {
            **snapshot,
            "vision": vision,
            "cleanup": cleanup,
            "configured": True,
            "available": True,
            "api_version": "v1",
            "resource": "media",
            "resource_version": control_store.version("media"),
        }

    @router.put("/api/media/{media_id}/review")
    def review_media(
        media_id: int,
        review: MediaReviewRequest,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if media_id <= 0:
            raise HTTPException(status_code=422, detail="媒体编号必须是正整数")
        if review.state not in {"approved", "pending", "rejected"}:
            raise HTTPException(status_code=422, detail="审核状态无效")
        if services.media_library is None or not callable(
            getattr(services.media_library, "set_review_state", None)
        ):
            raise HTTPException(status_code=503, detail="媒体审核服务不可用")
        try:
            result = mutate(
                mutation_info,
                "media",
                action="media.review.set",
                target=f"media#{media_id}",
                before={"requested_state": review.state},
                operation=lambda _version: services.media_library.set_review_state(
                    media_id,
                    review.state,
                ),
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)[:500]) from None
        event_broker.publish("media", "stickers", "overview")
        reviewed = result.value if isinstance(result.value, dict) else {}
        return mutation_payload(result, **reviewed)

    @router.get("/api/sources")
    def sources(
        limit: int = Query(default=100, ge=1, le=500),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.source_store is None:
            return {
                "counts": {},
                "platforms": [],
                "items": [],
                "configured": False,
                "available": False,
            }
        try:
            snapshot = services.source_store.admin_snapshot(limit=limit)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return {
                "counts": {},
                "platforms": [],
                "items": [],
                "configured": True,
                "available": False,
                "error": str(exc)[:500],
            }
        return {
            **snapshot,
            "configured": True,
            "available": True,
        }

    @router.delete("/api/media/legacy-images")
    async def cleanup_legacy_images(
        confirmation: CleanupConfirmationRequest,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.media_cleanup is None:
            raise HTTPException(status_code=503, detail="旧图片清理服务不可用")
        try:
            result = mutate(
                mutation_info,
                "media",
                action="media.legacy-cleanup",
                target="legacy-images",
                operation=lambda _version: services.media_cleanup.apply(
                    confirmation.confirmation_token,
                ),
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)[:500]) from None
        event_broker.publish("media", "overview")
        report = result.value if isinstance(result.value, dict) else {}
        return mutation_payload(result, **report)

    return router
