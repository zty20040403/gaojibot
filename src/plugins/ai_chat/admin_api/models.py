from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from ..local_model import LocalModelControlError
from .context import AdminRouteContext, authorize, mutation_context, mutation_payload
from .contracts import (
    AdminMutationContext,
    GroupEnabledRequest,
    LocalModelControlRequest,
    ModelSelectionRequest,
    ReasoningEffortRequest,
)
from .model_views import (
    _admin_model_profile,
    _admin_reasoning_effort,
    _explicit_preference,
    _group_model_overview,
    _preserve_admin_model_selections,
)


def create_router(context: AdminRouteContext) -> APIRouter:
    router = context.new_router()
    services = context.services
    mobile = context.services.mobile_authorization
    event_broker = context.event_broker
    mutate = context.mutate
    versioned = context.versioned

    @router.get("/api/local-model")
    def local_model_status(authorization: Optional[str] = Header(default=None)) -> dict[str, object]:
        authorize(authorization)
        runtime = services.local_model
        if runtime is None:
            return versioned("local-model", {"configured": False, "can_control": False})
        snapshot = runtime.snapshot()
        health = services.llm_gateway.health_snapshot().get(runtime.profile.name, {}) if services.llm_gateway else {}
        selected = getattr(services.settings, "model_simple_chat_profile", "") == runtime.profile.name
        can_control = bool(mobile and snapshot["control_configured"])
        return versioned("local-model", {
            **snapshot, "configured": True, "can_control": can_control,
            "control_reason": "" if can_control else "启停需要管理员账户、QQ 口令授权和 WSL 管理接口凭据",
            "simple_chat_selected": selected,
            "serving_simple_chat": bool(selected and snapshot["ready"] and health.get("status") != "open"),
            "circuit_state": health.get("status", "unknown"),
            "circuit_breaker_enabled": runtime.profile.circuit_breaker_enabled,
            "request_count": health.get("request_count", 0),
            "average_latency_ms": health.get("average_latency_ms"),
            "metrics_since": services.started_at,
        })

    @router.post("/api/local-model/control")
    async def local_model_control(
        body: LocalModelControlRequest,
        authorization: Optional[str] = Header(default=None),
        context: AdminMutationContext = Depends(mutation_context),
    ) -> dict[str, object]:
        authorize(authorization)
        if context.expected_version is None:
            raise HTTPException(status_code=428, detail="启停操作必须携带 If-Match 资源版本")
        runtime = services.local_model
        if runtime is None or not runtime.control_configured:
            raise HTTPException(status_code=503, detail="尚未配置 WSL 千问管理接口")
        try:
            result = await asyncio.to_thread(
                mutate, context, "local-model",
                action="local_model." + body.action, target=runtime.profile.name,
                before={"state": runtime.snapshot()["state"]},
                operation=lambda _version: runtime.control(body.action, str(body.request_id)),
            )
        except LocalModelControlError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None
        finally:
            event_broker.publish("local-model", "overview", "audit")
        return mutation_payload(result, **result.value)

    @router.get("/api/usage")
    def usage(
        days: int = Query(default=14, ge=1, le=365),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.usage_store is None:
            return {"items": [], "configured": False}
        return {
            "items": services.usage_store.daily_summary(days=days),
            "configured": True,
        }

    @router.get("/api/group-models")
    def group_models(
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        return versioned(
            "groups",
            _group_model_overview(
                services.model_catalog,
                services.model_preferences,
                services.reasoning_preferences,
                services.settings,
                services.message_ledger,
                services.user_profiles,
            ),
        )

    @router.put("/api/group-models/{group_id}/default")
    async def set_group_model(
        group_id: int,
        selection: ModelSelectionRequest,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if group_id <= 0:
            raise HTTPException(status_code=422, detail="群号必须是正整数")
        if services.model_preferences is None:
            raise HTTPException(status_code=503, detail="模型偏好存储不可用")
        profile = _admin_model_profile(services.model_catalog, selection.profile)

        def update_group_model(_version: int) -> dict[str, object]:
            _preserve_admin_model_selections(
                services.model_catalog,
                services.model_preferences,
                services.settings,
                group_id,
            )
            if profile is None:
                services.model_preferences.clear_group_default(group_id)
            else:
                services.model_preferences.set_group_default(group_id, profile.name)
            return {"profile": profile.name if profile is not None else None}

        result = mutate(
            mutation_info,
            "groups",
            action="group.model.set",
            target=str(group_id),
            before={"profile": services.model_preferences.get_group_default(group_id)},
            operation=update_group_model,
        )
        event_broker.publish("groups", "overview")
        return mutation_payload(
            result,
            scope="group",
            group_id=group_id,
            profile=profile.name if profile is not None else None,
        )

    @router.put("/api/group-models/{group_id}/enabled")
    async def set_group_enabled(
        group_id: int,
        selection: GroupEnabledRequest,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if group_id <= 0:
            raise HTTPException(status_code=422, detail="群号必须是正整数")
        if services.model_preferences is None:
            raise HTTPException(status_code=503, detail="群状态存储不可用")
        result = mutate(
            mutation_info,
            "groups",
            action="group.enabled.set",
            target=str(group_id),
            before={
                "enabled": services.model_preferences.get_group_enabled_override(
                    group_id
                )
            },
            operation=lambda _version: (
                services.model_preferences.set_group_enabled(
                    group_id,
                    selection.enabled,
                )
                or {"enabled": selection.enabled}
            ),
        )
        event_broker.publish("groups", "overview")
        return mutation_payload(
            result,
            group_id=group_id,
            enabled=selection.enabled,
        )

    @router.put("/api/group-models/{group_id}/vision-auto-describe")
    async def set_group_vision_auto_describe(
        group_id: int,
        selection: GroupEnabledRequest,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if group_id <= 0:
            raise HTTPException(status_code=422, detail="群号必须是正整数")
        if services.model_preferences is None:
            raise HTTPException(status_code=503, detail="群状态存储不可用")
        result = mutate(
            mutation_info,
            "groups",
            action="group.vision-auto-describe.set",
            target=str(group_id),
            before={
                "enabled": (
                    services.model_preferences.get_group_vision_auto_describe_override(
                        group_id
                    )
                )
            },
            operation=lambda _version: (
                services.model_preferences.set_group_vision_auto_describe(
                    group_id,
                    selection.enabled,
                )
                or {"vision_auto_describe": selection.enabled}
            ),
        )
        event_broker.publish("groups", "media", "overview")
        return mutation_payload(
            result,
            group_id=group_id,
            vision_auto_describe=selection.enabled,
        )

    @router.put("/api/group-models/{group_id}/users/{user_id}")
    async def set_group_user_model(
        group_id: int,
        user_id: int,
        selection: ModelSelectionRequest,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if group_id <= 0 or user_id <= 0:
            raise HTTPException(status_code=422, detail="群号和 QQ 号必须是正整数")
        if services.model_preferences is None:
            raise HTTPException(status_code=503, detail="模型偏好存储不可用")
        profile = _admin_model_profile(services.model_catalog, selection.profile)
        conversation_id = f"group:{group_id}:user:{user_id}"

        def update_user_model(_version: int) -> dict[str, object]:
            if profile is None:
                services.model_preferences.clear(conversation_id)
            else:
                services.model_preferences.set(conversation_id, profile.name)
            return {"profile": profile.name if profile is not None else None}

        result = mutate(
            mutation_info,
            "groups",
            action="group.user-model.set",
            target=f"{group_id}:{user_id}",
            before={
                "profile": services.model_preferences.get_explicit(conversation_id)
                if callable(getattr(services.model_preferences, "get_explicit", None))
                else dict(services.model_preferences.items()).get(conversation_id)
            },
            operation=update_user_model,
        )
        event_broker.publish("groups", "overview")
        return mutation_payload(
            result,
            scope="group_user",
            group_id=group_id,
            user_id=user_id,
            profile=profile.name if profile is not None else None,
        )

    @router.put("/api/group-models/{group_id}/reasoning-effort")
    async def set_group_reasoning_effort(
        group_id: int,
        selection: ReasoningEffortRequest,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if group_id <= 0:
            raise HTTPException(status_code=422, detail="群号必须是正整数")
        preferences = services.reasoning_preferences
        if preferences is None:
            raise HTTPException(status_code=503, detail="推理强度存储不可用")
        effort = _admin_reasoning_effort(selection.effort)

        def update_group_effort(_version: int) -> dict[str, object]:
            if effort is None:
                preferences.clear_group_member_default(group_id)
            else:
                preferences.set_group_member_default(group_id, effort)
            return {"effort": effort}

        before = preferences.get_group_member_default(group_id)
        result = mutate(
            mutation_info,
            "groups",
            action="group.reasoning-effort.set",
            target=str(group_id),
            before={"effort": before},
            operation=update_group_effort,
        )
        event_broker.publish("groups", "overview")
        return mutation_payload(
            result,
            scope="group_members",
            group_id=group_id,
            effort=effort,
        )

    @router.put(
        "/api/group-models/{group_id}/users/{user_id}/reasoning-effort"
    )
    async def set_group_user_reasoning_effort(
        group_id: int,
        user_id: int,
        selection: ReasoningEffortRequest,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if group_id <= 0 or user_id <= 0:
            raise HTTPException(status_code=422, detail="群号和 QQ 号必须是正整数")
        preferences = services.reasoning_preferences
        if preferences is None:
            raise HTTPException(status_code=503, detail="推理强度存储不可用")
        effort = _admin_reasoning_effort(selection.effort)
        conversation_id = f"group:{group_id}:user:{user_id}"

        def update_user_effort(_version: int) -> dict[str, object]:
            if effort is None:
                preferences.clear(conversation_id)
            else:
                preferences.set(conversation_id, effort)
            return {"effort": effort}

        result = mutate(
            mutation_info,
            "groups",
            action="group.user-reasoning-effort.set",
            target=f"{group_id}:{user_id}",
            before={
                "effort": _explicit_preference(preferences, conversation_id)
            },
            operation=update_user_effort,
        )
        event_broker.publish("groups", "overview")
        return mutation_payload(
            result,
            scope="group_user",
            group_id=group_id,
            user_id=user_id,
            effort=effort,
        )

    return router
