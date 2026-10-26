from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from ..conversation_scope import ConversationScope
from .context import AdminRouteContext, authorize, mutation_context, mutation_payload
from .contracts import AdminMutationContext, ContextFeedbackRequest


def create_router(context: AdminRouteContext) -> APIRouter:
    router = context.new_router()
    services = context.services
    event_broker = context.event_broker
    mutate = context.mutate
    versioned = context.versioned

    @router.get("/api/context-plans")
    def context_plans(
        limit: int = Query(default=100, ge=1, le=500),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.turn_journal is None:
            return {"items": [], "configured": False, "available": False}
        try:
            items = services.turn_journal.recent_context_plans(limit=limit)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return {
                "items": [],
                "configured": True,
                "available": False,
                "error": str(exc)[:500],
            }
        return {"items": items, "configured": True, "available": True}

    @router.get("/api/context-debug")
    def context_debug(
        limit: int = Query(default=100, ge=1, le=500),
        negative_only: bool = Query(default=False),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.turn_journal is None:
            return versioned(
                "context-debug",
                {
                    "items": [],
                    "historian": _historian_snapshot(services.job_store),
                    "configured": False,
                    "available": False,
                },
            )
        try:
            plans = services.turn_journal.recent_context_plans(limit=limit, negative_only=negative_only)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return versioned(
                "context-debug",
                {
                    "items": [],
                    "historian": _historian_snapshot(services.job_store),
                    "configured": True,
                    "available": False,
                    "error": str(exc)[:500],
                },
            )
        return versioned(
            "context-debug",
            {
                "items": [_context_debug_summary(item) for item in plans],
                "historian": _historian_snapshot(services.job_store),
                "configured": True,
                "available": True,
            },
        )

    @router.get("/api/context-debug/{turn_id}")
    def context_debug_detail(
        turn_id: int,
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.turn_journal is None:
            raise HTTPException(status_code=503, detail="上下文日志不可用")
        plans = services.turn_journal.recent_context_plans(turn_id=turn_id, limit=1)
        plan = next(
            (item for item in plans if int(item.get("turn_id") or 0) == turn_id),
            None,
        )
        if plan is None:
            raise HTTPException(status_code=404, detail="上下文决策不存在或已隐藏")
        messages = _context_evidence_messages(services.message_ledger, plan)
        return versioned(
            "context-debug",
            {
                "item": _context_debug_detail(plan, messages),
                "historian": _historian_snapshot(services.job_store),
            },
        )

    @router.put("/api/context-debug/{turn_id}/feedback")
    def update_context_feedback(
        turn_id: int,
        feedback: ContextFeedbackRequest,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.turn_journal is None:
            raise HTTPException(status_code=503, detail="上下文日志不可用")
        verdict = feedback.verdict.strip().casefold()
        if verdict not in {"correct", "off_topic"}:
            raise HTTPException(status_code=422, detail="反馈只能是答对了或答非所问")
        plans = services.turn_journal.recent_context_plans(turn_id=turn_id, limit=1)
        plan = next(
            (item for item in plans if int(item.get("turn_id") or 0) == turn_id),
            None,
        )
        if plan is None:
            raise HTTPException(status_code=404, detail="上下文决策不存在或已隐藏")
        result = mutate(
            mutation_info,
            "context-debug",
            action="context.feedback.set",
            target=f"turn#{turn_id}",
            before=plan.get("feedback"),
            operation=lambda resource_version: (
                services.turn_journal.set_context_feedback(
                    turn_id,
                    verdict=verdict,
                    note=feedback.note,
                    actor=mutation_info.actor,
                    resource_version=resource_version,
                )
            ),
        )
        event_broker.publish("context-debug", "audit")
        return mutation_payload(result, feedback=result.value)

    return router


def _context_debug_summary(plan: dict[str, Any]) -> dict[str, object]:
    decisions = [
        item
        for item in plan.get("recall_candidates", [])
        if isinstance(item, dict)
    ]
    selected = [item for item in decisions if bool(item.get("selected"))]
    candidate_count = sum(
        int(item.get("omitted_count") or 0)
        if item.get("handle") == "audit#omitted"
        else 1
        for item in decisions
    )
    selected_sources = {str(item.get("source") or "") for item in selected}
    budget = plan.get("adaptive_budget")
    budget = budget if isinstance(budget, dict) else {}
    used = budget.get("used")
    used = used if isinstance(used, dict) else {}
    route = plan.get("recall_route")
    route = route if isinstance(route, dict) else {}
    return {
        "turn_id": int(plan.get("turn_id") or 0),
        "turn_handle": str(plan.get("turn_handle") or ""),
        "scope_key": str(plan.get("scope_key") or ""),
        "created_at": int(plan.get("created_at") or 0),
        "status": str(plan.get("status") or ""),
        "model": str(plan.get("model") or ""),
        "profile": str(plan.get("profile") or ""),
        "current_topic": str(
            plan.get("topic_query") or plan.get("objective") or "未识别话题"
        )[:1000],
        "route": str(
            route.get("effective_mode")
            or route.get("mode")
            or "legacy"
        ),
        "route_confidence": float(route.get("confidence") or 0.0),
        "focus_message_id": plan.get("focus_message_id"),
        "confidence": float(plan.get("confidence") or 0.0),
        "selected_candidates": len(selected),
        "candidate_count": candidate_count,
        "context_tokens": int(used.get("total") or 0),
        "memory_usage": {
            "group": "group_memory" in selected_sources,
            "user": "user_memory" in selected_sources,
        },
        "feedback": plan.get("feedback"),
        "votes": plan.get("votes", {"asker": None, "others_up": 0, "others_down": 0}),
    }


def _context_debug_detail(
    plan: dict[str, Any],
    messages: list[dict[str, object]],
) -> dict[str, object]:
    summary = _context_debug_summary(plan)
    decisions = [
        dict(item)
        for item in plan.get("recall_candidates", [])
        if isinstance(item, dict)
    ]
    if not decisions:
        focus_id = int(plan.get("focus_message_id") or 0)
        related = {
            int(item)
            for item in plan.get("related_message_ids", [])
            if str(item).isdigit()
        }
        for item in plan.get("candidates", []):
            if not isinstance(item, dict):
                continue
            message_id = int(item.get("message_id") or 0)
            selected = message_id == focus_id or message_id in related
            decisions.append(
                {
                    "handle": f"msg#{message_id}" if message_id else "candidate",
                    "source": item.get("source") or "reference_resolver",
                    "selected": selected,
                    "raw_score": float(item.get("score") or 0.0),
                    "adjusted_score": float(item.get("score") or 0.0),
                    "decision_codes": [
                        "selected_by_reference_resolver"
                        if selected
                        else "not_selected_by_reference_resolver"
                    ],
                    "reason_codes": item.get("reason_codes") or [],
                    "scores": {
                        "lexical": item.get("lexical_score", 0.0),
                        "semantic": item.get("semantic_score", 0.0),
                        "relation": item.get("relation_score", 0.0),
                        "recency": item.get("recency_score", 0.0),
                    },
                    "evidence_ids": [message_id] if message_id else [],
                }
            )
    decisions.sort(
        key=lambda item: (
            bool(item.get("selected")),
            float(item.get("adjusted_score") or 0.0),
        ),
        reverse=True,
    )
    budget = plan.get("adaptive_budget")
    budget = budget if isinstance(budget, dict) else {}
    selected_sources = {
        str(item.get("source") or "")
        for item in decisions
        if bool(item.get("selected"))
    }
    return {
        **summary,
        "objective": str(plan.get("objective") or ""),
        "current_message_id": int(plan.get("current_message_id") or 0),
        "current_principal_id": plan.get("current_principal_id"),
        "topic_id": plan.get("topic_id"),
        "topic_message_ids": plan.get("topic_message_ids") or [],
        "reason_codes": plan.get("reason_codes") or [],
        "resolver_version": str(plan.get("resolver_version") or ""),
        "context_hash": str(plan.get("context_hash") or ""),
        "recall_route": plan.get("recall_route") or {},
        "token_budget": {
            key: value
            for key, value in budget.items()
            if key != "used"
        },
        "token_usage": budget.get("used") or {},
        "evidence_guard": plan.get("evidence_guard") or {},
        "memory_usage": {
            "group": {
                "used": "group_memory" in selected_sources,
                "handles": [
                    item.get("handle")
                    for item in decisions
                    if item.get("selected")
                    and item.get("source") == "group_memory"
                ],
            },
            "user": {
                "used": "user_memory" in selected_sources,
                "handles": [
                    item.get("handle")
                    for item in decisions
                    if item.get("selected")
                    and item.get("source") == "user_memory"
                ],
            },
        },
        "evidence_messages": messages,
        "candidates": decisions,
        "feedback": plan.get("feedback"),
    }


def _context_evidence_messages(
    ledger: Any,
    plan: dict[str, Any],
) -> list[dict[str, object]]:
    if ledger is None or not callable(getattr(ledger, "visible_messages_by_ids", None)):
        return []
    scope_key = str(plan.get("scope_key") or "")
    parts = scope_key.split(":", 2)
    if len(parts) != 3 or parts[1] not in {"group", "private"}:
        return []
    selected_ids = {
        int(item)
        for item in (
            plan.get("current_message_id"),
            plan.get("focus_message_id"),
            *(plan.get("related_message_ids") or []),
            *(plan.get("topic_message_ids") or []),
        )
        if item is not None and str(item).isdigit() and int(item) > 0
    }
    for candidate in plan.get("recall_candidates", []):
        if not isinstance(candidate, dict) or not candidate.get("selected"):
            continue
        selected_ids.update(
            int(item)
            for item in candidate.get("evidence_ids", [])
            if str(item).isdigit() and int(item) > 0
        )
    try:
        scope = ConversationScope(parts[0], parts[1], parts[2])
        records = ledger.visible_messages_by_ids(scope, tuple(sorted(selected_ids)))
    except (OSError, RuntimeError, TypeError, ValueError):
        return []
    return [
        {
            "message_id": int(item.canonical_message_id),
            "native_message_id": str(item.native_message_id),
            "sender_user_id": str(item.sender_native_user_id),
            "sender_principal_id": item.sender_principal_id,
            "sender_display": str(item.sender_display),
            "direction": str(item.direction),
            "text": str(item.prompt_text)[:4000],
            "occurred_at": int(item.occurred_at),
            "reply_to_message_id": item.reply_to_canonical_message_id,
            "roles": [
                role
                for role, message_id in (
                    ("current", plan.get("current_message_id")),
                    ("focus", plan.get("focus_message_id")),
                )
                if message_id is not None
                and int(message_id) == int(item.canonical_message_id)
            ],
        }
        for item in records
    ]


def _historian_snapshot(job_store: Any) -> dict[str, object]:
    if job_store is None or not callable(getattr(job_store, "recent_summaries", None)):
        return {
            "configured": False,
            "backlog": 0,
            "pending": 0,
            "running": 0,
            "retrying": 0,
            "failed": 0,
            "items": [],
        }
    try:
        jobs = [
            item
            for item in job_store.recent_summaries(limit=500)
            if str(getattr(item, "kind", "")) == "context.historian_capture"
        ]
    except (OSError, RuntimeError, TypeError, ValueError):
        return {
            "configured": True,
            "available": False,
            "backlog": 0,
            "pending": 0,
            "running": 0,
            "retrying": 0,
            "failed": 0,
            "items": [],
        }
    pending = [item for item in jobs if item.status == "pending"]
    running = [item for item in jobs if item.status == "running"]
    retrying = [item for item in pending if int(item.attempts) > 0]
    failed = [item for item in jobs if item.status == "failed"]
    noteworthy = [
        item
        for item in jobs
        if item.status in {"pending", "running", "failed"}
        or int(item.attempts) > 1
    ][:20]
    return {
        "configured": True,
        "available": True,
        "backlog": len(pending) + len(running),
        "pending": len(pending),
        "running": len(running),
        "retrying": len(retrying),
        "failed": len(failed),
        "items": [
            {
                "job_id": int(item.job_id),
                "handle": str(item.handle),
                "scope_key": str(item.scope_key),
                "status": str(item.status),
                "attempts": int(item.attempts),
                "max_attempts": int(item.max_attempts),
                "next_attempt_at": item.next_attempt_at,
                "last_error": str(item.last_error)[:500],
                "updated_at": int(item.updated_at),
            }
            for item in noteworthy
        ],
    }
