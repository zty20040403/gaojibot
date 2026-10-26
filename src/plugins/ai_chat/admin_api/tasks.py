from __future__ import annotations

from dataclasses import asdict
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query

from .context import (
    AdminRouteContext,
    authorize,
    mutation_context,
    mutation_payload,
    require_changed,
)
from .contracts import AdminMutationContext


def create_router(context: AdminRouteContext) -> APIRouter:
    router = context.new_router()
    services = context.services
    event_broker = context.event_broker
    mutate = context.mutate

    @router.get("/api/deliveries")
    def deliveries(
        limit: int = Query(default=100, ge=1, le=500),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.delivery_store is None:
            return {"items": [], "configured": False}
        items = []
        for item in services.delivery_store.recent_summaries(limit=limit):
            payload = asdict(item)
            payload["handle"] = item.handle
            items.append(payload)
        return {"items": items, "configured": True}

    @router.post("/api/deliveries/{delivery_id}/retry")
    async def retry_delivery(
        delivery_id: int,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        result = mutate(
            mutation_info,
            "deliveries",
            action="delivery.retry",
            target=f"delivery#{delivery_id}",
            operation=lambda _version: require_changed(
                bool(
                    services.delivery_store is not None
                    and services.delivery_store.requeue(delivery_id)
                ),
                409,
                "delivery cannot be retried from its current state",
            ),
        )
        event_broker.publish("deliveries", "overview")
        return mutation_payload(result, delivery_id=delivery_id)

    @router.post("/api/deliveries/{delivery_id}/cancel")
    async def cancel_delivery(
        delivery_id: int,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        result = mutate(
            mutation_info,
            "deliveries",
            action="delivery.cancel",
            target=f"delivery#{delivery_id}",
            operation=lambda _version: require_changed(
                bool(
                    services.delivery_store is not None
                    and services.delivery_store.cancel(delivery_id)
                ),
                409,
                "delivery cannot be cancelled from its current state",
            ),
        )
        event_broker.publish("deliveries", "overview")
        return mutation_payload(result, delivery_id=delivery_id)

    @router.get("/api/tasks")
    async def tasks(
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.running_tasks is None:
            return {"items": []}
        return {
            "items": [
                {
                    "task_id": item.task_id,
                    "conversation_id": item.conversation_id,
                    "group_id": item.group_id,
                    "user_id": item.user_id,
                    "message_id": item.message_id,
                    "summary": item.summary,
                    "elapsed_seconds": item.elapsed_seconds,
                }
                for item in services.running_tasks.list_all()
            ]
        }

    @router.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(
        task_id: str,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        result = mutate(
            mutation_info,
            "tasks",
            action="task.cancel",
            target=task_id,
            operation=lambda _version: require_changed(
                bool(
                    services.running_tasks is not None
                    and services.running_tasks.cancel_any(task_id) is not None
                ),
                404,
                "task not found",
            ),
        )
        event_broker.publish("tasks", "overview")
        return mutation_payload(result, task_id=task_id)

    @router.get("/api/jobs")
    def durable_jobs(
        limit: int = Query(default=100, ge=1, le=500),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.job_store is None:
            return {"items": [], "counts": {}, "configured": False}
        return {
            "items": [
                {
                    "job_id": item.job_id,
                    "handle": item.handle,
                    "kind": item.kind,
                    "scope_key": item.scope_key,
                    "status": item.status,
                    "priority": item.priority,
                    "attempts": item.attempts,
                    "max_attempts": item.max_attempts,
                    "next_attempt_at": item.next_attempt_at,
                    "lease_owner": item.lease_owner,
                    "lease_until": item.lease_until,
                    "last_error": item.last_error,
                    "created_at": item.created_at,
                    "updated_at": item.updated_at,
                    "finished_at": item.finished_at,
                }
                for item in services.job_store.recent_summaries(limit=limit)
            ],
            "counts": services.job_store.stats(),
            "configured": True,
        }

    @router.post("/api/jobs/{job_id}/cancel")
    def cancel_durable_job(
        job_id: int,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        result = mutate(
            mutation_info,
            "jobs",
            action="job.cancel",
            target=f"job#{job_id}",
            operation=lambda _version: require_changed(
                bool(
                    services.job_worker.cancel(job_id)
                    if services.job_worker is not None
                    else (
                        services.job_store is not None
                        and services.job_store.cancel(job_id)
                    )
                ),
                404,
                "job is not cancellable",
            ),
        )
        event_broker.publish("jobs", "overview")
        return mutation_payload(result, job_id=job_id)

    @router.post("/api/jobs/{job_id}/retry")
    def retry_durable_job(
        job_id: int,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        result = mutate(
            mutation_info,
            "jobs",
            action="job.retry",
            target=f"job#{job_id}",
            operation=lambda _version: require_changed(
                bool(
                    services.job_store is not None
                    and services.job_store.requeue(job_id)
                ),
                404,
                "job is not retryable",
            ),
        )
        event_broker.publish("jobs", "overview")
        return mutation_payload(result, job_id=job_id)

    return router
