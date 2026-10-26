from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from .context import (
    AdminRouteContext,
    authorize,
    mutation_context,
    mutation_payload,
    require_changed,
)
from .contracts import AdminMutationContext, SubAgentModelUpdate, SubAgentRevisionUpdate


def create_router(context: AdminRouteContext) -> APIRouter:
    router = context.new_router()
    services = context.services
    event_broker = context.event_broker
    mutate = context.mutate

    @router.get("/api/subagents")
    def subagents(
        limit: int = Query(default=100, ge=1, le=500),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.subagent_store is None:
            return {"items": [], "roles": [], "counts": {}, "configured": False}
        roles = (
            services.subagent_coordinator.manifest()
            if services.subagent_coordinator is not None
            else []
        )
        return {
            "items": [
                {
                    "task_id": item.task_id,
                    "handle": item.handle,
                    "trace_id": item.trace_id,
                    "scope_key": item.scope_key,
                    "conversation_id": item.conversation_id,
                    "requester_user_id": item.requester_user_id,
                    "trigger_message_id": item.trigger_message_id,
                    "objective": item.objective,
                    "status": item.status,
                    "plan": item.plan,
                    "result": item.result,
                    "last_error": item.last_error,
                    "cancel_requested": item.cancel_requested,
                    "created_at": item.created_at,
                    "updated_at": item.updated_at,
                    "finished_at": item.finished_at,
                }
                for item in services.subagent_store.recent(limit=limit)
            ],
            "roles": roles,
            "counts": services.subagent_store.stats(),
            "configured": services.subagent_coordinator is not None,
            "scheduler": services.subagent_coordinator.scheduler.snapshot() if services.subagent_coordinator else {},
            "model_options": [{"name": p.name, "model": p.model, "vision": p.capabilities.vision}
                for p in services.model_catalog.profiles if p.configured and p.capabilities.tools],
        }

    @router.get("/api/subagents/{task_id}")
    def subagent_detail(
        task_id: int,
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if services.subagent_store is None:
            raise HTTPException(status_code=404, detail="Sub-Agent task store is unavailable")
        task = services.subagent_store.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Sub-Agent task not found")
        control = services.subagent_store.control(task_id)
        final = services.delivery_store.find_by_key(f"subagent-final:{task_id}:{control['revision']}") if services.delivery_store else None
        from ..agent.progress import task_progress
        progress = task_progress(task, services.subagent_store.runs(task_id), services.subagent_store.task_evidence(task_id),
            services.subagent_store.external_calls(task_id), services.subagent_store.deliveries(task_id), final,
            revision=control["revision"])
        return {
            "progress": progress,
            "control": {key: control[key] for key in ("version", "revision", "policy")},
            "background": bool(control["dispatch"]),
            "final_delivery": {"delivery_id": final.delivery_id, "status": final.status, "attempts": final.attempts,
                "updated_at": final.updated_at, "last_error": final.last_error} if final else None,
            "artifact_deliveries": services.subagent_store.deliveries(task_id),
            "external_waits": [{"run_id": item["run_id"], "call_id": item["call_id"],
                "status": item["status"], "remote_path": item["remote_path"], "updated_at": item["updated_at"]}
                for item in services.subagent_store.external_calls(task_id)],
            "task": {
                "task_id": task.task_id,
                "handle": task.handle,
                "trace_id": task.trace_id,
                "scope_key": task.scope_key,
                "conversation_id": task.conversation_id,
                "requester_user_id": task.requester_user_id,
                "trigger_message_id": task.trigger_message_id,
                "objective": task.objective,
                "status": task.status,
                "plan": task.plan,
                "result": task.result,
                "last_error": task.last_error,
                "cancel_requested": task.cancel_requested,
                "created_at": task.created_at,
                "updated_at": task.updated_at,
                "finished_at": task.finished_at,
            },
            "runs": [
                {
                    "run_id": run.run_id,
                    "handle": run.handle,
                    "step_key": run.step_key,
                    "role": run.role,
                    "objective": run.objective,
                    "deliverable": run.deliverable,
                    "dependencies": list(run.dependencies),
                    "allowed_tools": list(run.allowed_tools),
                    "model_profile": run.model_profile,
                    "status": run.status,
                    "attempt": run.attempt,
                    "result": run.result,
                    "last_error": run.last_error,
                    "created_at": run.created_at,
                    "started_at": run.started_at,
                    "finished_at": run.finished_at,
                }
                for run in services.subagent_store.runs(task_id)
            ],
            "checkpoints": services.subagent_store.checkpoints(task_id),
            "run_contexts": services.subagent_store.run_contexts(task_id),
            "events": services.subagent_store.events(task_id),
        }

    @router.get("/api/subagents/{task_id}/evidence/{evidence_id}")
    def subagent_evidence(task_id: int, evidence_id: str,
                          authorization: Optional[str] = Header(default=None)) -> dict[str, object]:
        authorize(authorization)
        if services.subagent_store is None:
            raise HTTPException(status_code=404, detail="Task evidence unavailable")
        item = next((value for value in services.subagent_store.task_evidence(task_id)
                     if value["evidence_id"] == evidence_id), None)
        if item is None:
            raise HTTPException(status_code=404, detail="Evidence outside current task revision")
        return {key: item[key] for key in ("evidence_id", "task_id", "revision", "run_id", "tool_name",
            "arguments", "payload", "payload_hash", "complete", "recorded_at")}

    @router.put("/api/subagents/{task_id}/models")
    def subagent_models(task_id: int, payload: SubAgentModelUpdate,
        mutation_info: AdminMutationContext = Depends(mutation_context), authorization: Optional[str] = Header(default=None)):
        authorize(authorization)
        def operation(_version):
            if services.subagent_coordinator is None:
                raise HTTPException(503, "Sub-Agent unavailable")
            try:
                return services.subagent_coordinator.configure_models(task_id, payload.policy, payload.expected_version)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        result = mutate(mutation_info, "subagents", action="subagent.models", target=f"task#{task_id}", operation=operation)
        event_broker.publish("subagents", "audit")
        return mutation_payload(result, task_id=task_id)

    @router.post("/api/subagents/{task_id}/revise")
    def subagent_revise(task_id: int, payload: SubAgentRevisionUpdate,
        mutation_info: AdminMutationContext = Depends(mutation_context), authorization: Optional[str] = Header(default=None)):
        authorize(authorization)
        def operation(_version):
            if services.subagent_coordinator is None:
                raise HTTPException(503, "Sub-Agent unavailable")
            task = services.subagent_store.get(task_id)
            if task is None:
                raise HTTPException(404, "Task not found")
            try:
                return services.subagent_coordinator.revise(task_id, scope_key=task.scope_key,
                    requester_user_id=task.requester_user_id, instruction=payload.instruction,
                    step_keys=payload.step_keys, expected_version=payload.expected_version,
                    file_delivery_required=payload.file_delivery_required)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        result = mutate(mutation_info, "subagents", action="subagent.revise", target=f"task#{task_id}", operation=operation)
        event_broker.publish("subagents", "jobs", "audit")
        return mutation_payload(result, task_id=task_id)

    @router.post("/api/subagents/{task_id}/cancel")
    def cancel_subagent_task(
        task_id: int,
        mutation_info: AdminMutationContext = Depends(mutation_context),
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        result = mutate(
            mutation_info,
            "subagents",
            action="subagent.cancel",
            target=f"task#{task_id}",
            operation=lambda _version: require_changed(
                bool(
                    services.subagent_coordinator is not None
                    and services.subagent_coordinator.cancel(task_id)
                ),
                409,
                "Sub-Agent task is not cancellable",
            ),
        )
        event_broker.publish("subagents", "tasks", "overview")
        return mutation_payload(result, task_id=task_id)

    return router
