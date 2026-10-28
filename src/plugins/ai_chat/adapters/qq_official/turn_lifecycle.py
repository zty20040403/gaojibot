"""Transaction hooks for the unchanged shared model/tool loop."""
from __future__ import annotations

import json
import time
from contextlib import contextmanager

from src.plugins.ai_chat.agent.control import JobFence, LeaseLost
from src.plugins.ai_chat.message_ir import MessageBody, TextNode, body_to_json


class LifecycleError(RuntimeError):
    """Fixed, operator-safe diagnostic."""


class OfficialTurnLifecycle:
    def __init__(self, *, api, token, handle, job, admission, transactions, journal,
                 database_calls, usage=None, clock=time.time):
        self.api, self.token, self.handle, self.job = api, token, handle, job
        self.admission, self.transactions, self.journal = admission, transactions, journal
        self._calls, self.usage, self.clock = database_calls, usage, clock

    def args(self):
        return dict(token=self.token, handle=self.handle, job=self.job,
                    admission=self.admission, now=int(self.clock()))

    def _current(self, cursor):
        return self.api.assert_execution_current(cursor, **self.args())

    async def assert_current(self):
        def check():
            with self.transactions.transaction() as cursor:
                row = self._current(cursor)
                if int(self.clock()) >= row["deadline_at"]:
                    raise LifecycleError("turn_deadline_expired")
        await self._calls(check)

    @contextmanager
    def effect_scope(self, inbound):
        """Hold both execution fences across one synchronous command effect.

        The command was marked executing before this boundary. A separate
        settings-store commit is not claimed to be atomic with its reply;
        interruption retains needs_attention and cannot replay the effect.
        """
        if (inbound.endpoint != self.handle.endpoint or inbound.actor != self.handle.actor
                or inbound.route_epoch != self.handle.route_epoch or inbound.scope.key != self.handle.scope_key
                or inbound.native_message_id != self.handle.source_native_message_id):
            raise LifecycleError("execution_input_mismatch")
        with self.transactions.transaction() as cursor:
            row = self._current(cursor)
            if row["state"] != "executing" or int(self.clock()) >= row["deadline_at"]:
                raise LifecycleError("execution_effect_not_current")
            yield cursor

    def job_fence(self):
        def guard(cursor):
            try:
                row = self._current(cursor)
                if row["state"] != "executing" or int(self.clock()) >= row["deadline_at"]:
                    raise LifecycleError("execution_effect_not_current")
            except Exception:
                raise LeaseLost("official_turn_execution_unavailable") from None

        def check():
            try:
                with self.transactions.transaction() as cursor:
                    guard(cursor)
                return True
            except (Exception, LeaseLost):
                return False
        return JobFence(self.job.job_id, self.job.lease_owner, self.job.attempts, check,
                        self.job.lease_generation, guard)

    async def begin(self, *, inbound, selected_profile, objective, prompt_version):
        def begin():
            with self.transactions.transaction() as cursor:
                row = self._current(cursor)
                if row["state"] != "prepared" or row["execution_kind"] != "chat":
                    raise LifecycleError("execution_not_prepared")
                source = cursor.execute("SELECT native_message_id,body_json FROM messages WHERE canonical_message_id=?",
                                        (self.handle.canonical_message_id,)).fetchone()
                if (inbound.endpoint != self.handle.endpoint or inbound.actor != self.handle.actor
                        or inbound.route_epoch != self.handle.route_epoch or inbound.scope.key != self.handle.scope_key
                        or source is None or inbound.native_message_id != source["native_message_id"]
                        or json.loads(body_to_json(inbound.body)) != json.loads(source["body_json"])):
                    raise LifecycleError("execution_input_mismatch")
                turn = self.journal.start_turn(inbound.scope,
                    trigger_canonical_message_id=self.handle.canonical_message_id, objective=objective,
                    provider=selected_profile.provider_identity, model=selected_profile.model,
                    profile=selected_profile.name, prompt_version=prompt_version,
                    started_at=int(self.clock()), transaction_cursor=cursor)
                self.api.begin_execution(cursor, **self.args(), journal_turn_id=turn.turn_id)
                return turn.turn_id
        return await self._calls(begin)

    async def finish(self, *, turn_id, status, trace, final_text, error_code, scope_key):
        if scope_key != self.handle.scope_key or not isinstance(final_text, str):
            raise LifecycleError("execution_result_mismatch")
        body = MessageBody() if status in {"silence", "aborted", "cancelled"} else MessageBody((TextNode(0, final_text),))

        def finish():
            with self.transactions.transaction() as cursor:
                self._current(cursor)
                changed = self.api.capture_result(cursor, **self.args(), body=body, turn_id=turn_id, status=status)
                if not changed:
                    return
                if turn_id is not None:
                    journal_row = cursor.execute("SELECT status FROM agent_turns WHERE turn_id=? FOR UPDATE", (turn_id,)).fetchone()
                    if journal_row is None or journal_row["status"] != "running":
                        raise LifecycleError("execution_journal_not_running")
                    payload = trace.to_payload() if trace is not None else {}
                    if error_code:
                        payload["failure"] = {"code": error_code}
                    recorded = self.journal.finish_turn(turn_id, status=status, final_text=final_text,
                        trace_payload=payload or None, input_tokens=trace.input_tokens if trace else 0,
                        output_tokens=trace.output_tokens if trace else 0,
                        total_tokens=trace.total_tokens if trace else 0,
                        finished_at=int(self.clock()), transaction_cursor=cursor)
                    if recorded is None:
                        raise LifecycleError("execution_journal_missing")
                if self.usage is not None and trace is not None and (trace.input_tokens > 0 or trace.output_tokens > 0):
                    self.usage.record(scope_key=scope_key, source="turn", provider=trace.provider,
                        model=trace.model, input_tokens=trace.input_tokens, output_tokens=trace.output_tokens,
                        cached_tokens=trace.cached_tokens, turn_id=turn_id,
                        occurred_at=int(self.clock()), transaction_cursor=cursor)
        await self._calls(finish)

    async def interrupted(self, *, turn_id, reason):
        def interrupt():
            with self.transactions.transaction() as cursor:
                row = self._current(cursor)
                if row["journal_turn_id"] != turn_id:
                    raise LifecycleError("execution_journal_mismatch")
                return self.api.interrupt_execution(cursor, **self.args(), reason=reason)
        return await self._calls(interrupt)
