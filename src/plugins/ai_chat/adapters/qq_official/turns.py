"""Direct asyncio execution for official QQ turns and commands."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
import hashlib
import json
import logging
import secrets
import time
import traceback

from src.plugins.ai_chat.agent_loop import FinalStreamState
from src.plugins.ai_chat.application.commands import CommandPolicy, CommandServices
from src.plugins.ai_chat.application.platform_chat import PlatformChatService
from src.plugins.ai_chat.application.platform_conversation import build_platform_chat_orchestrator
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.execution_control import LeaseLost as ToolLeaseLost
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MediaNode, MessageBody, TextNode, body_from_json, body_to_json
from src.plugins.ai_chat.model_catalog import SUPPORTED_REASONING_EFFORTS
from src.plugins.ai_chat.observability import telemetry
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey, InboundMessage
from src.plugins.ai_chat.timeline import render_line

from .commands import guarded_services, route_command, usage_callback
from .conversation_control import permitted
from .lease import LeaseLost, assert_epoch
from .renderer import render_plan
from .sections import ReplyStream
from .turn_platform import (
    ExecutionBinding, ExecutionError, ExecutionFenceLost, ExecutionHandle,
    OfficialTurnPlatform, PlatformBoundaryError, TransportState, assert_execution_current,
)

_turn_observation = ContextVar("qq_turn_observation", default=None)
logger = logging.getLogger(__name__)


class LifecycleError(RuntimeError):
    """Fixed, operator-safe diagnostic."""


class TurnInputError(RuntimeError):
    """Fixed diagnostic only."""


@dataclass(frozen=True)
class ExecutionDecision:
    state: str
    reason: str = ""
    handle: ExecutionHandle | None = field(default=None, repr=False)
    journal_turn_id: int | None = None
    plan_id: str | None = None


def _issue(row) -> ExecutionHandle:
    return ExecutionHandle(
        row["execution_id"], row["inbox_id"],
        EndpointKey.parse(row["endpoint_key"]), ActorKey.parse(row["requester_actor_key"]),
        row["canonical_message_id"], row["source_native_message_id"], row["scope_key"],
        row["endpoint_generation"], row["route_epoch"], row["account_fencing_epoch"],
        row["execution_revision"], row["deadline_at"], row["result_purpose"], row["execution_kind"],
    )


def claim_execution(cursor, *, token, inbox_id: str, now: int, deadline_at: int,
                    admission, result_purpose: str = "final", execution_kind: str = "chat") -> ExecutionDecision:
    assert_epoch(cursor, token)
    inbox = cursor.execute("SELECT * FROM qq_official_inbound_events WHERE inbox_id=? AND account_key=?",
                           (inbox_id, token.account_key)).fetchone()
    if inbox is None or inbox["canonical_message_id"] is None:
        raise ExecutionError("turn_source_unavailable")
    message = cursor.execute("SELECT m.*,c.scope_key FROM messages m JOIN conversations c USING(conversation_id) WHERE canonical_message_id=?",
                             (inbox["canonical_message_id"],)).fetchone()
    endpoint, actor = EndpointKey.parse(inbox["endpoint_key"]), ActorKey.parse(message["sender_actor_key"])
    if not admission(cursor, endpoint, actor):
        raise ExecutionError("turn_source_unavailable")
    if not permitted(cursor, endpoint, canonical=inbox["canonical_message_id"], commands=execution_kind == "command"):
        return ExecutionDecision("cancelled", "conversation_stopped_or_replaced")
    existing = cursor.execute("SELECT * FROM qq_official_turn_executions WHERE inbox_id=?",
                              (inbox["inbox_id"],)).fetchone()
    if existing is not None:
        return ExecutionDecision("already_running" if existing["state"] == "executing" else existing["state"])
    fields = dict(
        execution_id=secrets.token_hex(16), account_key=token.account_key,
        endpoint_key=endpoint.key, requester_actor_key=actor.key, inbox_id=inbox["inbox_id"], job_id=None,
        canonical_message_id=message["canonical_message_id"], source_native_message_id=inbox["native_message_id"],
        scope_key=message["scope_key"], endpoint_generation=inbox["endpoint_generation"], route_epoch=inbox["route_epoch"],
        account_fencing_epoch=token.epoch, worker_owner=token.holder_id, job_lease_generation=1, execution_revision=1,
        state="prepared", input_digest=hashlib.sha256(inbox["inbox_id"].encode()).hexdigest(), reason_code="",
        result_purpose=result_purpose, execution_kind=execution_kind, deadline_at=deadline_at,
        created_at=now, updated_at=now,
    )
    row = cursor.execute("INSERT INTO qq_official_turn_executions (" + ",".join(fields) + ") VALUES ("
                         + ",".join("?" for _ in fields) + ") RETURNING *", tuple(fields.values())).fetchone()
    return ExecutionDecision("prepared", handle=_issue(row))


def begin_execution(cursor, *, token, handle: ExecutionHandle, now: int, admission, journal_turn_id: int | None):
    row = assert_execution_current(cursor, token=token, handle=handle, now=now, admission=admission)
    cursor.execute("UPDATE qq_official_turn_executions SET state='executing',journal_turn_id=?,started_at=?,updated_at=? WHERE execution_id=?",
                   (journal_turn_id, now, now, row["execution_id"]))


def _current_reply(cursor, handle: ExecutionHandle, message):
    expected = message["reply_to_native_message_id"]
    if not expected:
        return None
    source = cursor.execute("""SELECT ref_msg_idx FROM qq_official_messages
        WHERE account_key=? AND endpoint_key=? AND native_message_id=?
          AND canonical_message_id=? AND direction='inbound'""",
        (handle.endpoint.account.key, handle.endpoint.key, message["native_message_id"],
         handle.canonical_message_id)).fetchone()
    if source is None or not source["ref_msg_idx"]:
        return None
    targets = cursor.execute("""SELECT i.native_message_id,i.canonical_message_id,i.ambiguous,
        q.canonical_message_id AS mapped_id,m.source_endpoint_key,m.native_message_id AS ledger_native
        FROM qq_official_message_indexes i LEFT JOIN qq_official_messages q
          ON q.account_key=i.account_key AND q.endpoint_key=i.endpoint_key AND q.native_message_id=i.native_message_id
        LEFT JOIN messages m ON m.canonical_message_id=i.canonical_message_id
        WHERE i.account_key=? AND i.endpoint_key=? AND i.index_value=? AND i.index_kind IN ('msg_idx','ref_idx')""",
        (handle.endpoint.account.key, handle.endpoint.key, source["ref_msg_idx"])).fetchall()
    if not targets or any(
        row["ambiguous"] or row["native_message_id"] != expected or row["canonical_message_id"] is None
        or row["mapped_id"] != row["canonical_message_id"] or row["source_endpoint_key"] != handle.endpoint.key
        or row["ledger_native"] != expected for row in targets
    ):
        return None
    return expected


def load_turn_input(cursor, *, token, handle: ExecutionHandle, now: int, admission) -> InboundMessage:
    assert_execution_current(cursor, token=token, handle=handle, now=now, admission=admission)
    row = cursor.execute("""SELECT m.*,i.event_id,i.received_at,i.trigger_requested
        FROM messages m JOIN qq_official_inbound_events i ON i.canonical_message_id=m.canonical_message_id
        WHERE i.inbox_id=? AND i.account_key=? AND i.endpoint_key=? AND m.canonical_message_id=?""",
        (handle.inbox_id, token.account_key, handle.endpoint.key, handle.canonical_message_id)).fetchone()
    if row is None:
        raise TurnInputError("turn_source_unavailable")
    try:
        body = body_from_json(row["body_json"])
        if json.loads(body_to_json(body)) != json.loads(row["body_json"]):
            raise ValueError
        scope = ConversationScope.from_key(handle.scope_key)
    except (TypeError, ValueError):
        raise TurnInputError("turn_source_body_invalid") from None
    return InboundMessage(
        endpoint=handle.endpoint, actor=handle.actor, event_id=row["event_id"],
        native_message_id=row["native_message_id"], sender_display=row["sender_display"],
        platform_role="", body=body, mentions_bot=bool(row["trigger_requested"]),
        reply_to_native_message_id=_current_reply(cursor, handle, row),
        occurred_at=row["occurred_at"], reply_credential=None,
        canonical_scope=scope if scope != handle.endpoint.scope else None,
        route_epoch=handle.route_epoch,
    )


class OfficialTurnLifecycle:
    def __init__(self, *, token, handle: ExecutionHandle, admission, transactions, journal,
                 database_calls, usage=None, clock=time.time):
        self.token, self.handle, self.admission = token, handle, admission
        self.transactions, self.journal = transactions, journal
        self._calls, self.usage, self.clock = database_calls, usage, clock
        self.platform: OfficialTurnPlatform | None = None
        self.journal_turn_id: int | None = None
        self.delivery_outcome: str = "pending"

    def _current(self, cursor):
        return assert_execution_current(
            cursor, token=self.token, handle=self.handle, now=int(self.clock()), admission=self.admission,
        )

    async def assert_current(self):
        def check():
            with self.transactions.transaction() as cursor:
                row = self._current(cursor)
                if int(self.clock()) >= row["deadline_at"]:
                    raise LifecycleError("turn_deadline_expired")
        await self._calls(check)

    def command_source(self, cursor, inbound):
        if (inbound.endpoint != self.handle.endpoint or inbound.actor != self.handle.actor
                or inbound.route_epoch != self.handle.route_epoch or inbound.scope.key != self.handle.scope_key
                or inbound.native_message_id != self.handle.source_native_message_id):
            raise LifecycleError("execution_input_mismatch")
        row = self._current(cursor)
        route = cursor.execute("SELECT generation,route_epoch FROM qq_official_endpoints WHERE endpoint_key=?",
                               (self.handle.endpoint.key,)).fetchone()
        if (row["state"] != "executing" or int(self.clock()) >= row["deadline_at"] or route is None
                or (route["generation"], route["route_epoch"]) != (self.handle.endpoint_generation, self.handle.route_epoch)):
            raise LifecycleError("execution_effect_not_current")
        return row

    @contextmanager
    def effect_scope(self, inbound):
        with self.transactions.transaction() as cursor:
            self.command_source(cursor, inbound)
            yield cursor

    async def begin(self, *, inbound, selected_profile, objective, prompt_version):
        observation = _turn_observation.get()
        if observation is not None:
            observation.update(kind=inbound.endpoint.kind, model=selected_profile.model, profile=selected_profile.name)
            logger.info("QQ turn start kind=%s model=%s profile=%s",
                        inbound.endpoint.kind, selected_profile.model, selected_profile.name)

        def run():
            now = int(self.clock())
            with self.transactions.transaction() as cursor:
                row = self._current(cursor)
                if row["state"] != "prepared" or row["execution_kind"] != "chat":
                    raise LifecycleError("execution_not_prepared")
                turn = self.journal.start_turn(
                    inbound.scope, trigger_canonical_message_id=self.handle.canonical_message_id,
                    objective=objective, provider=selected_profile.provider_identity,
                    model=selected_profile.model, profile=selected_profile.name,
                    prompt_version=prompt_version, started_at=now, transaction_cursor=cursor,
                )
                begin_execution(cursor, token=self.token, handle=self.handle, now=now,
                                admission=self.admission, journal_turn_id=turn.turn_id)
                self.journal_turn_id = turn.turn_id
                return turn.turn_id

        return await self._calls(run)

    async def finish(self, *, turn_id, status, trace, final_text, error_code, scope_key):
        observation = _turn_observation.get()
        if observation is not None:
            observation["status"] = status
            if trace is not None:
                observation.update(model=trace.model, profile=trace.profile,
                                   tools=sum(1 for message in trace.messages if message.get("role") == "tool"))
        if scope_key != self.handle.scope_key or not isinstance(final_text, str):
            raise LifecycleError("execution_result_mismatch")
        silent = status in {"silence", "aborted", "cancelled"} or not final_text.strip()

        def run():
            now = int(self.clock())
            with self.transactions.transaction() as cursor:
                row = self._current(cursor)
                if row["state"] == "handed_off":
                    return False
                if turn_id is not None:
                    journal_row = cursor.execute("SELECT status FROM agent_turns WHERE turn_id=? FOR UPDATE",
                                                 (turn_id,)).fetchone()
                    if journal_row is None or journal_row["status"] != "running":
                        raise LifecycleError("execution_journal_not_running")
                    payload = trace.to_payload() if trace is not None else {}
                    if error_code:
                        payload["failure"] = {"code": error_code}
                    if trace is not None and trace.model:
                        cursor.execute("UPDATE agent_turns SET provider=?,model=?,profile=? WHERE turn_id=?",
                                       (trace.provider, trace.model, trace.profile, turn_id))
                    recorded = self.journal.finish_turn(
                        turn_id, status=status, final_text=final_text,
                        trace_payload=payload or None, input_tokens=trace.input_tokens if trace else 0,
                        output_tokens=trace.output_tokens if trace else 0,
                        total_tokens=trace.total_tokens if trace else 0,
                        finished_at=now, transaction_cursor=cursor,
                    )
                    if recorded is None:
                        raise LifecycleError("execution_journal_missing")
                if self.usage is not None and trace is not None and (trace.input_tokens > 0 or trace.output_tokens > 0):
                    self.usage.record(
                        scope_key=scope_key, source="turn", provider=trace.provider, model=trace.model,
                        input_tokens=trace.input_tokens, output_tokens=trace.output_tokens,
                        cached_tokens=trace.cached_tokens, turn_id=turn_id,
                        occurred_at=now, transaction_cursor=cursor,
                    )
                pending = list(getattr(self.platform, "pending_media", ()) or ())
                if silent or self.platform is None:
                    cursor.execute("UPDATE qq_official_turn_executions SET state='handed_off',plan_id=NULL,handed_off_at=?,updated_at=? WHERE execution_id=?",
                                   (now, now, self.handle.execution_id))
                    self.delivery_outcome = "silent"
                    # A sticker chosen without any reply text still goes out.
                    for enqueue in pending if status == "silence" else ():
                        enqueue(cursor)
                    return bool(pending) and status == "silence"
                try:
                    from .mentions import parse_mention_body
                    self.platform.enqueue_reply(cursor, parse_mention_body(cursor, self.handle.endpoint, final_text),
                                                now=now, turn_id=turn_id)
                except PlatformBoundaryError as exc:
                    if str(exc) in {"needs_attachment", "delivery_expired"}:
                        cursor.execute("UPDATE qq_official_turn_executions SET state='cancelled',reason_code=?,updated_at=? WHERE execution_id=?",
                                       (str(exc), now, self.handle.execution_id))
                        self.delivery_outcome = str(exc)
                        return False
                    raise
                # Same transaction, later delivery ids: the sender's per-conversation
                # queue sends the sticker after the reply text.
                for enqueue in pending:
                    enqueue(cursor)
                self.delivery_outcome = "handed_off"
                return True

        enqueued = await self._calls(run)
        if enqueued and self.platform is not None:
            self.platform.wake()

    async def interrupted(self, *, turn_id, reason, trace=None):
        def run():
            now = int(self.clock())
            with self.transactions.transaction() as cursor:
                assert_epoch(cursor, self.token)
                row = cursor.execute("""SELECT * FROM qq_official_turn_executions WHERE execution_id=?
                    AND account_key=? AND account_fencing_epoch=? FOR UPDATE""",
                    (self.handle.execution_id, self.token.account_key, self.token.epoch)).fetchone()
                if row is None or (row["endpoint_key"], row["requester_actor_key"], row["canonical_message_id"], row["scope_key"]) != (
                        self.handle.endpoint.key, self.handle.actor.key, self.handle.canonical_message_id, self.handle.scope_key):
                    raise LifecycleError("execution_source_mismatch")
                if row["journal_turn_id"] != turn_id:
                    raise LifecycleError("execution_journal_mismatch")
                if row["state"] not in {"prepared", "executing"}:
                    return row["state"]
                cursor.execute("UPDATE qq_official_turn_executions SET state='needs_attention',reason_code=?,updated_at=? WHERE execution_id=?",
                               (reason, now, self.handle.execution_id))
                if turn_id is not None:
                    payload = trace.to_payload() if trace else {}
                    payload.update(failure={"code": reason}, usage_incomplete=True)
                    cancelled = reason == "turn_cancelled_or_owner_lost" or not permitted(
                        cursor, self.handle.endpoint, canonical=self.handle.canonical_message_id,
                        commands=row["execution_kind"] == "command",
                    )
                    self.journal.finish_turn(
                        turn_id, status="aborted" if cancelled else "crashed", final_text="",
                        trace_payload=payload, input_tokens=trace.input_tokens if trace else 0,
                        output_tokens=trace.output_tokens if trace else 0,
                        total_tokens=trace.total_tokens if trace else 0,
                        finished_at=now, transaction_cursor=cursor,
                    )
                    if self.usage is not None and trace is not None and (trace.input_tokens or trace.output_tokens):
                        self.usage.record(
                            scope_key=self.handle.scope_key, source="turn", provider=trace.provider,
                            model=trace.model, input_tokens=trace.input_tokens, output_tokens=trace.output_tokens,
                            cached_tokens=trace.cached_tokens, turn_id=turn_id, occurred_at=now,
                            transaction_cursor=cursor,
                        )
                return "needs_attention"

        return await self._calls(run)


class OfficialTurnConsumer:
    def __init__(self, *, token, store, outbox, journal, database_calls,
                 orchestrator, command_services, transport_state, usage=None,
                 sender_wake=None, policy=None, clock=time.time,
                 model_timeout_seconds=600, business_services=None, rest_provider=None):
        if type(model_timeout_seconds) is not int or not 1 <= model_timeout_seconds <= 3600:
            raise ValueError("invalid_model_timeout")
        self.token, self.store, self.outbox = token, store, outbox
        self.journal, self._calls, self.orchestrator = journal, database_calls, orchestrator
        self.command_services, self.transport_state, self.usage = command_services, transport_state, usage
        self.sender_wake, self.policy, self.clock = sender_wake, policy, clock
        self.model_timeout_seconds = model_timeout_seconds
        self.business_services = business_services
        self.rest_provider = rest_provider

    @staticmethod
    def _unpack(source) -> tuple[str, tuple[int, ...], str, object]:
        if isinstance(source, str):
            return source, (), "", None
        payload = getattr(source, "payload", None)
        if isinstance(payload, dict):
            inbox_id = str(payload.get("inbox_id") or "")
            batch_ids = tuple(payload.get("batch_message_ids", ()))
        else:
            inbox_id = str(getattr(source, "inbox_id", ""))
            batch_ids = tuple(getattr(source, "batch_message_ids", ()))
        proactive_reason = str(getattr(source, "proactive_reason", "") or "").strip()
        mid_turn = getattr(source, "mid_turn_pending", None)
        return inbox_id, batch_ids, proactive_reason, mid_turn

    def _prepare(self, inbox_id: str, batch_ids: tuple[int, ...], proactive_reason: str = ""):
        now = int(self.clock())
        with self.outbox.deliveries.transaction() as cursor:
            row = cursor.execute("""SELECT i.received_at,i.turn_text,i.turn_text_version,x.deadline_at,x.execution_kind
                FROM qq_official_inbound_events i JOIN messages m ON m.canonical_message_id=i.canonical_message_id
                LEFT JOIN qq_official_turn_executions x ON x.inbox_id=i.inbox_id
                WHERE i.inbox_id=? AND i.account_key=?""",
                (inbox_id, self.token.account_key)).fetchone()
            if row is None:
                raise ExecutionError("turn_source_unavailable")
            if row["turn_text_version"] != 1 or not isinstance(row["turn_text"], str):
                raise ExecutionError("turn_input_unavailable")
            text = row["turn_text"]
            kind = "command" if text.lstrip().startswith(("/", "!")) else "chat"
            if row["execution_kind"] is not None and row["execution_kind"] != kind:
                raise ExecutionError("turn_parser_identity_conflict")
            deadline = row["deadline_at"] or row["received_at"] + self.outbox.max_wait_seconds
            decision = claim_execution(
                cursor, token=self.token, inbox_id=inbox_id, now=now,
                deadline_at=deadline, admission=self.store.admitted, execution_kind=kind,
            )
            if decision.handle is None:
                return decision, None, None
            inbound = load_turn_input(
                cursor, token=self.token, handle=decision.handle, now=now, admission=self.store.admitted,
            )
            if kind == "chat" and (batch_ids or any(isinstance(n, MediaNode) for n in inbound.body.nodes)):
                lines = []
                for canonical in batch_ids or (decision.handle.canonical_message_id,):
                    message_row = MessageLedger._message_row(cursor, canonical)
                    if message_row is None:
                        raise ExecutionError("batch_source_unavailable")
                    message = MessageLedger._row_to_message(message_row)
                    if (message.scope_key != inbound.scope.key or message.source_endpoint_key != inbound.endpoint.key
                            or message.sender_actor_key != inbound.actor.key or message.direction != "inbound"):
                        raise ExecutionError("batch_source_mismatch")
                    lines.append(render_line(message))
                text = ("[连续消息，请合起来回答一次]\n" if batch_ids else "") + "\n".join(lines)
                inbound = replace(inbound, batch_message_ids=batch_ids)
            if kind == "chat" and proactive_reason:
                text = f"[判定：{proactive_reason}]\n{text}"
            return decision, inbound, text

    def _drain_mid_turn_lines(self, handle: ExecutionHandle, items: list[object]) -> list[str]:
        lines: list[str] = []
        with self.outbox.deliveries.transaction() as cursor:
            assert_epoch(cursor, self.token)
            for item in items:
                item_inbox = str(getattr(item, "inbox_id", "") or "")
                if not item_inbox:
                    continue
                row = cursor.execute(
                    """SELECT i.canonical_message_id, i.trigger_requested
                       FROM qq_official_inbound_events i
                       WHERE i.inbox_id=? AND i.account_key=? AND i.endpoint_key=?""",
                    (item_inbox, self.token.account_key, handle.endpoint.key),
                ).fetchone()
                if row is None or row["canonical_message_id"] is None:
                    continue
                cid = int(row["canonical_message_id"])
                if not permitted(cursor, handle.endpoint, canonical=cid):
                    continue
                msg_row = MessageLedger._message_row(cursor, cid)
                if msg_row is None:
                    continue
                msg = MessageLedger._row_to_message(msg_row)
                if msg.direction != "inbound":
                    continue
                reason = str(getattr(item, "proactive_reason", "") or "").strip()
                rendered = render_line(msg, max_chars=2000)
                if reason:
                    lines.append(f"[判定：{reason}]\n{rendered}")
                elif row["trigger_requested"] and handle.endpoint.kind == "group":
                    lines.append(f"{rendered}（@你）")
                else:
                    lines.append(rendered)
        return lines

    async def __call__(self, source):
        observation = dict(kind=None, model="", profile="", tools=0, status="crashed")
        mark = _turn_observation.set(observation)
        started = time.monotonic()
        try:
            return await self._run(source)
        finally:
            if observation["kind"] is not None:
                elapsed = time.monotonic() - started
                labels = dict(platform="qq-official", kind=observation["kind"], status=observation["status"])
                telemetry.turns.labels(**labels).inc()
                telemetry.turn_duration.labels(**labels).observe(elapsed)
                logger.info("QQ turn end kind=%s seconds=%.2f status=%s model=%s profile=%s tools=%d",
                            observation["kind"], elapsed, observation["status"], observation["model"],
                            observation["profile"], observation["tools"])
            _turn_observation.reset(mark)

    async def _run(self, source):
        inbox_id, batch_ids, proactive_reason, mid_turn = self._unpack(source)
        try:
            return await self._execute(inbox_id, batch_ids, proactive_reason=proactive_reason, mid_turn=mid_turn)
        except asyncio.CancelledError:
            raise
        except (ToolLeaseLost, LeaseLost, ExecutionFenceLost):
            logger.warning("QQ turn stopped: account or permission changed")
            return {"status": "cancelled"}
        except Exception as error:
            logger.warning("QQ turn error=%s frames=%s", type(error).__name__,
                           [(frame.name, frame.lineno) for frame in traceback.extract_tb(error.__traceback__)[-4:]])

            def report():
                now = int(self.clock())
                with self.outbox.deliveries.transaction() as cursor:
                    row = cursor.execute("SELECT * FROM qq_official_turn_executions WHERE inbox_id=?",
                                         (inbox_id,)).fetchone()
                    if row is None:
                        return
                    endpoint, actor = EndpointKey.parse(row["endpoint_key"]), ActorKey.parse(row["requester_actor_key"])
                    if not permitted(cursor, endpoint, canonical=row["canonical_message_id"],
                                     commands=row["execution_kind"] == "command"):
                        return
                    self.outbox.enqueue(
                        self.token, rendered=render_plan(MessageBody((TextNode(0, "这次处理失败了，请再试一次。"),)), endpoint),
                        requester=actor, logical_key="turn-error:" + row["execution_id"],
                        source_scope_key=row["scope_key"], source_native_message_id=row["source_native_message_id"],
                        source_canonical_message_id=row["canonical_message_id"],
                        expires_at=now + 300, transaction_cursor=cursor,
                    )
                    if row["journal_turn_id"] is not None:
                        self.journal.finish_turn(row["journal_turn_id"], status="crashed", final_text="",
                                                 finished_at=now, transaction_cursor=cursor)
                    cursor.execute("UPDATE qq_official_turn_executions SET state='cancelled',reason_code='turn_failed',updated_at=? WHERE execution_id=? AND plan_id IS NULL",
                                   (now, row["execution_id"]))

            await self._calls(report)
            if self.sender_wake:
                self.sender_wake()
            return {"status": "failed"}

    async def _execute(self, inbox_id: str, batch_ids: tuple[int, ...], *, proactive_reason: str = "", mid_turn=None):
        decision, inbound, text = await self._calls(self._prepare, inbox_id, batch_ids, proactive_reason)
        if decision.handle is None:
            return {"status": decision.state, "reason": decision.reason}
        handle = decision.handle
        admission = self.store.admitted
        life = OfficialTurnLifecycle(
            token=self.token, handle=handle, admission=admission,
            transactions=self.outbox.deliveries, journal=self.journal,
            database_calls=self._calls, usage=self.usage, clock=self.clock,
        )
        binding = ExecutionBinding(self.token, handle, admission)
        platform = OfficialTurnPlatform(
            inbound, execution=binding, outbox=self.outbox, database_calls=self._calls,
            transport_state=self.transport_state, sender_wake=self.sender_wake,
            policy=self.policy, clock=self.clock, rest_provider=self.rest_provider,
        )
        life.platform = platform
        if await platform.ready(inbound.endpoint) is not None:
            raise RuntimeError("official_turn_platform_not_ready")
        try:
            if handle.execution_kind == "command":
                async def admitted(_inbound):
                    await life.assert_current()
                    return True

                async def before_effect(_name, _inbound):
                    def begin():
                        with self.outbox.deliveries.transaction() as cursor:
                            begin_execution(cursor, token=self.token, handle=handle,
                                            now=int(self.clock()), admission=admission, journal_turn_id=None)
                    await self._calls(begin)

                services = self.command_services(life, inbound) if callable(self.command_services) else self.command_services
                result = await route_command(
                    text, inbound, services=services, admission=admitted, before_effect=before_effect,
                    services_factory=(lambda: self.business_services(life, inbound, platform, services))
                    if self.business_services is not None else None,
                )
                if result.kind != "reply" or result.result is None:
                    await life.interrupted(turn_id=None, reason="command_route_suppressed")
                    return {"status": "suppressed"}
                await life.finish(turn_id=None, status="succeeded", trace=None,
                                  final_text=result.result.text, error_code="", scope_key=inbound.scope.key)
            else:
                extra_kwargs = {}
                if mid_turn is not None:
                    async def feedback_provider():
                        notes: list[str] = []
                        for info in self.orchestrator.running_tasks.list_for_endpoint(handle.endpoint):
                            notes.extend(self.orchestrator.running_tasks.drain_feedback(info.task_id))
                        queued = list(mid_turn.pending)
                        mid_turn.pending.clear()
                        if queued:
                            merged_lines = await self._calls(self._drain_mid_turn_lines, handle, queued)
                            if merged_lines:
                                notes.append("[新消息]\n" + "\n".join(merged_lines))
                        return notes
                    extra_kwargs["feedback_provider"] = feedback_provider
                seconds = min(self.model_timeout_seconds, max(0, handle.deadline_at - self.clock()))
                async with asyncio.timeout(seconds):
                    await self.orchestrator.run(
                        inbound, platform, text, durable_lifecycle=life,
                        final_stream_sink=ReplyStream(platform), final_stream_state=FinalStreamState(),
                        **extra_kwargs,
                    )
        except BaseException:
            try:
                await life.interrupted(
                    turn_id=None if handle.execution_kind == "command" else life.journal_turn_id,
                    reason="consumer_execution_interrupted",
                )
            except BaseException:
                pass
            raise
        return {"status": life.delivery_outcome}


def build_turn_consumer(runtime, token, app):
    def scope_enabled(scope):
        if scope.account != runtime.loaded.account:
            return False
        if app.model_preferences.get_scope_enabled_override(scope) is False:
            return False
        endpoint = EndpointKey(scope.account, scope.kind, scope.native_conversation_id)
        with runtime.deliveries.transaction() as cursor:
            assert_epoch(cursor, token)
            row = cursor.execute("SELECT binding_state,membership_state FROM qq_official_endpoints WHERE endpoint_key=?",
                                 (endpoint.key,)).fetchone()
            return bool(row and row["membership_state"] == "present"
                        and (row["binding_state"] == "active" if endpoint.kind == "group"
                             else row["binding_state"] != "revoked"))

    from .billing import foreground
    service_options = {"scope_enabled": scope_enabled, "billing_factory": foreground(runtime, app)}
    media = getattr(runtime, "media_service", None)
    if media is not None:
        service_options["media_factory"] = media.foreground
    files = getattr(runtime, "file_service", None)
    if files is not None:
        service_options["generation_factory"] = files.generation.foreground
    agents = runtime.agent_service
    service = PlatformChatService(app, **service_options)
    orchestrator = build_platform_chat_orchestrator(app, service=service)

    def command_services(life, inbound):
        active_cursor = ContextVar("official_command_cursor", default=None)

        @contextmanager
        def effect_scope(current):
            with life.effect_scope(current) as cursor:
                marker = active_cursor.set(cursor)
                try:
                    yield cursor
                finally:
                    active_cursor.reset(marker)

        def policy(cursor):
            profiles = tuple(profile.name for profile in app.model_catalog.profiles if profile.configured)
            owner = runtime.store.authority.owner_for_actor(cursor, inbound.endpoint, inbound.actor)
            role = ""
            if inbound.endpoint.kind == "group":
                from .message_roles import source_role
                role = source_role(
                    cursor, inbound.endpoint, inbound.actor,
                    life.handle.canonical_message_id, now=int(runtime.store.clock()),
                )
            preference = (app.model_preferences.get_conversation_default(inbound.endpoint.scope)
                          or app.settings.model_profile_for_scope(inbound.endpoint.scope))
            profile = app.model_catalog.resolve_preference(preference)
            efforts = tuple(SUPPORTED_REASONING_EFFORTS) if profile.configured and profile.provider in {"openai", "cliproxy"} else ()
            auto_effort = bool(getattr(app.settings, "reasoning_router_profile", "") and profile.reasoning_effort and efforts)
            return CommandPolicy(
                models=profiles, efforts=efforts, default_model=profile.name,
                default_effort=profile.reasoning_effort or "",
                allow_model_read=True, allow_model_change=owner is not None,
                allow_effort_read=True, allow_effort_change=True,
                allow_group_default=owner is not None,
                allow_effort_group_default=(owner is not None or role in {"owner", "admin"}),
                auto_effort=auto_effort,
                global_model=True,
            )

        async def host_policy(_inbound):
            def read():
                with life.effect_scope(inbound) as cursor:
                    return policy(cursor)
            return await runtime._calls(read)

        tasks = app.running_tasks
        if agents is not None:
            from .background_agents import AgentCommands
            tasks = AgentCommands(tasks, agents)

        async def browser(current, request):
            def authorize(cursor):
                source = life.command_source(cursor, current)
                if request.name == "logout" and current.endpoint.kind == "group":
                    from .message_roles import source_role
                    role = source_role(cursor, current.endpoint, current.actor,
                                       source["canonical_message_id"], now=int(runtime.store.clock()))
                    if role not in {"owner", "admin"} and runtime.store.authority.owner_for_actor(cursor, current.endpoint, current.actor) is None:
                        raise PermissionError("browser_logout_denied")
                return source
            return await agents.vm_lifecycle.browser.command(current, request, authorize=authorize, key=life.handle.execution_id)

        from .config_commands import command as config_command
        from .persona_commands import command as persona_command
        from .status_commands import command as status_command
        services = CommandServices(
            running_tasks=tasks, config=config_command(runtime, life, app), status=status_command(runtime, life, app),
            persona=persona_command(runtime, life, app),
            model_preferences=app.model_preferences, reasoning_preferences=app.reasoning_preferences,
            host_policy=host_policy, usage=usage_callback(runtime.deliveries.transaction, runtime._calls),
            browser=browser if agents is not None and agents.vm_lifecycle is not None else None,
        )
        return guarded_services(services, inbound, effect_scope=effect_scope, database_call=runtime._calls)

    def transport_state():
        gateway = runtime._gateway
        return TransportState(runtime.loaded.account, runtime.loaded.credentials is not None,
                              runtime._token is token and gateway is not None and gateway.state == "ready")

    from .business_commands import build_business_services
    return OfficialTurnConsumer(
        token=token, store=runtime.store, outbox=runtime.outbox, journal=runtime.journal,
        database_calls=runtime._calls, orchestrator=orchestrator, command_services=command_services,
        business_services=build_business_services(runtime, app),
        policy=runtime.loaded.delivery.turn_policy(), transport_state=transport_state,
        usage=app.usage_store, sender_wake=lambda: runtime.sender.wake() if runtime.sender is not None else None,
        clock=runtime.store.clock,
        rest_provider=lambda: runtime.sender.rest if runtime.sender is not None else None,
    )
