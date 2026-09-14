"""Reconstruct authorized tool environments from durable tasks, never Python closures."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from nonebot import get_bot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent, Message, MessageSegment

from .control import JobFence, LeaseLost, active_job_fence
from .workspaces import StepWorkspaces, prune_acknowledged_artifacts
from .workspace_cleanup import prune_task_workspaces
from ..onebot_codec import scope_from_event, decode_onebot_message
from ..onebot_availability import file_delivery_blocker
from ..deepseek import DeepSeekTrace
from ..workers.durable_jobs import DurableJobWorker, JobDeferred
from .external import poll_external
from ..delivery import body_fingerprint
from .file_outbox import attempt_file
from ..tool_policy import tool_enabled


class ContinuationCancelled(Exception):
    pass


class SubAgentDispatcher:
    kind = "subagent.workflow"

    def __init__(self, services):
        self.services = services
        self.context = services.context
        self.store = self.context.subagent_store
        self.jobs = self.context.job_store
        self.coordinator = self.context.subagent_coordinator
        self.worker = DurableJobWorker(self.jobs, logger=self.context.logger, concurrency=2, per_scope_limit=1)
        self.worker.register(self.kind, self.execute, compensator=self.settle_failed_dispatch)
        self._next_artifact_prune = 0.0
        self._next_message_reconcile = 0.0

    @staticmethod
    def restore_event(dispatch):
        model = GroupMessageEvent if dispatch["event"].get("message_type") == "group" else PrivateMessageEvent
        event = model.model_validate(dispatch["event"])
        if str(event.self_id) != str(dispatch["bot_id"]):
            raise ValueError("Task bot identity changed")
        return event

    def enqueue_final(self, task_id):
        control = self.store.control(task_id)
        task = self.store.get(task_id)
        if self.store.control(task_id)["revision"] != control["revision"]:
            return None
        if task is None or task.status not in {"completed", "partial", "failed", "cancelled"}:
            return None
        event = self.restore_event(control["dispatch"])
        if scope_from_event(event).key != task.scope_key or event.user_id != task.requester_user_id:
            raise ValueError("Final delivery envelope does not match the task owner")
        if self.context.delivery_store is None:
            raise RuntimeError("Durable delivery outbox is required for background tasks")
        text = str(task.result.get("answer") or task.result.get("result", {}).get("summary") or task.last_error or task.status)
        if task.status in {"failed", "cancelled"}:
            text = ("任务已取消。" if task.status == "cancelled" else "任务未完成。") + (task.last_error or text)
        body = decode_onebot_message(Message(MessageSegment.text(f"{task.handle}\n{text}"))).body
        delivery, _ = self.context.delivery_store.enqueue(
            idempotency_key=f"subagent-final:{task_id}:{control['revision']}",
            source_scope_key=task.scope_key, source_canonical_message_id=task.trigger_message_id,
            target_scope=scope_from_event(event), body=body, reply_to_native_message_id=str(event.message_id))
        # The marker follows the outbox commit. A crash in between safely repeats enqueue.
        self.store.mark_final_queued(task_id, control["revision"])
        return delivery

    async def settle_failed_dispatch(self, job, reason: str):
        if reason != "failed":
            return
        task_id = int(job.payload["task_id"])
        task = self.store.get(task_id)
        if (task is None or task.scope_key != job.scope_key
                or self.store.control(task_id)["revision"] != int(job.payload["revision"])
                or task.status in {"completed", "partial", "failed", "cancelled"}):
            return
        def owns_lease():
            current = self.jobs.get(job.job_id)
            return bool(current and current.status == "running" and current.lease_owner == job.lease_owner
                        and current.attempts == job.attempts and (current.lease_until or 0) > time.time())
        token = active_job_fence.set(JobFence(job.job_id, job.lease_owner, job.attempts, owns_lease))
        try:
            error = "后台任务多次执行失败，已停止；修复连接或权限后可重新提交或修订任务。"
            self.store.settle_unfinished_runs(task_id, running_status="failed", pending_status="skipped", error=error)
            self.store.set_task_state(task_id, "failed", error=error)
        except LeaseLost:
            pass
        finally:
            active_job_fence.reset(token)

    def enqueue(self, task_id: int):
        task = self.store.get(task_id)
        control = self.store.control(task_id)
        return self.jobs.enqueue(kind=self.kind, idempotency_key=f"subagent:{task_id}:revision:{control['revision']}",
            scope_key=task.scope_key, payload={"task_id": task_id, "revision": control["revision"]}, max_attempts=5,
            resume_on_lease_loss=True)

    async def run_forever(self):
        async def reconcile_queue():
            while True:
                try:
                    await self.reconcile_once()
                except Exception as exc:
                    # A failed scan must not cancel the independent execution worker.
                    self.context.logger.error(
                        f"Sub-Agent queue reconciliation failed: {type(exc).__name__}; retrying in 10s."
                    )
                await asyncio.sleep(10)
        async with asyncio.TaskGroup() as group:
            group.create_task(reconcile_queue())
            group.create_task(self.worker.run_forever())

    async def reconcile_once(self):
        for task_id in await asyncio.to_thread(self.store.dispatchable_tasks):
            try:
                await asyncio.to_thread(self.enqueue, task_id)
            except Exception as exc:
                self.context.logger.warning(f"task#{task_id} queue reconciliation deferred: {type(exc).__name__}")
        for task_id in await asyncio.to_thread(self.store.finalizable_tasks):
            try:
                await asyncio.to_thread(self.enqueue_final, task_id)
            except Exception as exc:
                self.context.logger.warning(f"task#{task_id} final outbox reconciliation deferred: {type(exc).__name__}")
        for task_id in await asyncio.to_thread(self.store.queued_file_tasks):
            try:
                await self.deliver_queued_files(task_id)
            except Exception as exc:
                self.context.logger.warning(f"task#{task_id} file outbox deferred: {type(exc).__name__}")
        for task_id in await asyncio.to_thread(self.store.uncertain_deliveries):
            try:
                await self.reconcile(task_id)
            except Exception as exc:
                self.context.logger.warning("Sub-Agent delivery reconciliation failed for task#%s: %s", task_id, type(exc).__name__)
        for task_id in await asyncio.to_thread(self.store.unsettled_file_receipts):
            try:
                await asyncio.to_thread(self.settle_file_receipts, task_id)
            except Exception as exc:
                self.context.logger.warning(f"task#{task_id} file receipt settlement deferred: {type(exc).__name__}")
        for task_id in await asyncio.to_thread(self.store.rejected_file_tasks):
            try:
                await asyncio.to_thread(self.notify_rejected_files, task_id)
            except Exception as exc:
                self.context.logger.warning(f"task#{task_id} file failure notice deferred: {type(exc).__name__}")
        now = time.time()
        if now >= self._next_message_reconcile:
            try:
                await self.reconcile_final_messages()
            except Exception as exc:
                self.context.logger.warning(f"Final message reconciliation deferred: {type(exc).__name__}")
            self._next_message_reconcile = now + 30
        if now >= self._next_artifact_prune:
            try:
                await self.prune_workspaces()
            except Exception as exc:
                self.context.logger.warning("Task workspace cleanup failed; retained for retry: %s", type(exc).__name__)
            self._next_artifact_prune = now + 30

    async def reconcile_final_messages(self):
        """Confirm lost receipts from matching self-messages, never resend on absence."""
        outbox = self.context.delivery_store
        if outbox is None:
            return 0
        histories = {}
        matched = 0
        for delivery in outbox.ambiguous_finals():
            try:
                task_id = int(delivery.idempotency_key.split(":")[1])
                dispatch = self.store.control(task_id)["dispatch"]
                event = self.restore_event(dispatch)
                if not isinstance(event, GroupMessageEvent) or scope_from_event(event).key != delivery.target_scope.key:
                    continue
                bot_id = str(dispatch["bot_id"])
                key = (bot_id, event.group_id)
                if key not in histories:
                    # One query per group per pass; an unavailable history is not proof of non-delivery.
                    histories[key] = []
                    bot = get_bot(bot_id)
                    response = await asyncio.wait_for(bot.call_api("get_group_msg_history",
                        group_id=event.group_id, count=100, reverse_order=False), timeout=15)
                    histories[key] = response.get("messages", []) if isinstance(response, dict) else response
                for raw in histories[key] or []:
                    if not isinstance(raw, dict):
                        continue
                    sender = raw.get("user_id") or (raw.get("sender") or {}).get("user_id")
                    if str(sender) != bot_id or str(raw.get("group_id", event.group_id)) != str(event.group_id):
                        continue
                    timestamp = int(raw.get("time") or 0)
                    if not delivery.created_at - 5 <= timestamp <= time.time() + 60 or not raw.get("message_id"):
                        continue
                    decoded = decode_onebot_message(raw.get("message", []))
                    if body_fingerprint(decoded.body, decoded.reply_to_native_message_id or "") != body_fingerprint(delivery.body, delivery.reply_to_native_message_id):
                        continue
                    result = outbox.reconcile_echo(delivery.target_scope, decoded.body,
                        native_message_id=raw["message_id"], reply_to_native_message_id=decoded.reply_to_native_message_id,
                        observed_at=timestamp, window_seconds=max(timestamp - delivery.created_at + 60, 600))
                    if result is not None:
                        matched += 1
                    break
            except (KeyError, ValueError):
                continue
            except Exception as exc:
                self.context.logger.warning(f"{delivery.handle} receipt lookup deferred: {type(exc).__name__}")
        return matched

    def notify_rejected_files(self, task_id: int):
        control = self.store.control(task_id)
        task = self.store.get(task_id)
        if task is None or not control["dispatch"] or self.context.delivery_store is None:
            return
        event = self.restore_event(control["dispatch"])
        if scope_from_event(event).key != task.scope_key or event.user_id != task.requester_user_id:
            raise ValueError("File failure notice does not match task owner")
        for delivery in self.store.deliveries(task_id):
            if delivery["revision"] != control["revision"] or delivery["state"] != "rejected":
                continue
            phase = f"file_rejected:{control['revision']}:{delivery['key']}"
            if any(item["phase"] == phase for item in self.store.checkpoints(task_id)):
                continue
            name = delivery["payload"].get("filename") or "附件"
            body = decode_onebot_message(Message(MessageSegment.text(
                f"{task.handle} 文件未送达：{name}。文件准备或上传失败，自动重试已停止。"
                "这不是成功交付；具体原因见控制台文件记录。"))).body
            notice, _ = self.context.delivery_store.enqueue(
                idempotency_key=f"subagent-final:{task_id}:{control['revision']}:{phase}",
                source_scope_key=task.scope_key, source_canonical_message_id=task.trigger_message_id,
                target_scope=scope_from_event(event), body=body, reply_to_native_message_id=str(event.message_id))
            self.store.append_checkpoint(task_id, phase, {"delivery_id": notice.delivery_id})

    async def deliver_queued_files(self, task_id: int):
        from ..agent_tools import AgentToolExecutor
        task = self.store.get(task_id)
        control = self.store.control(task_id)
        if task is None or task.cancel_requested or not control["dispatch"] or not tool_enabled("send_file_from_sandbox"):
            return
        event = self.restore_event(control["dispatch"])
        if scope_from_event(event).key != task.scope_key or event.user_id != task.requester_user_id:
            raise ValueError("File outbox does not match the original task owner")
        if not isinstance(event, GroupMessageEvent) or not self.services.group_enabled(event.group_id):
            return
        # No claim is made while QQ is disconnected, so reconnect can safely retry.
        bot = get_bot(str(control["dispatch"]["bot_id"]))
        executor = AgentToolExecutor(bot=bot, event=event, owner=task.conversation_id,
            sandbox_manager=self.context.sandbox_manager, max_file_bytes=self.context.settings.sandbox_max_file_bytes,
            scope=scope_from_event(event))
        workspaces = StepWorkspaces(self.context.state_dir, executor,
            retention_seconds=self.context.settings.subagent_retention_seconds)
        for delivery in self.store.deliveries(task_id):
            if delivery["revision"] != control["revision"] or delivery["state"] != "queued":
                continue
            async with asyncio.timeout(120):
                await attempt_file(self.store, task_id, delivery,
                    prepare=lambda item: workspaces.prepare_delivery(task_id, item), send=executor.send_file_content,
                    readiness=executor.file_delivery_blocker)

    async def prune_workspaces(self):
        root = self.context.state_dir / "subagent_artifacts"
        reclaimed, deferred = await prune_task_workspaces(root, self.context.sandbox_manager, self.coordinator)

        def can_prune(task_id, digest):
            task = self.store.get(task_id)
            if task is None or task.status != "completed":
                return False
            ready, digests = self.coordinator._artifact_retention_state(task)
            return ready and digest in digests

        deleted, invalid = await asyncio.to_thread(prune_acknowledged_artifacts, root, can_prune=can_prune)
        if reclaimed or deleted or deferred or invalid:
            self.context.logger.info("Task cleanup: %s sandboxes, %s legacy snapshots reclaimed; %s deferred",
                reclaimed, deleted, deferred + invalid)

    async def reconcile(self, task_id: int):
        control = self.store.control(task_id)
        dispatch = control["dispatch"]
        if not dispatch:
            return {"matched": 0}
        event = self.restore_event(dispatch)
        task = self.store.get(task_id)
        if scope_from_event(event).key != task.scope_key or event.user_id != task.requester_user_id:
            raise ValueError("Invalid dispatch scope")
        bot = get_bot(str(dispatch["bot_id"]))
        if not isinstance(event, GroupMessageEvent):
            return {"matched": 0}
        blocker = await file_delivery_blocker(bot)
        if blocker is not None:
            return {"matched": 0, "state": "deferred", "error": blocker}
        response = await bot.call_api("get_group_root_files", group_id=event.group_id)
        files = response.get("files", []) if isinstance(response, dict) else []
        matched = 0
        for delivery in self.store.deliveries(task_id):
            if delivery["state"] not in {"sending", "unknown"}:
                continue
            payload = delivery["payload"]
            not_before = int(payload.get("upload_started_at") or 0)
            found = next((f for f in files if f.get("file_name") == payload.get("filename")
                and int(f.get("file_size", -1)) == int(payload.get("size", -2))
                and str(f.get("uploader")) == str(bot.self_id)
                and (not not_before or not f.get("upload_time")
                     or int(f.get("upload_time") or 0) >= not_before - 5)), None)
            updated = {**payload, "ok": bool(found), "reconciled": bool(found)}
            if found:
                updated["file_id"] = found.get("file_id")
                updated.update(state="acknowledged", error="",
                    receipt={"ok": True, "reconciled": True, "file_id": found.get("file_id")})
            changed = self.store.finish_delivery(task_id, delivery["key"], "acknowledged" if found else "unknown", updated,
                revision=delivery["revision"], expected_payload=payload)
            if found and changed:
                matched += 1
        if matched:
            task = self.store.get(task_id)
            if task is not None:
                lifecycle_executor = SimpleNamespace(
                    owner=task.conversation_id,
                    base_owner=task.conversation_id,
                    sandbox_manager=self.context.sandbox_manager,
                )
                workspaces = StepWorkspaces(
                    self.context.state_dir,
                    lifecycle_executor,
                    retention_seconds=self.context.settings.subagent_retention_seconds,
                )
                retention_ready, artifact_digests = self.coordinator._artifact_retention_state(task)
                if retention_ready:
                    await workspaces.finalize_task(
                        task_id,
                        self.store.runs(task_id),
                        artifact_digests=artifact_digests,
                        cleanup_revision=control["revision"] if task.status == "completed" else None,
                        finished_at=task.finished_at if task.status == "completed" else None,
                    )
                    self.store.append_event(
                        task_id,
                        "task.workspace_retained",
                        {
                            "reason": "artifact_delivery_reconciled",
                            "containers": "stopped",
                            "workspace": "retained",
                        },
                    )
        return {"matched": matched}

    def settle_file_receipts(self, task_id: int):
        control = self.store.control(task_id)
        task = self.store.get(task_id)
        event = self.restore_event(control["dispatch"])
        if scope_from_event(event).key != task.scope_key or event.user_id != task.requester_user_id:
            raise ValueError("File receipt envelope does not match the task owner")
        summary = self.store.sync_file_receipt_summary(task_id, control["revision"])
        if summary is None:
            return None
        delivery = None
        if summary["notice_needed"]:
            outbox = getattr(self.context, "delivery_store", None)
            if outbox is None:
                return None
            names = [str(item.get("filename") or "文件") for item in summary["confirmed"]]
            text = (f"{task.handle}（第 {control['revision']} 版）文件回执更新："
                f"QQ 群文件已确认收到 {len(names)} 个文件，无需重复上传。\n"
                + "\n".join(names[:5]) + "\n其他未完成事项仍以原报告为准。")
            body = decode_onebot_message(Message(MessageSegment.text(text))).body
            delivery, _ = outbox.enqueue(
                idempotency_key=f"subagent-final:{task_id}:{control['revision']}:file-receipts",
                source_scope_key=task.scope_key, source_canonical_message_id=task.trigger_message_id,
                target_scope=scope_from_event(event), body=body,
                reply_to_native_message_id=str(event.message_id))
        # Persist this marker after enqueue; a crash between them repeats the same outbox key.
        self.store.append_checkpoint(task_id, f"artifact_receipts_settled:{control['revision']}",
            {"revision": control["revision"], "delivery_id": delivery.delivery_id if delivery else None})
        return delivery

    async def execute(self, job):
        task_id = int(job.payload["task_id"])
        task = self.store.get(task_id)
        control = self.store.control(task_id)
        if task is None or job.scope_key != task.scope_key or control["revision"] != int(job.payload["revision"]):
            return {"state": "obsolete"}
        dispatch = control["dispatch"]
        event = self.restore_event(dispatch)
        if scope_from_event(event).key != task.scope_key or event.user_id != task.requester_user_id:
            raise ValueError("Task dispatch envelope does not match its owner")
        def owns_lease():
            current = self.jobs.get(job.job_id)
            return bool(current and current.status == "running" and current.lease_owner == job.lease_owner
                        and current.attempts == job.attempts and (current.lease_until or 0) > time.time())

        fence = JobFence(job.job_id, job.lease_owner, job.attempts, owns_lease)
        token = active_job_fence.set(fence)
        try:
            fence.assert_owned()
            if task.cancel_requested and task.status != "cancelled":
                self.store.settle_unfinished_runs(task_id, running_status="cancelled", pending_status="skipped", error="用户取消任务")
                self.store.set_task_state(task_id, "cancelled", error="用户取消任务；已提交的远程作业不代表已取消，可在控制台核对。")
                task = self.store.get(task_id)
            if task.status not in {"completed", "partial", "failed", "cancelled"}:
                remaining = float(dispatch.get("deadline", time.time() + self.coordinator.timeout_seconds)) - time.time()
                try:
                    if remaining <= 0:
                        raise TimeoutError
                    try:
                        bot = get_bot(str(dispatch["bot_id"]))
                    except KeyError:
                        raise JobDeferred("QQ 未连接，恢复连接后继续任务", 30) from None
                    if isinstance(event, GroupMessageEvent) and not self.services.group_enabled(event.group_id):
                        raise JobDeferred("群已停用，暂停执行", 30)
                    external = self.store.external_calls(task_id)
                    if any(c["status"] == "pending" for c in external):
                        client = getattr(self.context, "fleet_client", None)
                        try:
                            ready = client is not None and await asyncio.wait_for(poll_external(self.store, task, client), timeout=min(remaining, 240))
                        except TimeoutError:
                            if remaining <= 240:
                                raise
                            ready = False
                        if self.store.cancellation_requested(task_id):
                            raise ContinuationCancelled()
                        if not ready:
                            self.store.set_task_state(task_id, "waiting_external")
                            raise JobDeferred("等待远程作业结果", 15)
                    if self.store.cancellation_requested(task_id):
                        raise ContinuationCancelled()
                    if task.status not in {"queued", "interrupted"}:
                        self.store.interrupt_task(task_id)
                    remaining = float(dispatch.get("deadline", time.time() + self.coordinator.timeout_seconds)) - time.time()
                    trace = DeepSeekTrace(trace_id=task.trace_id)
                    async with asyncio.timeout(remaining):
                        await self.services.tools._ask_ai(bot, event, task.objective,
                            selected_model_override=dispatch.get("profile"), turn_trace=trace,
                            resume_task_id=task_id)
                except ContinuationCancelled:
                    self.store.settle_unfinished_runs(task_id, running_status="cancelled", pending_status="skipped", error="用户取消任务")
                    self.store.set_task_state(task_id, "cancelled", error="用户取消任务；已提交的远程作业需另行核对。")
                except TimeoutError:
                    self.store.settle_unfinished_runs(task_id, running_status="failed", pending_status="skipped", error="任务总时限已到")
                    self.store.set_task_state(task_id, "failed", error="任务总时限已到，停止自动接续；已提交的远程作业可能仍在运行，请在控制台核对。已完成结果保留。")
                task = self.store.get(task_id)
                if task.status == "waiting_external":
                    raise JobDeferred("等待远程作业结果", 15)
                if task.status not in {"completed", "partial", "failed", "cancelled"}:
                    raise RuntimeError("Task did not reach a settled state; retaining it for takeover")
            fence.assert_owned()
            delivery = self.enqueue_final(task_id)
            if delivery is None:
                raise JobDeferred("任务修订已变化，重新核对投递", 0)
            self.store.append_event(task_id, "task.final_delivery_queued", {"delivery_id": delivery.delivery_id, "revision": control["revision"]})
            return {"task_id": task_id, "status": task.status, "delivery_id": delivery.delivery_id}
        except LeaseLost:
            raise asyncio.CancelledError("worker lease lost")
        finally:
            active_job_fence.reset(token)
