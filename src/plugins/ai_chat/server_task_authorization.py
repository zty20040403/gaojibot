"""One durable server authorization per host-issued task, shared by its steps."""
from __future__ import annotations

import asyncio
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from nonebot import get_bot

from src.bot_security.service import assert_approved
from src.bot_security.store import SecurityError, canonical, digest
from .agent.execution import active_agent_step
from .onebot_codec import scope_from_event
from .agent.collaboration_runtime import group_roster, execution_event
from .workers.durable_jobs import JobDeferred


server_task: ContextVar[dict[str, Any] | None] = ContextVar("server_task", default=None)


class ServerTaskAuthorization:
    def __init__(self, context):
        self.context = context
        self.mobile = context.mobile_authorization
        self.mobile.executors["server_task"] = self.grant

    @contextmanager
    def bind(self, event, turn_id):
        scope = {"scope": scope_from_event(event).key, "qq_id": str(event.user_id),
                 "bot_id": str(event.self_id)}
        step = re.fullmatch(r"task#([1-9][0-9]*)/.+", active_agent_step.get() or "")
        if step:
            task_id = int(step[1])
            task = self.context.subagent_store.get(task_id)
            scope.update(kind="task", id=task_id,
                revision=self.context.subagent_store.control(task_id)["revision"],
                objective=task.objective if task else "")
        elif turn_id is not None:
            turn = self.context.turn_journal.get_turn_by_id(turn_id)
            scope.update(kind="turn", id=turn_id, revision=turn.started_at if turn else 0,
                objective=turn.objective if turn else "")
        else:
            scope = None
        token = server_task.set(scope)
        try:
            yield
        finally:
            server_task.reset(token)

    def validate(self, scope):
        if not isinstance(scope, dict) or scope.get("bot_id") != self.mobile.bot_selector():
            raise SecurityError("服务器操作没有有效的任务身份")
        if scope.get("kind") == "task":
            task = self.context.subagent_store.get(scope["id"])
            valid = (task is not None and task.scope_key == scope["scope"]
                and str(self.context.subagent_store.revision_actor(scope["id"])) == scope["qq_id"]
                and task.status in {"received", "queued", "planning", "running", "verifying", "waiting_external", "interrupted"}
                and not task.cancel_requested and task.finished_at is None
                and task.objective == scope["objective"]
                and self.context.subagent_store.control(scope["id"])["revision"] == scope["revision"])
            dispatch = self.context.subagent_store.control(scope["id"]).get("dispatch", {})
            valid = valid and float(dispatch.get("deadline") or float("inf")) > time.time()
            if valid:
                try:
                    self.context.subagent_store.require_execution_actor(scope["id"], int(scope["qq_id"]))
                except (ValueError, PermissionError):
                    valid = False
        elif scope.get("kind") == "turn":
            turn = self.context.turn_journal.get_turn_by_id(scope["id"])
            valid = (turn is not None and turn.scope_key == scope["scope"]
                and turn.status == "running" and turn.finished_at is None
                and turn.objective == scope["objective"] and turn.started_at == scope["revision"])
        else:
            valid = False
        if not valid:
            raise SecurityError("本任务已结束、取消或变更，旧服务器授权已失效", 409)

    async def validate_execution(self, scope):
        """Approval watchers run outside the task guard, so recheck membership here."""
        await asyncio.to_thread(self.validate, scope)
        if scope["kind"] != "task":
            return
        store = self.context.subagent_store
        control = await asyncio.to_thread(store.control, scope["id"])
        if control.get("dispatch", {}).get("execution") is None:
            return
        task = await asyncio.to_thread(store.get, scope["id"])
        try:
            event = await asyncio.to_thread(execution_event, store, task, control)
            bot = get_bot(scope["bot_id"])
            members = await group_roster(bot, event)
        except (KeyError, ValueError, JobDeferred):
            raise SecurityError("无法核实协作任务的群成员身份，未执行服务器操作", 503) from None
        if event.user_id not in members or task.requester_user_id not in members:
            raise SecurityError("任务发起人或协作者已离群，旧服务器授权不可继续使用", 409)
        await asyncio.to_thread(self.validate, scope)

    async def grant(self, request):
        assert_approved("server_task", request["payload"])
        scope = request["payload"]
        await self.validate_execution(scope)
        await asyncio.to_thread(self.mobile.store.validate_execution_approval, request["approval_id"], self.mobile.owner)
        if scope["qq_id"] != request["qq_id"] or scope["bot_id"] != request["bot_id"]:
            raise SecurityError("任务与授权账户不匹配")
        return {"ok": True, "task": f"{scope['kind']}#{scope['id']}",
                "message": "本任务已统一授权，所有子任务共享；结束、取消或修订后失效。"}

    async def ensure(self, scope, account, *, wait=True):
        await self.validate_execution(scope)
        if account["qq_id"] != scope["qq_id"]:
            raise SecurityError("任务发起人与授权管理员不匹配")
        current = await asyncio.to_thread(self.mobile.store.account_for_qq, scope["qq_id"])
        if not current or current["account_id"] != account["account_id"] or current["version"] != account["version"]:
            raise SecurityError("管理员权限已变化")
        request = await asyncio.to_thread(self.mobile.store.task_authorization, account, scope["bot_id"], scope)
        if request is None:
            reference = "server-task:" + digest(canonical([account["account_id"], account["version"], scope]))
            request = await self.mobile.propose(account, kind="server_task", payload=scope, reference=reference,
                summary=f"本任务双管理员统一服务器授权：{scope['kind']}#{scope['id']}\n会话：{scope['scope']}\n"
                    f"目标：{scope['objective'][:1800]}\n"
                    "两位不同管理员确认后，本任务及所有子任务可在管理员已有服务器权限内执行重要命令，无需逐条确认。"
                    "不扩大服务器原有权限；任务结束、取消或修订后失效。")
        deadline = time.monotonic() + 185
        while True:
            await asyncio.to_thread(self.validate, scope)
            current = await asyncio.to_thread(self.mobile.store.task_authorization, account, scope["bot_id"], scope)
            if current is None:
                raise SecurityError("本任务服务器授权已撤销")
            if current["status"] == "succeeded":
                await self.validate_execution(scope)
                current = await asyncio.to_thread(self.mobile.store.task_authorization, account, scope["bot_id"], scope)
                if current is None or current["status"] != "succeeded":
                    raise SecurityError("任务双人授权已撤销，未执行重要命令", 409)
                return True
            if current["status"] not in {"sending", "pending", "awaiting_quorum", "queued", "executing"}:
                raise SecurityError("本任务服务器授权未通过；可在私聊重发仍有效任务的口令")
            if not wait:
                return False
            if time.monotonic() >= deadline:
                raise SecurityError("等待本任务服务器授权超时，未执行重要命令", 408)
            await asyncio.sleep(0.5)
            await asyncio.to_thread(self.mobile.store.get, request["approval_id"], account["account_id"])
