"""Host-controlled approvals and persisted results around one remote invocation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import secrets

from src.bot_security.store import SecurityError
from ..agent.evidence import redact
from .calls import TERMINAL, McpCallStore
from .client import McpCallError, McpWireClient
from .contracts import canonical
from .store import McpDenied


class McpService:
    def __init__(self, store: McpCallStore, wire: McpWireClient, *,
                 scope_allowed: Callable[[str, str, str], Awaitable[bool]], mobile=None,
                 db_call=asyncio.to_thread):
        self.store, self.wire, self.scope_allowed = store, wire, scope_allowed
        self.mobile, self.db_call = mobile, db_call

    async def _scope(self, call):
        async with asyncio.timeout(5):
            allowed = await self.scope_allowed(call["scope_key"], call["bot_id"], call["user_id"])
        if not allowed:
            raise McpDenied("MCP actor or bot no longer belongs to the enabled group")

    async def _read(self, call):
        return await self.db_call(self.store.call, call["call_id"], scope_key=call["scope_key"],
                                  bot_id=call["bot_id"], user_id=call["user_id"])

    @staticmethod
    def _result(call):
        return {"call_id": call["call_id"], "status": call["status"],
                "ok": call["status"] == "succeeded", "executed": call["status"] == "succeeded",
                "dispatch_started": call["dispatched_at"] is not None,
                "needs_attention": call["status"] == "needs_attention", "result": call["result"]}

    async def invoke(self, *, scope_key, bot_id, user_id, invocation, name, remote, arguments,
                     expected_configuration=None, before_approval=None):
        identity = {"scope_key": scope_key, "bot_id": bot_id, "user_id": user_id}
        await self._scope(identity)
        call = await self.db_call(self.store.prepare_call, **identity, invocation=invocation,
                                 name=name, remote=remote, arguments=arguments,
                                 expected_configuration=expected_configuration)
        if call["status"] in TERMINAL:
            return self._result(call)
        if call["binding"]["configuration"]["trust"] == "approval":
            if call["status"] != "waiting":
                return self._result(call)
            if self.mobile is None or self.mobile.bot_selector() != bot_id:
                raise McpDenied("MCP approval service is not available for this bot")
            account = await self.db_call(self.mobile.store.account_for_qq, user_id)
            if account is None:
                raise McpDenied("An unreviewed MCP tool requires a bound administrator")
            summary = ("MCP 外部工具执行，需两位管理员复核。\n"
                + canonical({"group": scope_key, "bot": bot_id, "user": user_id,
                    "tool": name + "/" + remote, "configuration": call["binding"]["configuration"],
                    "arguments": redact(arguments), "binding_hash": call["binding_hash"]}))
            if len(summary) > 11000:
                raise McpDenied("MCP approval arguments are too long to review; narrow the operation")
            if before_approval is not None:
                await before_approval(call)
            approval = await self.mobile.propose(account, kind="mcp_tool", quorum=2,
                payload=self.store.approval_payload(call), summary=summary, reference=call["call_id"])
            return {"ok": False, "executed": False, "status": "awaiting_approval", "approval_required": True,
                "call_id": call["call_id"], "approval_id": approval["approval_id"], "message": approval["message"]}
        return await self._run(call)

    async def execute_approved(self, request):
        if self.mobile is None or request.get("kind") != "mcp_tool":
            raise McpDenied("Invalid MCP approval executor")
        await self.db_call(self.mobile.store.validate_execution_approval, request["approval_id"], self.mobile.owner)
        call = await self.db_call(self.store.authorize_call, request["payload"]["call_id"], request["approval_id"])
        if request["payload"] != self.store.approval_payload(call):
            raise McpDenied("MCP approval payload changed")
        return await self._run(call, approval_id=request["approval_id"])

    async def _run(self, call, *, approval_id=None):
        await self._scope(call)
        if call["status"] in TERMINAL:
            return self._result(call)
        if call["approval_id"] != approval_id:
            raise McpDenied("MCP call must run through its original approval executor")
        owner = secrets.token_hex(16)
        claimed = await self.db_call(self.store.claim_call, call["call_id"], owner=owner,
            scope_key=call["scope_key"], bot_id=call["bot_id"], user_id=call["user_id"])
        if claimed is None:
            return self._result(await self._read(call))

        async def before_call():
            await self._scope(call)
            if approval_id:
                await self.db_call(self.mobile.store.validate_execution_approval, approval_id, self.mobile.owner)
            await self.db_call(self.store.dispatch_call, call["call_id"], owner)

        try:
            server, tool = await self.db_call(self.store.resolve, call["server_name"], call["remote_name"], call["scope_key"])
            result = await self.wire.call(server["config"], tool["definition"],
                claimed["binding"]["arguments"], before_call=before_call)
            await self.db_call(self.store.finish_call, call["call_id"], owner, result)
        except asyncio.CancelledError:
            # If cancellation races the DB dispatch commit, recovery examines the
            # persisted boundary rather than trusting an in-memory 'sent' flag.
            raise
        except (McpCallError, McpDenied, SecurityError, ValueError) as error:
            current = await self._read(call)
            if current["status"] not in TERMINAL:
                unknown = current["status"] == "dispatched" or bool(getattr(error, "may_have_run", False))
                await self.db_call(self.store.finish_call, call["call_id"], owner,
                    {"ok": False, "code": error.code if isinstance(error, McpCallError) else "mcp_host_check_failed"},
                    unconfirmed=unknown)
        # Check scope again before disclosing a result if the actor left mid-call.
        await self._scope(call)
        return self._result(await self._read(call))
