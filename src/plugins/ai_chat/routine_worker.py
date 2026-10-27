"""Execute an owned scheduled Agent turn and durably hand its reply to Outbox."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import time
from typing import TYPE_CHECKING, Awaitable, Callable

from nonebot import get_bot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, PrivateMessageEvent

from .agent.control import JobFence, LeaseLost, active_job_fence
from .agent.execution import active_agent_step
from .agent.preview_receipts import routine_preview_delivery_note
from .agent_loop import AgentTrace
from .model_budget import ModelBudgetExceeded, ModelCallBudget, model_budget_scope
from .onebot_codec import decode_onebot_message
from .onebot_availability import delivery_blocker
from .output_planner import plan_reply
from .routines import (
    ROUTINE_JOB_KIND,
    RoutineEffectUncertain,
    RoutineLeaseLost,
    RoutineRun,
    RoutineDeferred,
)
from .routine_subagents import TERMINAL, recovery_context
from .tool_policy import policy_for_tool
from .workers.durable_jobs import JobDeferred

if TYPE_CHECKING:
    from .handler_services import HandlerServices
    from .storage.jobs import DurableJob


@dataclass
class RoutineExecution:
    services: HandlerServices
    job: DurableJob
    run: RoutineRun
    bot: object | None = None
    _membership_checked_at: float = field(default=0, init=False)

    async def check_membership(self) -> None:
        if (self.run.spec.kind != "group" or self.bot is None
                or time.monotonic() - self._membership_checked_at < 30):
            return
        try:
            async with asyncio.timeout(10):
                members = await self.bot.get_group_member_list(
                    group_id=int(self.run.spec.conversation_id), no_cache=True)
            if not isinstance(members, list) or any(
                not isinstance(member, dict) or not str(member.get("user_id", "")).isdigit()
                for member in members
            ):
                raise ValueError("Invalid group roster")
        except Exception:
            raise RoutineDeferred("无法核实例行任务创建者的群成员身份，稍后重试") from None
        if not any(str(member["user_id"]) == self.run.spec.creator_id for member in members):
            app = self.services.context
            try:
                await app.db_call(app.routine_store.cancel, self.run.spec.scope, self.run.routine_id,
                                  expected_version=self.run.spec.version)
            except ValueError:
                pass  # An edit already invalidated this execution's version.
            raise LeaseLost("Routine creator is no longer a member of its group")
        self._membership_checked_at = time.monotonic()

    async def guard(self) -> None:
        app = self.services.context
        owned = await app.db_call(
            app.routine_store.owns_attempt, self.job, self.run.attempt_id
        )
        if not owned:
            raise LeaseLost("Routine attempt lost its lease or was cancelled")
        if self.run.deadline_at is not None and time.time() >= self.run.deadline_at:
            raise TimeoutError("Routine execution deadline expired")
        if self.run.spec.kind == "group" and not await app.db_call(
            self.services.group_enabled, int(self.run.spec.conversation_id)
        ):
            raise LeaseLost("Routine conversation was disabled during execution")
        await self.check_membership()

    async def tool(
        self, name: str, arguments: dict, action: Callable[[], Awaitable[str]]
    ) -> str:
        await self.guard()
        # Child orchestration has its own persisted task/checkpoint receipts.
        # A wrapper "started" effect would incorrectly block recovery after a yield.
        if name in {"run_subagents", "delegate_agent", "resume_subagent"} or active_agent_step.get() is not None:
            return await action()
        if policy_for_tool(name).idempotency in {"pure", "idempotent"}:
            return await action()
        app = self.services.context
        try:
            key, saved = await app.db_call(
                app.routine_store.claim_effect,
                self.job,
                self.run.attempt_id,
                name,
                arguments,
            )
            if saved is not None:
                return saved
            result = await action()
            await app.db_call(
                app.routine_store.finish_effect,
                self.job,
                self.run.attempt_id,
                key,
                result,
            )
            return result
        except RoutineLeaseLost as exc:
            raise LeaseLost(str(exc)) from exc


def routine_event(run: RoutineRun):
    # Not a fabricated incoming QQ message: the negative ID must not match ledger
    # messages or cause the final delivery to quote an unrelated user message.
    common = dict(
        time=int(time.time()),
        self_id=int(run.spec.bot_id),
        post_type="message",
        user_id=int(run.spec.creator_id),
        message_id=-int(run.run_id[:12], 16),
        message=Message(run.spec.prompt),
        original_message=Message(run.spec.prompt),
        raw_message=run.spec.prompt,
        font=0,
        sender={"user_id": int(run.spec.creator_id)},
        to_me=True,
    )
    if run.spec.kind == "group":
        return GroupMessageEvent(
            **common,
            message_type="group",
            sub_type="normal",
            group_id=int(run.spec.conversation_id),
        )
    return PrivateMessageEvent(**common, message_type="private", sub_type="friend")


class RoutineDispatcher:
    def __init__(self, services: HandlerServices):
        self.services = services
        self.app = services.context
        self.store = self.app.routine_store
        self.app.job_worker.register(
            ROUTINE_JOB_KIND,
            self.execute,
            timeout_seconds=self.app.settings.routine_timeout_seconds + 30,
        )

    async def run_forever(self) -> None:
        while True:
            try:
                await self.app.db_call(self.store.reconcile_terminal_jobs)
                if self.app.subagent_store is not None:
                    await self.app.db_call(self.app.subagent_store.reconcile_routine_parents)
                await self.app.db_call(self.store.queue_terminal_notices)
                await self.app.db_call(self.store.materialize_due)
            except Exception as exc:
                self.app.logger.error(f"Routine scheduler failed: {type(exc).__name__}")
            await asyncio.sleep(self.app.settings.routine_check_seconds)

    async def execute(self, job):
        run = await self.app.db_call(
            self.store.execution_run, job
        )
        if run.status == "quiet":
            return {"state": "quiet", "recovered": True}
        if run.status in {"generated", "enqueued"}:
            delivery_id = await self.app.db_call(self.store.queue_result, job)
            return {"delivery_id": delivery_id, "recovered": True}
        if run.status in {"failed", "skipped"}:
            await self.app.db_call(self.store.queue_terminal_notices)
            return {"state": run.status, "recovered": True}
        if run.deadline_at is not None and run.deadline_at <= time.time():
            await self.app.db_call(self.store.begin_attempt, job)
            await self.app.db_call(self.store.queue_terminal_notices)
            return {"state": "failed", "reason": "execution_deadline"}
        try:
            bot = get_bot(run.spec.bot_id)
        except KeyError:
            raise JobDeferred("例行任务的原 QQ 账号未连接", 30) from None
        if str(bot.self_id) != run.spec.bot_id:
            raise ValueError("Routine bot identity does not match its owner")
        blocker = await delivery_blocker(bot)
        if blocker:
            raise JobDeferred(blocker, 30)
        if run.spec.kind == "group" and not await self.app.db_call(
            self.services.group_enabled, int(run.spec.conversation_id)
        ):
            raise JobDeferred("群已停用，暂停例行任务", 30)
        execution = RoutineExecution(self.services, job, run, bot=bot)
        try:
            await execution.check_membership()
        except RoutineDeferred as exc:
            raise JobDeferred(str(exc), exc.delay_seconds) from None
        except LeaseLost:
            return {"state": "cancelled", "reason": "creator_left"}
        run = await self.app.db_call(
            self.store.begin_attempt, job,
            timeout_seconds=self.app.settings.routine_timeout_seconds,
        )
        if run.status == "skipped":
            return {"state": "skipped", "reason": "daily_budget"}
        if run.status == "failed":
            await self.app.db_call(self.store.queue_terminal_notices)
            return {"state": "failed", "reason": "execution_deadline"}

        execution.run = run
        budget = ModelCallBudget(run.spec.max_tokens, before_request=execution.guard)
        trace = AgentTrace()
        turn = None
        status = "completed"
        interrupted_status = None
        fence_token = active_job_fence.set(JobFence(job.job_id, job.lease_owner, job.attempts,
            lambda: self.store.owns_attempt(job, run.attempt_id)))
        try:
            receipts = await self.app.db_call(self.store.recovery_receipts, job)
            event = routine_event(run)
            conversation = self.services.chat._conversation_id(event)
            profile = await self.app.db_call(
                self.services.chat._preferred_model_profile, conversation
            )
            turn = await self.app.db_call(
                self.app.turn_journal.start_turn,
                run.spec.scope,
                trigger_canonical_message_id=None,
                objective=run.spec.prompt,
                provider=profile.provider,
                model=profile.model,
                profile=profile.name,
            )
            await self.app.db_call(
                self.store.bind_turn, job, run.attempt_id, turn.turn_id
            )
            with model_budget_scope(budget):
                async with asyncio.timeout(max(0, run.deadline_at - time.time())):
                    for child in await self.app.db_call(self.store.child_receipts, job):
                        if child["status"] not in TERMINAL:
                            await self.services.tools._ask_ai(
                                bot, event, child["objective"], journal_turn_id=turn.turn_id,
                                turn_trace=trace, selected_profile_override=profile,
                                available_image_sources=[], resume_task_id=child["task_id"],
                                routine_execution=execution,
                            )
                    children = await self.app.db_call(self.store.child_receipts, job)
                    answer = await self.services.tools._ask_ai(
                        bot,
                        event,
                        run.spec.prompt,
                        journal_turn_id=turn.turn_id,
                        turn_trace=trace,
                        selected_profile_override=profile,
                        available_image_sources=[],
                        turn_context=recovery_context(receipts, children),
                        routine_execution=execution,
                    )
        except (RoutineLeaseLost, LeaseLost) as exc:
            interrupted_status = "aborted"
            raise asyncio.CancelledError("routine lease lost") from exc
        except JobDeferred:
            interrupted_status = "aborted"
            raise
        except RoutineDeferred as exc:
            interrupted_status = "aborted"
            raise JobDeferred(str(exc), exc.delay_seconds) from None
        except RoutineEffectUncertain:
            status = "failed"
            answer = f"{run.spec.handle}「{run.spec.name}」已暂停本次执行：有操作缺少执行回执，无法确认是否已完成，因此没有重复操作。请在控制台核对。"
        except ModelBudgetExceeded:
            status = "failed"
            answer = f"{run.spec.handle}「{run.spec.name}」本次模型预算已用完，任务未全部完成。已执行操作保留，下次仍按计划运行。"
        except TimeoutError:
            status = "failed"
            answer = f"{run.spec.handle}「{run.spec.name}」本次执行超时，尚未全部完成；已发出的远程操作可能仍在运行，请核对回执。"
        except BaseException as exc:
            interrupted_status = "aborted" if isinstance(exc, asyncio.CancelledError) else "crashed"
            raise
        finally:
            active_job_fence.reset(fence_token)
            usage = budget.summary()
            try:
                await self.app.db_call(
                    self.store.usage.settle_routine,
                    run.attempt_id,
                    provider=",".join(usage["providers"]) or "none",
                    model=",".join(usage["models"]) or "none",
                    input_tokens=usage["input_tokens"],
                    output_tokens=usage["output_tokens"],
                    cached_tokens=usage["cached_tokens"],
                    usage_known=usage["usage_known"],
                )
            finally:
                if turn is not None and interrupted_status is not None:
                    try:
                        await self.app.db_call(
                            self.app.turn_journal.finish_turn, turn.turn_id,
                            status=interrupted_status, trace_payload=trace.to_payload(),
                            input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
                            total_tokens=usage["input_tokens"] + usage["output_tokens"],
                        )
                    except Exception as exc:
                        self.app.logger.error(f"Routine turn finalization failed: {type(exc).__name__}")
        message = answer if isinstance(answer, Message) else Message(str(answer))
        children = await self.app.db_call(self.store.child_receipts, job)
        if children:
            preview_note = await self.app.db_call(routine_preview_delivery_note,
                self.app.subagent_store, run, children)
            if preview_note:
                message += "\n\n" + preview_note
        quiet = status == "completed" and all(segment.type == "text" for segment in message) and plan_reply(
            message.extract_plain_text()).silence
        body = decode_onebot_message(message).body
        await self.app.db_call(
            self.store.complete_execution, job, run.attempt_id,
            journal=self.app.turn_journal,
            turn_id=turn.turn_id if turn else None,
            status="silence" if quiet else ("succeeded" if status == "completed" else "aborted"),
            body=None if quiet else body,
            final_text="" if quiet else message.extract_plain_text(),
            trace_payload=trace.to_payload(),
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
        )
        if quiet:
            return {"state": "quiet", "delivery_id": None}
        delivery_id = await self.app.db_call(self.store.queue_result, job)
        return {"state": status, "delivery_id": delivery_id}
