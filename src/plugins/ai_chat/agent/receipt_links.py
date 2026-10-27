"""Recover historical authorization from this task's persisted intents, never replay them."""
from __future__ import annotations

import asyncio
import hashlib

from .control import LeaseLost, assert_job_owned
from .evidence import canonical
from .external import remote_record


RECEIPT_TOOL = "historical_operation_receipt"
_MANAGED_OPS = {"exec.run", "host.reboot", "units.start", "units.stop", "units.restart", "units.reload"}


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def evidence_fingerprint(items):
    # Reviewer output and lookup timestamps must not invalidate their own acceptance cache.
    return digest(sorted((item["evidence_id"], item["payload_hash"]) for item in items))


def receipt_sources(store, task, run_ids, revision):
    allowed = {run.run_id for run in store.runs(task.task_id)} & set(run_ids)
    ancestors = {revision: allowed}
    checkpoints = {item["state"].get("revision"): item["state"] for item in store.revision_checkpoints(task.task_id)}
    # A previous revision is accessible only through the same run's explicit revision checkpoint.
    for current in range(revision, 1, -1):
        previous = checkpoints.get(current, {}).get("previous_runs", [])
        allowed = allowed & {item["run_id"] for item in previous}
        if not allowed:
            break
        ancestors[current - 1] = allowed
    sources = []
    for source_revision, permitted in ancestors.items():
        for item in store.external_calls(task.task_id, revision=source_revision):
            if (item["task_id"] != task.task_id or item["revision"] != source_revision
                    or item["run_id"] not in permitted):
                continue
            request = item["request"]
            if (request.get("method") != "POST" or request.get("path") != "/v1/ops/call"
                    or request.get("actor") != f"qq:{store.revision_actor(task.task_id, source_revision)}"
                    or request.get("origin") != task.scope_key):
                continue
            body = request.get("body")
            if not isinstance(body, dict) or not isinstance(body.get("params"), dict):
                continue
            key = "subagent:" + digest([task.task_id, source_revision, item["run_id"], item["call_id"]])
            if body.get("idempotency_key") != key:
                continue
            record = remote_record(item["response"])
            if body.get("operation") not in _MANAGED_OPS and not record.get("operation_id"):
                continue
            sources.append(item)
    return sources


def _matches(proof, item):
    body = item["request"]["body"]
    record = remote_record(item["response"])
    if not isinstance(proof, dict) or not (
        proof.get("source") == "gaoji-control" and proof.get("historical") is True
        and proof.get("intent_key") == body["idempotency_key"]
        and proof.get("origin_scope") == item["request"]["origin"]
        and proof.get("operation") == body["operation"]
        and proof.get("host_id") == body["params"].get("host")
        and proof.get("params_hash") == digest(body["params"])
        and proof.get("operation_id")
    ):
        return False
    if record.get("operation_id") and record["operation_id"] != proof["operation_id"]:
        return False
    if record.get("backend_operation_id") and record["backend_operation_id"] != proof.get("backend_job_id"):
        return False
    approval = proof.get("approval") or {}
    return (proof.get("authorized_before_dispatch") is not True or (
        proof.get("contract_hash") and approval.get("contract_hash") == proof["contract_hash"]
        and approval.get("operation_id") == proof["operation_id"]
        and approval.get("consumed_at") is not None and proof.get("dispatched_at") is not None))


async def link_operation_receipts(store, task, run_ids, lookup):
    revision = store.control(task.task_id)["revision"]
    def check_owned():
        assert_job_owned()
        if store.cancellation_requested(task.task_id):
            raise asyncio.CancelledError
        if store.control(task.task_id)["revision"] != revision:
            raise LeaseLost("Task revision changed while reading historical receipts")
    check_owned()
    sources = receipt_sources(store, task, run_ids, revision)
    existing = store.task_evidence(task.task_id, run_ids=set(run_ids))
    cached = {item["payload"].get("source_request", {}).get("request_hash") for item in existing
              if item["tool_name"] == RECEIPT_TOOL and item["payload"].get("ok") is True}
    for item in sources:
        check_owned()
        request = item["request"]
        request_hash = digest(request)
        if request_hash in cached:
            continue
        try:
            async with asyncio.timeout(5):
                proof = await lookup(request["body"]["idempotency_key"],
                                     actor=f"qq:{store.revision_actor(task.task_id)}", origin=request["origin"])
        except Exception:
            # A missing receipt is not an instruction to resubmit or approve a server command.
            check_owned()
            continue
        check_owned()
        if not _matches(proof, item):
            continue
        verified = proof.get("authorized_before_dispatch") is True
        payload = {"ok": verified, "historical": True,
            "source_request": {"task_id": task.task_id, "revision": item["revision"],
                "run_id": item["run_id"], "call_id": item["call_id"], "request_hash": request_hash,
                "record_updated_at": item["updated_at"]},
            "receipt": proof,
            "instruction": "仅证明原请求的历史授权与派发。旧状态不能证明当前健康或修复效果，也不授予新的执行权限。"}
        if not verified:
            payload["error"] = "historical_authorization_unverified"
        store.record_evidence(task.task_id, item["run_id"], RECEIPT_TOOL,
            {"operation": request["body"]["operation"], "params": request["body"]["params"],
             "source_revision": item["revision"]}, payload,
            call_id="receipt:" + request_hash, revision=revision)
