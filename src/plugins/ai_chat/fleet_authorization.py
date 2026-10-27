from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from src.bot_security.service import MobileAuthorization, assert_approved, current_principal
from src.bot_security.store import SecurityError
from .server_task_authorization import server_task
from .agent.external import active_external


class FleetAuthorization:
    def __init__(self, client: Any, mobile: MobileAuthorization):
        self.client = client
        self.mobile = mobile
        mobile.executors["fleet"] = self.execute
        mobile.http_summary = self.http_summary
        mobile.pollers.append(self.poll)
        self.next_discovery = 0.0
        self.tasks = None
        self.submit_lock = asyncio.Lock()

    async def account(self, actor: str) -> dict[str, Any]:
        account = current_principal.get()
        if account is None and actor.startswith("qq:"):
            account = await asyncio.to_thread(self.mobile.store.account_for_qq, actor[3:])
        if not account or account["role"] != "admin" or not account["enabled"]:
            raise SecurityError("仅绑定 QQ 的管理员可以管理服务器")
        return account

    async def request(self, method: str, path: str, body: dict[str, Any] | None, *, actor: str, origin: str) -> dict[str, Any]:
        if method == "GET":
            if actor.startswith("qq:"):
                account = await asyncio.to_thread(self.mobile.store.account_for_qq, actor[3:])
                if account:
                    actor = "admin:" + account["username"]
            return await self.client._raw_request(method, path, body, actor=actor, origin=origin)
        # Diagnostics only observe a fixed, preconfigured target.
        if path in {"/v1/diagnostics", "/v1/runbook-cases/search"}:
            return await self.client._raw_request(method, path, body, actor=actor, origin=origin)
        account = await self.account(actor)
        console = bool(account.get("session_hash")) and origin == "admin-console"
        actor = "admin:" + account["username"]
        if path in {"/v1/ops/call", "/v1/operations/prepare", "/v1/deployments/prepare"}:
            result = await self.client._raw_request(method, path, body, actor=actor, origin=origin)
            record = result.get("operation", result)
            if isinstance(record, dict) and (record.get("operation_id") or record.get("deployment_id")):
                await self.watch(record, account, actor, origin)
            if isinstance(record, dict) and record.get("status") == "awaiting_approval":
                approval = await self.propose_record(record, account, actor, origin,
                    wait=active_external.get() is None)
                return {**result, **approval}
            return result
        if re.fullmatch(r"/v1/(operations|deployments)/[^/]+/approve", path):
            record = await self.client._raw_request("GET", path.removesuffix("/approve"), actor=actor, origin=origin)
            self.check_contract(record, body or {})
            return await self.propose_record(record, account, actor, origin, authorize_console=console)
        elif self.important_mutation(path):
            if console:
                payload = {"method": method, "path": path, "body": body, "actor": actor, "origin": origin}
                return await self.mobile.propose(account, kind="fleet", payload=payload,
                    summary="服务器高风险变更：\n" + json.dumps(payload, ensure_ascii=False, indent=2))
            scope = server_task.get()
            if self.tasks is None or scope is None or scope["scope"] != origin:
                raise SecurityError("重要服务器命令需要绑定当前任务后申请统一授权")
            await self.tasks.ensure(scope, account)
        result = await self.client._raw_request(method, path, body, actor=actor, origin=origin)
        if result.get("operation_id") or result.get("deployment_id") or result.get("job_id"):
            await self.watch(result, account, actor, origin)
        await asyncio.to_thread(self.mobile.store.record_action, account["account_id"],
            "fleet.submitted", path, "console" if console else "task")
        if result.get("job_id") and result.get("status") in {"queued", "running", "verifying", "cancelling"}:
            return {**result, "submitted": True}
        return result

    @staticmethod
    def important_mutation(path):
        # Worker tasks remain quota-bound isolated jobs, not host shell commands.
        ordinary = {"/v1/jobs", "/v1/artifacts", "/v1/guardians", "/v1/runbook-cases",
                    "/v1/borrow-grants"}
        return not (path in ordinary or re.fullmatch(
            r"/v1/(jobs/[^/]+/cancel|guardians/[^/]+/status|borrow-grants/[^/]+/status|resource-policies/[^/]+/(capacity|availability))", path))

    @staticmethod
    def contract_view(record: dict[str, Any]) -> dict[str, Any]:
        # Only immutable, reviewed fields; runtime progress is not authorization data.
        keys = ("operation_id", "deployment_id", "host_id", "resource_ref", "operation", "arguments",
                "contract_hash", "resource_version", "intent", "targets", "source", "plan", "artifact",
                "repository", "repository_id", "revision", "source_revision", "expected_remote_revision", "requested_changes",
                "canary_host_id", "failure_policy", "ref", "target_hosts", "strategy", "resolved_commit",
                "deadline_at", "expected_state", "verification", "compensation", "contract", "preflight")
        return {key: record[key] for key in keys if key in record}

    @classmethod
    def describe(cls, record: dict[str, Any]) -> str:
        return "核对本次服务器操作（只批准本次操作）：\n" + json.dumps(cls.contract_view(record), ensure_ascii=False, indent=2)

    @staticmethod
    def check_contract(record: dict[str, Any], body: dict[str, Any]) -> None:
        if record.get("status") != "awaiting_approval" or record.get("contract_hash") != body.get("contract_hash") or record.get("resource_version") != body.get("resource_version"):
            raise SecurityError("操作已变化或不在待批准状态，请重新准备并核对", 409)

    async def http_summary(self, payload: dict[str, Any], summary: str) -> str:
        path = payload["path"]
        if re.fullmatch(r"/fleet/(operations|deployments)/[^/]+/approve", path):
            account = await self.account("")
            remote_path = "/v1" + path.removeprefix("/fleet").removesuffix("/approve")
            record = await self.client._raw_request("GET", remote_path, actor="admin:" + account["username"], origin="admin-console")
            self.check_contract(record, payload["body"] or {})
            return self.describe(record)
        return summary

    async def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        payload = request["payload"]
        assert_approved("fleet", payload)
        if "contract" in payload:
            record = await self.client._raw_request("GET", payload["path"].removesuffix("/approve"), actor=payload["actor"], origin=payload["origin"])
            self.check_contract(record, payload["body"])
            if self.contract_view(record) != payload["contract"]:
                raise SecurityError("操作参数已变化，旧口令不可使用", 409)
        await asyncio.to_thread(self.mobile.store.validate_execution_approval, request["approval_id"], self.mobile.owner)
        result = await self.client._raw_request(payload["method"], payload["path"], payload["body"], actor=payload["actor"], origin=payload["origin"])
        if result.get("operation_id") or result.get("deployment_id") or result.get("job_id"):
            await self.watch(result, request["account"], payload["actor"], payload["origin"])
        return {"ok": True, "submitted": result.get("status") in {"queued", "running", "verifying", "cancelling", "preflight_queued"}, "data": result,
                "message": "已提交这次已批准的操作；服务器最终结果将另发 QQ 私聊。"}

    @staticmethod
    def record_path(record: dict[str, Any]) -> str:
        for key, kind in (("deployment_id", "deployments"), ("operation_id", "operations"), ("job_id", "jobs")):
            identifier = record.get(key)
            if identifier:
                if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(identifier)):
                    raise SecurityError("无效服务器操作编号", 502)
                return f"/v1/{kind}/{identifier}"
        raise SecurityError("无效服务器操作编号", 502)

    async def watch(self, record, account, actor, origin):
        metadata = dict(account)
        scope = server_task.get()
        if scope is not None and scope["scope"] == origin and scope["qq_id"] == account["qq_id"]:
            metadata["server_task"] = scope
        await asyncio.to_thread(self.mobile.store.watch_fleet, self.record_path(record), metadata,
                               self.mobile.bot_selector(), actor, origin)

    async def propose_record(self, record, account, actor, origin, *, wait=True, authorize_console=False):
        path = self.record_path(record)
        if not record.get("contract_hash") or not record.get("resource_version"):
            raise SecurityError("操作未提供完整批准信息", 502)
        if account.get("session_hash") and origin == "admin-console" and not authorize_console:
            return {"approval_required": True, "operation": record,
                    "next_action": "操作参数已准备；点击执行会申请两位管理员复核，尚未执行服务器命令。"}
        scope = server_task.get() or account.get("server_task")
        if scope is not None:
            if self.tasks is None or scope["scope"] != origin:
                raise SecurityError("服务器操作与任务范围不匹配")
            ready = await self.tasks.ensure(scope, account, wait=wait)
            if not ready:
                return {"approval_required": True, "operation": record,
                        "next_action": "等待本任务统一授权，不要重复申请。"}
            # Polling and parallel steps may see the same proposal. Re-read under
            # one submission lock; the server also checks contract/version CAS.
            async with self.submit_lock:
                latest = await self.client._raw_request("GET", path, actor=actor, origin=origin)
                await self.tasks.validate_execution(scope)
                if not await self.tasks.ensure(scope, account, wait=False):
                    raise SecurityError("任务双人授权尚未完成", 409)
                if latest.get("status") == "awaiting_approval":
                    if self.contract_view(latest) != self.contract_view(record):
                        raise SecurityError("服务器操作预检已变化，请重新读取", 409)
                    result = await self.client._raw_request("POST", path + "/approve",
                        {"contract_hash": latest["contract_hash"], "resource_version": latest["resource_version"]},
                        actor=actor, origin=origin)
                    await asyncio.to_thread(self.mobile.store.record_action, account["account_id"],
                        "fleet.task_authorized", path, f"{scope['kind']}#{scope['id']}")
                else:
                    result = latest
            return {"ok": True, "approval_required": False, "operation": result,
                    "submitted": result.get("status") in {"queued", "running", "verifying"},
                    "executed": result.get("status") == "succeeded",
                    "next_action": "本任务已统一授权。操作已提交或已有执行记录，读取最终结果；不要再次请求口令或重复提交。"}
        payload = {"method": "POST", "path": path + "/approve",
                   "body": {"contract_hash": record["contract_hash"], "resource_version": record["resource_version"]},
                   "actor": actor, "origin": origin, "contract": self.contract_view(record)}
        return await self.mobile.propose(account, kind="fleet", payload=payload, summary=self.describe(record),
            reference=path + ":" + record["contract_hash"] + ":" + str(record["resource_version"]))

    async def poll(self):
        # Discover exact guardian repair proposals made while the bot was offline.
        if time.monotonic() >= self.next_discovery:
            self.next_discovery = time.monotonic() + 15
            accounts = {"admin:" + item["username"]: item for item in await asyncio.to_thread(self.mobile.store.accounts)
                        if item["enabled"] and item["role"] == "admin"}
            recent = await self.client._raw_request("GET", "/v1/operations?limit=200")
            for record in recent.get("items", []):
                if record.get("status") == "awaiting_approval" and record.get("arguments", {}).get("guardian_id"):
                    account = accounts.get(record.get("actor_id"))
                    if account:
                        await self.watch(record, account, record["actor_id"], record["origin_scope"])
        for item in await asyncio.to_thread(self.mobile.store.fleet_watches):
            done = False
            try:
                record = await self.client._raw_request("GET", item["path"], actor=item["actor"], origin=item["origin"])
                if record.get("status") == "awaiting_approval":
                    if self.mobile.bot_selector() == item["bot_id"]:
                        await self.propose_record(record, item["account"], item["actor"], item["origin"], wait=False)
                elif record.get("status") in {"succeeded", "failed", "cancelled", "expired", "needs_attention", "preflight_failed", "rolled_back"}:
                    await self.mobile.sender(item["bot_id"], item["account"]["qq_id"],
                        f"服务器执行结果：{item['path'].rsplit('/', 1)[-1]}\n状态：{record['status']}\n"
                        + json.dumps({k: record[k] for k in ("worker_id", "host_id", "error_code", "result", "error", "failure_reason", "verification") if k in record}, ensure_ascii=False)[:3000])
                    done = True
            except Exception:
                # Polling/notification failure never resubmits the server mutation.
                pass
            finally:
                await asyncio.to_thread(self.mobile.store.finish_fleet_watch, item["path"], done=done)
