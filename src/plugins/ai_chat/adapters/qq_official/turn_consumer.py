"""Existing durable-job handler for shared chat/command execution."""
from __future__ import annotations

import asyncio
import time

from src.plugins.ai_chat.agent.control import active_job_fence, LeaseLost as ToolLeaseLost
from src.plugins.ai_chat.adapters.qq_official.lease import LeaseLost
from src.plugins.ai_chat.platform_types import SendRequest
from src.plugins.ai_chat.workers.durable_jobs import JobDeferred

from .commands import route_command
from .turn_lifecycle import OfficialTurnLifecycle
from .turn_input import load_turn_input
from .turn_platform import ExecutionBinding, OfficialTurnPlatform


class OfficialTurnConsumer:
    """One account owner; existing queue leases remain the sole scheduler.

    Only a prepared execution may enter the shared orchestrator. Every retry
    first reads durable execution state; result_ready retries only its handoff.
    Runtime callbacks and stores are trusted local objects, never model input.
    """

    def __init__(self, *, api, token, store, outbox, journal, database_calls,
                 orchestrator, command_services, transport_state, usage=None,
                 sender_wake=None, policy=None, clock=time.time,
                 model_timeout_seconds=600):
        if type(model_timeout_seconds) is not int or not 1 <= model_timeout_seconds <= 3600:
            raise ValueError("invalid_model_timeout")
        self.api, self.token, self.store, self.outbox = api, token, store, outbox
        self.journal, self._calls, self.orchestrator = journal, database_calls, orchestrator
        self.command_services, self.transport_state, self.usage = command_services, transport_state, usage
        self.sender_wake, self.policy, self.clock = sender_wake, policy, clock
        self.model_timeout_seconds = model_timeout_seconds

    def _prepare(self, job):
        now = int(self.clock())
        with self.outbox.deliveries.transaction() as cursor:
            # claim_execution performs the authoritative full job/source fence.
            # These reads only select the immutable deadline and parser branch.
            row = cursor.execute("""SELECT i.received_at,i.turn_text,i.turn_text_version,x.deadline_at,x.execution_kind
                FROM qq_official_inbound_events i JOIN messages m ON m.canonical_message_id=i.canonical_message_id
                LEFT JOIN qq_official_turn_executions x ON x.inbox_id=i.inbox_id
                WHERE i.inbox_id=? AND i.account_key=? AND i.dispatch_job_id=?""",
                (job.payload.get("inbox_id"), self.token.account_key, job.job_id)).fetchone()
            if row is None:
                raise self.api.ExecutionError("turn_source_unavailable")
            if row["turn_text_version"] != 1 or not isinstance(row["turn_text"], str):
                raise self.api.ExecutionError("turn_input_unavailable")
            text = row["turn_text"]
            kind = "command" if text.lstrip().startswith(("/", "!")) else "chat"
            if row["execution_kind"] is not None and row["execution_kind"] != kind:
                raise self.api.ExecutionError("turn_parser_identity_conflict")
            deadline = row["deadline_at"] or row["received_at"] + self.outbox.max_wait_seconds
            decision = self.api.claim_execution(cursor, token=self.token, job=job, now=now,
                deadline_at=deadline, admission=self.store.admitted, execution_kind=kind)
            if decision.handle is None:
                return decision, None, None
            inbound = load_turn_input(cursor, execution_api=self.api, token=self.token,
                handle=decision.handle, job=job, now=now, admission=self.store.admitted)
            return decision, inbound, text

    def _result(self, life):
        with self.outbox.deliveries.transaction() as cursor:
            row = self.api.assert_execution_current(cursor, **life.args())
            body = self.api.load_result(cursor, **life.args())
            if row["result_status"] in {"silence", "aborted", "cancelled"}:
                self.api.complete_without_plan(cursor, **life.args())
                return None
            return body

    async def __call__(self, job):
        try:
            return await self._execute(job)
        except (ToolLeaseLost, LeaseLost):
            raise JobDeferred("official_turn_owner_lost", 3) from None
        except self.api.ExecutionFenceLost:
            raise JobDeferred("official_turn_fence_unavailable", 3) from None
        except self.api.ExecutionError as exc:
            if str(exc) in {"turn_trigger_unavailable", "turn_source_unavailable", "turn_input_unavailable", "turn_text_unavailable"}:
                return {"status": "rejected", "reason": str(exc)}
            raise JobDeferred("official_turn_evidence_unavailable", 3) from None
        except (JobDeferred, asyncio.CancelledError):
            raise
        except Exception:
            # DB/model exceptions may contain private input or opaque IDs.
            raise JobDeferred("official_turn_interrupted", 3) from None

    async def _execute(self, job):
        decision, inbound, text = await self._calls(self._prepare, job)
        if decision.handle is None:
            return {"status": decision.state, "reason": decision.reason}
        handle = decision.handle
        life = OfficialTurnLifecycle(api=self.api, token=self.token, handle=handle,
            job=job, admission=self.store.admitted, transactions=self.outbox.deliveries,
            journal=self.journal, database_calls=self._calls, usage=self.usage, clock=self.clock)
        binding = ExecutionBinding(self.api, self.token, handle, job, self.store.admitted)
        platform = OfficialTurnPlatform(inbound, execution=binding, outbox=self.outbox,
            database_calls=self._calls, transport_state=self.transport_state,
            sender_wake=self.sender_wake, policy=self.policy, clock=self.clock)
        if decision.state == "prepared":
            if await platform.ready(inbound.endpoint) is not None:
                raise JobDeferred("official_turn_platform_not_ready", 3)
            fence_context = active_job_fence.set(life.job_fence())
            try:
                if handle.execution_kind == "command":
                    async def admitted(_inbound):
                        await life.assert_current()
                        return True

                    async def before_effect(_name, _inbound):
                        def begin():
                            with self.outbox.deliveries.transaction() as cursor:
                                self.api.begin_command_execution(cursor, **life.args())
                        await self._calls(begin)

                    services = self.command_services(life, inbound) if callable(self.command_services) else self.command_services
                    result = await route_command(text, inbound, services=services,
                        admission=admitted, before_effect=before_effect)
                    # The source-bound execution fence is the authoritative
                    # current admission check before all command callbacks.
                    if result.kind != "reply" or result.result is None:
                        await life.interrupted(turn_id=None, reason="command_route_suppressed")
                        return {"status": "suppressed"}
                    await life.finish(turn_id=None, status="succeeded", trace=None,
                        final_text=result.result.text, error_code="", scope_key=inbound.scope.key)
                else:
                    seconds = min(self.model_timeout_seconds, max(0, handle.deadline_at - self.clock()))
                    async with asyncio.timeout(seconds):
                        await self.orchestrator.run(inbound, platform, text, durable_lifecycle=life)
            except BaseException:
                try:
                    await life.interrupted(turn_id=None if handle.execution_kind == "command" else decision.journal_turn_id,
                        reason="consumer_execution_interrupted")
                except BaseException:
                    pass
                raise
            finally:
                active_job_fence.reset(fence_context)
        body = await self._calls(self._result, life)
        if body is None:
            return {"status": "silent"}
        outcome = await platform.send(SendRequest(endpoint=inbound.endpoint, actor=inbound.actor,
            body=body, delivery_id=self.api.logical_result_key(handle), route_epoch=handle.route_epoch,
            reply_credential=inbound.reply_credential, reply_to_native_message_id=inbound.native_message_id))
        if outcome.status == "failed":
            if outcome.reason in {"needs_attachment", "delivery_expired"}:
                return {"status": outcome.reason}
            raise JobDeferred("official_result_handoff_unavailable", 3)
        # Delivery completion belongs to M3 receipts. Queueing does not claim
        # the user has received a response.
        return {"status": "handed_off"}
