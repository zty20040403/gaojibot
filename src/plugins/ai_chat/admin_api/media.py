from __future__ import annotations

from typing import Optional
import time

from ..image_captions import CaptionSettings
from ..admin_control import AdminVersionConflict
from ..conversation_scope import ConversationScope

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from .context import AdminRouteContext, authorize, mutation_context, mutation_payload
from .contracts import (
    AdminMutationContext,
    CleanupConfirmationRequest,
    MediaReviewRequest,
    ImageCaptionSettingsRequest,
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
            captions = CaptionSettings(services.media_library.database).versioned_snapshot(int(time.time()))
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
            "caption_settings": captions['items'],
            "caption_resource_version": captions['resource_version'],
            "captions_available": services.vision_worker is not None,
            "configured": True,
            "available": True,
            "api_version": "v1",
            "resource": "media",
            "resource_version": control_store.version("media"),
        }

    @router.put('/api/media/captions/settings')
    def caption_settings(
        selection: ImageCaptionSettingsRequest,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.media_library is None or not control_store.persistent or selection.enabled and services.vision_worker is None:
            raise HTTPException(status_code=503, detail='图片说明服务未配置')
        if mutation_info.expected_version is None:
            raise HTTPException(428,'请刷新图片说明设置，再提交带 If-Match 的操作')
        store = CaptionSettings(services.media_library.database)
        try:
            scope = ConversationScope.from_key(selection.scope_key)
            if scope.account is None or scope.platform!='qq-official':raise ValueError('caption_scope_unavailable')
            with store.transaction() as connection:
                cursor = connection.cursor()
                try:
                    cursor.execute('SELECT account_key FROM qq_official_accounts WHERE account_key=? FOR UPDATE',(scope.account.key,))
                    def operation():
                        row = cursor.execute('SELECT image_captions,image_daily_limit FROM conversation_settings WHERE scope_key=?',(scope.key,)).fetchone()
                        before = dict(row) if row else {'image_captions':False,'image_daily_limit':200}
                        return before,store.set(scope.key,enabled=selection.enabled,daily_limit=selection.daily_limit,transaction_cursor=cursor)
                    result = control_store.mutate_transaction(cursor,'captions',expected_version=mutation_info.expected_version,
                        actor=mutation_info.actor,action='media.captions.set',target=scope.key,operation=operation)
                finally:
                    cursor.close()
        except AdminVersionConflict as error:
            raise HTTPException(409,{'code':'resource_version_conflict','resource':error.resource_key,
                'expected_version':error.expected,'current_version':error.current}) from None
        except (TypeError,ValueError):
            raise HTTPException(409,'图片说明会话或设置无效') from None
        except Exception:
            raise HTTPException(503,'图片说明设置未能提交，请刷新后重试') from None
        event_broker.publish('captions','media')
        return mutation_payload(result, **(result.value if isinstance(result.value, dict) else {}))

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
