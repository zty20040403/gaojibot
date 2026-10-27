"""Resume the ordinary Agent from approved receipts, under a durable job fence."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json

from nonebot import get_bot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message

from ..agent.control import JobFence, LeaseLost, active_job_fence
from ..agent.execution import active_agent_step
from ..agent_loop import AgentTrace
from ..message_ir import MessageBody, TextNode
from ..model_budget import ModelBudgetExceeded, ModelCallBudget, model_budget_scope
from ..onebot_availability import delivery_blocker
from ..onebot_codec import decode_onebot_message
from ..output_planner import plan_reply
from ..tool_policy import policy_for_tool
from ..workers.durable_jobs import JobDeferred
from .checkpoint import validate_transcript
from .continuations import CONTINUATION_JOB, McpEffectUncertain
from .contracts import canonical


class McpApprovalPending(BaseException):
    def __init__(self, message="外部工具已暂停，等待管理员审批；审批完成后会继续并把结果发回这里。"):
        self.message = message
        super().__init__(message)


class McpDeferred(BaseException):
    """Pause the worker without being converted to a model/tool failure."""


class McpChatCheckpoint:
    def __init__(self, app, turn, profile, *, execution=None):
        self.app, self.turn, self.profile, self.execution = app, turn, profile, execution
        self.messages = []
        self.system_context = ""
        self.history_length = execution.state["checkpoint"]["history_length"] if execution is not None else None
        self.prepared = False

    def capture(self, messages):
        if self.history_length is None:
            # The first capture precedes this turn's first model/tool loop.
            self.history_length = max((index + 1 for index, message in enumerate(messages)
                                       if message.get("role") == "user"), default=0)
        self.messages = deepcopy(messages)

    async def prepare(self, call):
        event = self.turn.context.event
        if self.execution is not None:
            await self.execution.guard()
            await self.app.db_call(self.app.mcp_runtime.store.pause_for_approval, self.execution.job,
                call["call_id"], self.messages, self.turn.reply_checkpoint())
        else:
            await self.app.db_call(self.app.mcp_runtime.store.attach_checkpoint, call["call_id"],
                scope_key=f"onebot-v11:group:{event.group_id}", bot_id=str(event.self_id), user_id=str(event.user_id),
                messages=self.messages, question=self.turn.user_text,
                profile={"name": self.profile.name, "provider": self.profile.provider_identity, "model": self.profile.model},
                message_id=event.message_id, message_time=event.time,
                reply=self.turn.reply_checkpoint(), system_context=self.system_context,
                history_length=self.history_length or 0)
        self.prepared = True


class McpContinuationExecution:
    def __init__(self, runtime, job, state, call):
        self.runtime, self.app, self.store = runtime, runtime.app, runtime.store
        self.job, self.state, self.call = job, state, call
        self.turn = None

    async def guard(self):
        if not await self.app.db_call(self.store.owns_execution, self.job):
            raise LeaseLost("MCP continuation lease changed")
        if self.store.clock() >= self.state["created_at"] + 3600:
            raise ModelBudgetExceeded("MCP continuation exceeded its one-hour lifetime")
        if not await self.runtime.scope_allowed(self.call["scope_key"], self.call["bot_id"], self.call["user_id"]):
            raise McpDeferred("MCP 原群成员身份暂时无法核实")

    async def reserve(self, reservation):
        await self.app.db_call(self.store.reserve_model_call, self.job, reservation.reserved)

    async def checkpoint(self, messages):
        await self.guard()
        reply = self.turn.reply_checkpoint() if self.turn is not None else self.state["reply"]
        if messages == self.state["messages"] and reply == self.state["reply"]:
            return
        revision = await self.app.db_call(self.store.save_transcript, self.job, messages,
            expected_revision=self.state["revision"], reply=reply)
        self.state.update(messages=deepcopy(messages), reply=deepcopy(reply), revision=revision)

    async def tool(self, name, arguments, action):
        await self.guard()
        # MCP already owns its exact invocation/dispatch receipt. Its waiting
        # state must remain resumable rather than becoming an unknown host effect.
        if name.startswith("mcp__") or active_agent_step.get() is not None:
            return await action()
        pending = validate_transcript(self.state["messages"])
        if not pending:
            raise LeaseLost("Continuation tool has no persisted call")
        identifier = next(iter(pending))
        policy = policy_for_tool(name)
        # A keyed tool may own a separate receipt, but that is not evidence that
        # this particular continuation can safely repeat it after a crash.
        idempotency = "non-idempotent" if policy.idempotency == "keyed" else policy.idempotency
        saved = await self.app.db_call(self.store.begin_effect, self.job, identifier, name, arguments,
                                       idempotency=idempotency)
        if saved is not None:
            receipt = json.loads(saved)
            if self.turn is not None:
                self.turn.restore_reply_checkpoint(receipt["reply"])
            return receipt["result"]
        try:
            result = await action()
            receipt = canonical({"result": result, "reply": self.turn.reply_checkpoint() if self.turn is not None else {}})
            await self.app.db_call(self.store.finish_effect, self.job, identifier, receipt)
        except Exception as exc:
            if idempotency == "non-idempotent":
                raise McpEffectUncertain("A continuation effect returned no receipt") from exc
            raise
        return result


def continuation_event(state, call):
    checkpoint = state["checkpoint"]
    return GroupMessageEvent(time=checkpoint["message_time"], self_id=int(call["bot_id"]),
        post_type="message", message_type="group", sub_type="normal", user_id=int(call["user_id"]),
        group_id=int(call["scope_key"].rsplit(":", 1)[1]), message_id=checkpoint["message_id"],
        message=Message(checkpoint["question"]), original_message=Message(checkpoint["question"]),
        raw_message=checkpoint["question"], font=0, sender={"user_id": int(call["user_id"])}, to_me=True)


class McpContinuationDispatcher:
    def __init__(self, runtime):
        self.runtime, self.app, self.store = runtime, runtime.app, runtime.store
        self.app.job_worker.register(CONTINUATION_JOB, self.execute, timeout_seconds=600)

    async def execute(self, job):
        state, call = await self.app.db_call(self.store.execution, job)
        if state["status"] == "enqueued":
            return {"delivery_id": state["delivery_id"], "recovered": True}
        execution = McpContinuationExecution(self.runtime, job, state, call)
        token = active_job_fence.set(JobFence(job.job_id, job.lease_owner, job.attempts,
                                            lambda: self.store.owns_execution(job)))
        try:
            if self.store.clock() >= state["created_at"] + 3600:
                raise ModelBudgetExceeded("MCP continuation expired")
            resumed = await self.app.db_call(self.store.resolve_pending_approval, job)
            if resumed is None:
                raise JobDeferred("MCP 工具仍在等待审批或执行回执", 15)
            execution.state = state = resumed
            try:
                bot = get_bot(call["bot_id"])
            except KeyError:
                raise JobDeferred("MCP 原机器人账号尚未连接", 30) from None
            if str(bot.self_id) != call["bot_id"]:
                raise JobDeferred("MCP 原机器人账号不匹配", 30)
            blocker = await delivery_blocker(bot)
            if blocker:
                raise JobDeferred(blocker, 30)
            await execution.guard()
            if await self.app.db_call(self.store.uncertain_effects, job):
                raise McpEffectUncertain("An earlier effect lacks a receipt")
            saved = state["checkpoint"]
            profile = self.app.model_catalog.try_resolve(saved["profile"]["name"])
            if profile is None or {"name": profile.name, "provider": profile.provider_identity, "model": profile.model} != saved["profile"]:
                return await self._notice(job, "审批已处理，但原模型配置已变更，续跑已暂停。请核对工具回执后重新发起请求。")
            budget = ModelCallBudget(max(1, 262144 - state["reserved_tokens"]), max_requests=8,
                                     before_request=execution.guard, reserve_request=execution.reserve)
            with model_budget_scope(budget):
                answer = await self.runtime.handlers.tools._ask_ai(
                    bot, continuation_event(state, call), saved["question"], selected_profile_override=profile,
                    available_image_sources=[], turn_trace=AgentTrace(), _mcp_execution=execution)
            if await self.app.db_call(self.store.uncertain_effects, job):
                raise McpEffectUncertain("An effect still lacks an execution receipt")
            message = answer if isinstance(answer, Message) else Message(str(answer))
            if not message or plan_reply(message.extract_plain_text()).silence:
                message = Message("外部工具已返回回执，本次没有补充回复。具体执行状态可在控制台查看。")
            await execution.guard()
            body = decode_onebot_message(message).body
            identifier = await self.app.db_call(self.store.queue_result, job, body)
            return {"delivery_id": identifier}
        except McpApprovalPending:
            raise JobDeferred("MCP 续跑等待下一次工具审批", 15) from None
        except McpDeferred as exc:
            raise JobDeferred(str(exc), 30) from None
        except McpEffectUncertain:
            return await self._notice(job, "续跑已停止：有操作缺少执行回执，无法确认是否完成，因此没有重复执行。请在控制台核对；本次任务不能算完成。")
        except ModelBudgetExceeded:
            return await self._notice(job, "续跑达到时间或模型预算上限，尚未全部完成。已保存执行回执，不会自动重复已执行的操作。")
        except LeaseLost as exc:
            raise asyncio.CancelledError("MCP continuation lease lost") from exc
        finally:
            active_job_fence.reset(token)

    async def _notice(self, job, text):
        identifier = await self.app.db_call(self.store.queue_result, job, MessageBody((TextNode(0, text),)))
        return {"delivery_id": identifier, "incomplete": True}
