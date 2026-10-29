"""One account's async transport boundary over the existing durable outbox.

This is an internal authenticated-runtime port, never a chat/tool API. Storage
owns scheduling and retry budgets. The sender makes exactly one HTTP invocation
per issued attempt; local receipt persistence can be retried without sending.
All database callbacks finish their transaction before rate waits or network I/O.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import re
import time

from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey
from src.qq_official.errors import QQAPIError
from src.qq_official.http import scoped_request_observer

from .binding import BindingEffect
from .lease import LeaseLost, assert_epoch
from .outbox_types import SendOutcome, SendReservation
from .rate_policy import LocalAccountRateGate, RateWaitExpired
from .renderer import render_plan
from .send_policy import read_server_cooldown
from .official_metrics import observe


class SenderUnavailable(RuntimeError):
    """Fixed diagnostics only; upstream must not log arbitrary caught errors."""


async def _drain(task):
    """Finish an already started bounded operation despite repeated cancellation."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    return task.result()


class OfficialSender:
    def __init__(self, account: AccountKey, token, outbox, rest, ledger, *,
                 database_calls, ensure_current, journal=None, rate_gate=None,
                 clock=time.monotonic, wall_clock=time.time, sleep=asyncio.sleep,
                 poll_seconds=0.5, max_inflight=3, max_waiters=100,
                 receipt_retries=3, request_timeout=30, receipt_sweep_seconds=5, observer=None):
        if (not isinstance(account, AccountKey) or account.platform != "qq-official"
                or token.account_key != account.key
                or type(max_inflight) is not int or not 1 <= max_inflight <= 10
                or type(max_waiters) is not int or not max_inflight <= max_waiters <= 100
                or type(receipt_retries) is not int or not 1 <= receipt_retries <= 3
                or not 0 < request_timeout <= 30 or not 0 < poll_seconds <= 60
                or not 0.01 <= receipt_sweep_seconds <= 60):
            raise SenderUnavailable("invalid_sender_configuration")
        if rest.token_manager.credentials.app_id != account.app_id:
            raise SenderUnavailable("transport_account_mismatch")
        self.account, self.token, self.outbox, self.rest = account, token, outbox, rest
        self.ledger, self.journal = ledger, journal
        self._calls, self.ensure_current = database_calls, ensure_current
        self.clock, self.wall_clock, self.sleep = clock, wall_clock, sleep
        self.rate_gate = rate_gate or LocalAccountRateGate(account, clock=clock, wall_clock=wall_clock, sleep=sleep)
        self.poll_seconds, self.max_waiters = poll_seconds, max_waiters
        self.receipt_retries, self.request_timeout = receipt_retries, request_timeout
        self.receipt_sweep_seconds = receipt_sweep_seconds
        self._receipt_cursor = None
        self._slots = asyncio.Semaphore(max_inflight)
        self._recover_lock, self._schedule_lock = asyncio.Lock(), asyncio.Lock()
        self._recovered = False
        self._active, self._binding_ids, self._rate_waiters = set(), set(), set()
        self._tasks, self._unrecorded, self._receipt_ids = {}, {}, set()
        self._wake = asyncio.Event()
        self._stopping = self._running = False
        self.reason = "ready"
        self.observer = observer

    def wake(self):
        self._wake.set()

    async def _fence(self):
        if self._stopping:
            raise LeaseLost("Official sender stopped")
        try:
            await self.ensure_current()
        except LeaseLost:
            raise LeaseLost("Official sender account lease lost") from None

    def _cooldown(self, endpoint, mode):
        with self.outbox.deliveries.transaction() as cursor:
            assert_epoch(cursor, self.token)
            return read_server_cooldown(cursor, endpoint, mode)

    @asynccontextmanager
    async def _rate_slot(self, candidate, cooldown, monotonic_deadline):
        self._rate_waiters.add(candidate.delivery_id)
        try:
            while True:
                if self.clock() >= monotonic_deadline:
                    raise RateWaitExpired("rate_wait_deadline_expired")
                permit = await self.rate_gate.acquire(candidate.endpoint, candidate.send_mode,
                    certification=candidate.certification, deadline=candidate.expires_at,
                    read_blocked_until=cooldown)
                if not self._slots.locked():
                    # No await suspends between this check and successful acquire.
                    await self._slots.acquire()
                    break
                # Discard permits that aged while waiting for a network slot.
                try:
                    async with asyncio.timeout(max(0, monotonic_deadline - self.clock())):
                        async with self._slots:
                            pass
                except TimeoutError:
                    raise RateWaitExpired("rate_wait_deadline_expired") from None
        finally:
            self._rate_waiters.discard(candidate.delivery_id)
        try:
            observe(self.observer, 'rate', 'admitted')
            yield permit
        finally:
            self._slots.release()

    async def _capture(self, reservation, body):
        # This RAM evidence is deliberately retained on persistent DB failure.
        # Never enqueue/reprepare the delivery to "retry" a successful HTTP call.
        self._unrecorded[reservation.delivery_id] = (reservation, body)
        for attempt in range(self.receipt_retries):
            try:
                receipt = await self._calls(self.outbox.capture_receipt, reservation, body)
            except Exception:
                if attempt + 1 < self.receipt_retries:
                    await self.sleep(0.05 * (attempt + 1))
                    continue
                self.reason = "receipt_capture_unavailable"
                raise SenderUnavailable(self.reason) from None
            self._receipt_ids.add(receipt)
            self._unrecorded.pop(reservation.delivery_id, None)
            return receipt

    async def _persist_success(self, reservation, body):
        work = asyncio.create_task(self._capture(reservation, body))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            # A positive response must reach the late-receipt append even when
            # shutdown/lease loss cancels its caller. No fence is needed here.
            try:
                await _drain(work)
            except Exception:
                pass  # RAM remains; a new process conservatively recovers unknown.
            raise

    async def _finalize(self, receipt):
        await self._fence()
        result = await self._calls(self.outbox.finalize, self.token, receipt,
                                   ledger=self.ledger, journal=self.journal)
        # A returned result is durable; held receipts are rediscovered by the
        # paginated sweep, rather than accumulating in an unbounded RAM set.
        self._receipt_ids.discard(receipt)
        return result

    async def sweep_receipts(self):
        """One bounded round-robin page, including receipts captured by old owners.

        This only finalizes evidence: restart parking runs once in recover().
        Held/unfinalizable early rows cannot starve later pages or cause a tight
        loop. New rows inserted before the cursor appear on the next wrap.
        """
        try:
            await self._fence()
            receipts = await self._calls(self.outbox.pending_receipts, self.token,
                limit=100, after_receipt_id=self._receipt_cursor)
            results = []
            for receipt in receipts:
                results.append(await self._finalize(receipt))
            self._receipt_cursor = receipts[-1] if len(receipts) == 100 else None
            return tuple(results)
        except (LeaseLost, asyncio.CancelledError):
            raise
        except Exception:
            self.reason = "receipt_sweep_unavailable"
            raise SenderUnavailable(self.reason) from None

    async def recover(self):
        """Finalize durable/RAM receipts locally; never reconstruct an HTTP call."""
        async with self._recover_lock:
            try:
                await self._fence()
                if not self._recovered:
                    self._receipt_ids.update(await self._calls(self.outbox.recover, self.token))
                    self._recovered = True
                for reservation, body in tuple(self._unrecorded.values()):
                    await self._persist_success(reservation, body)
                results = []
                for receipt in tuple(self._receipt_ids):
                    results.append(await self._finalize(receipt))
                self.reason = "ready"
                return tuple(results)
            except (LeaseLost, asyncio.CancelledError):
                raise
            except Exception:
                self.reason = "receipt_recovery_unavailable"
                raise SenderUnavailable(self.reason) from None

    async def _prepare(self, delivery_id, permit):
        # DatabaseCalls deliberately drains writes before raising cancellation.
        # Shield the outer call as well to retain the newly issued handle; losing
        # it would lose the only proof permitting an unsent cancellation.
        work = asyncio.create_task(self._calls(self.outbox.prepare, self.token, delivery_id, permit))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            try:
                result = await _drain(work)
                if isinstance(result, SendReservation):
                    await self._cleanup(result, started=False)
            except Exception:
                pass
            raise

    async def _cleanup(self, reservation, *, started):
        async def settle():
            try:
                if started:
                    return await self._calls(self.outbox.fail, self.token, reservation,
                        QQAPIError("transport_ambiguous", stage="send"))
                return await self._calls(self.outbox.cancel_unsent, self.token, reservation,
                                         reason="request_not_started")
            except LeaseLost:
                return None  # A lost owner cannot reclaim authority to settle.
            except Exception:
                self.reason = "attempt_settlement_unavailable"
                return None  # Durable recovery retains uncertainty, never resends.
        return await _drain(asyncio.create_task(settle()))

    async def send_delivery(self, delivery_id):
        """One candidate/gate/prepare/HTTP cycle, without inline message retries."""
        if type(delivery_id) is not int or delivery_id < 1:
            raise SenderUnavailable("invalid_delivery_handle")
        if delivery_id in self._active:
            return SendOutcome("sending", "local_attempt_inflight", delivery_id)
        if self.reason != "ready":
            raise SenderUnavailable("sender_paused")
        self._active.add(delivery_id)
        reservation, started, accepted = None, False, False
        try:
            if not self._recovered:
                await self.recover()
            await self._fence()
            candidate = await self._calls(self.outbox.candidate, self.token, delivery_id)
            if isinstance(candidate, SendOutcome):
                return candidate
            if candidate.endpoint.account != self.account:
                raise SenderUnavailable("candidate_account_mismatch")
            if delivery_id in self._binding_ids and candidate.send_mode != "passive":
                return SendOutcome("cancelled", "binding_reply_window_required", delivery_id)
            monotonic_deadline = self.clock() + max(0, candidate.expires_at - self.wall_clock())

            async def cooldown():
                await self._fence()
                return await self._calls(self._cooldown, candidate.endpoint, candidate.send_mode)

            # A slow shared refresh must finish BEFORE rate admission. Otherwise
            # spaced permits can accumulate behind one refresh and wake in a burst.
            try:
                prepared = await self.rest.prepare_send(candidate.endpoint.kind,
                                                         candidate.endpoint.native_conversation_id)
            except (asyncio.CancelledError, LeaseLost):
                raise
            except Exception:
                self.reason = "token_preflight_unavailable"
                raise SenderUnavailable(self.reason) from None
            if self.clock() >= monotonic_deadline:
                return SendOutcome("pending", "send_deadline_expired", delivery_id)
            # Waiting candidates hold no durable attempt or window/quota charge.
            # Only actual dispatches occupy slots, so background rate waiters do
            # not exhaust the account's slots ahead of interactive passive sends.
            async with self._rate_slot(candidate, cooldown, monotonic_deadline) as permit:
                await self._fence()
                reservation = await self._prepare(delivery_id, permit)
                if isinstance(reservation, SendOutcome):
                    result, reservation = reservation, None
                    return result
                if not await self._calls(self.outbox.mark_dispatching, self.token, reservation):
                    result = await self._cleanup(reservation, started=False)
                    reservation = None
                    return result or SendOutcome("unavailable", "dispatch_revalidation_failed", delivery_id)
                await self._fence()
                remaining = min(self.request_timeout, reservation.send_by_at - self.wall_clock(),
                                monotonic_deadline - self.clock())
                if remaining <= 0:
                    result = await self._cleanup(reservation, started=False)
                    reservation = None
                    return result or SendOutcome("cancelled", "send_deadline_expired", delivery_id)
                if not self.rest.prepared_is_current(prepared):
                    # Known expiry/rotation is checked without I/O. A later
                    # attempt must prefetch again and pass the rate gate anew.
                    result = await self._cleanup(reservation, started=False)
                    reservation = None
                    return result or SendOutcome("unavailable", "prepared_token_unavailable", delivery_id)
                request_clock = None
                request_result = 'unknown'
                def physical_started():
                    nonlocal request_clock
                    if request_clock is None:
                        request_clock = self.clock()
                        observe(self.observer, 'send', 'started', mode=candidate.send_mode)
                try:
                    async with asyncio.timeout(remaining):
                        payload = self.outbox.request_started(reservation)
                        started = True
                        with scoped_request_observer(physical_started):
                            body = await self.rest.send_message(reservation.endpoint.kind,
                                reservation.endpoint.native_conversation_id, payload, prepared=prepared)
                    accepted = True
                    request_result = 'accepted'
                except QQAPIError as error:
                    request_result = 'confirmed_no' if error.accepted is False else 'unknown'
                    return await self._calls(self.outbox.fail, self.token, reservation, error)
                except TimeoutError:
                    return await self._calls(self.outbox.fail, self.token, reservation,
                        QQAPIError("transport_ambiguous", stage="send"))
                finally:
                    if request_clock is not None:
                        observe(self.observer, 'send', request_result, mode=candidate.send_mode,
                                seconds=max(0, self.clock() - request_clock))
                receipt = await self._persist_success(reservation, body)
                return await self._finalize(receipt)
        except RateWaitExpired:
            observe(self.observer, 'rate', 'expired')
            return SendOutcome("pending", "rate_wait_expired", delivery_id)
        except asyncio.CancelledError:
            if reservation is not None and not accepted:
                await self._cleanup(reservation, started=started)
            raise
        except LeaseLost:
            raise LeaseLost("Official sender account lease lost") from None
        except Exception:
            if reservation is not None and not accepted:
                await self._cleanup(reservation, started=started)
            if self.reason == "ready":
                self.reason = "sender_storage_or_transport_unavailable"
            raise SenderUnavailable(self.reason) from None
        finally:
            self._active.discard(delivery_id)

    async def run(self):
        """Poll existing durable work; no new queue or global FIFO is introduced."""
        if self._running:
            raise SenderUnavailable("sender_already_running")
        self._running = True
        try:
            await self.recover()
            next_sweep = self.clock() + self.receipt_sweep_seconds
            while not self._stopping:
                self._wake.clear()
                for delivery_id, task in tuple(self._tasks.items()):
                    if task.done():
                        del self._tasks[delivery_id]
                        task.result()  # Fixed-safe exceptions stop this account.
                await self._fence()
                if self.clock() >= next_sweep:
                    await self.sweep_receipts()
                    next_sweep = self.clock() + self.receipt_sweep_seconds
                due = await self._calls(self.outbox.list_due, self.token, limit=self.max_waiters)
                async with self._schedule_lock:
                    positions = {delivery_id: index for index, delivery_id in enumerate(due)}
                    preempted = False
                    for index, delivery_id in enumerate(due):
                        if delivery_id in self._tasks or delivery_id in self._binding_ids:
                            continue
                        if len(self._tasks) >= self.max_waiters:
                            victim = next((key for key in reversed(self._tasks)
                                if key in self._rate_waiters and positions.get(key, len(due)) > index), None)
                            if preempted or victim is None:
                                break
                            # Only a lower-priority unclaimed waiter can yield
                            # capacity. Drain/remove it before filling its slot;
                            # never cancel a prepared/in-flight physical request.
                            preempted = True
                            work = self._tasks[victim]
                            work.cancel()
                            await asyncio.gather(work, return_exceptions=True)
                            self._tasks.pop(victim, None)
                        if self._stopping:
                            break
                        task = asyncio.create_task(self.send_delivery(delivery_id))
                        task.add_done_callback(lambda _: self.wake())
                        self._tasks[delivery_id] = task
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)
                except TimeoutError:
                    pass
        except (LeaseLost, asyncio.CancelledError):
            raise
        except Exception:
            raise SenderUnavailable("sender_loop_unavailable") from None
        finally:
            for task in self._tasks.values():
                task.cancel()
            await _drain(asyncio.create_task(self._drain_tasks()))
            self._running = False

    async def _drain_tasks(self):
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    async def stop(self):
        self._stopping = True
        self.wake()
        for task in self._tasks.values():
            task.cancel()
        await self._drain_tasks()

    def _binding_plan(self, effect):
        now = int(self.wall_clock())
        if (not isinstance(effect, BindingEffect) or effect.kind not in {"issue_code", "candidate_preview"}
                or effect.endpoint.account != self.account or effect.endpoint.kind != "private"
                or not re.fullmatch(r"BC-[0-9a-f]{32}", effect.challenge_id)
                or (effect.kind == "issue_code" and not re.fullmatch(r"[0-9]{6}", effect.code))):
            return None
        preview = effect.kind == "candidate_preview"
        if preview and (not re.fullmatch(r"BN-[0-9a-f]{32}", effect.candidate_id)
                        or type(effect.candidate_version) is not int or effect.candidate_version < 1):
            return None
        with self.outbox.deliveries.transaction() as cursor:
            assert_epoch(cursor, self.token)
            row = cursor.execute("""SELECT c.* FROM qq_official_binding_challenges c
                JOIN qq_official_owner_grants g ON g.grant_id=c.owner_grant_id
                WHERE c.challenge_id=? AND c.account_key=? AND c.state=?
                  AND c.expires_at>? AND g.state='active'
                  AND g.revision=c.owner_revision""",
                (effect.challenge_id, self.account.key, "candidate" if preview else "sending", now)).fetchone()
            if row is None or row["issuer_endpoint_key"] != effect.endpoint.key:
                return None
            if not preview and row["delivery_epoch"] != self.token.epoch:
                return None
            endpoint = EndpointKey.parse(row["issuer_endpoint_key"])
            actor = ActorKey.parse(row["issuer_actor_key"])
            if not actor.matches_endpoint(endpoint) or actor.identity_kind != "c2c_user":
                return None
            # C2C endpoint identifies the real issuer; no app ID or requester
            # projection can manufacture this source. OTP never uses proactive.
            window = cursor.execute("""SELECT w.* FROM qq_official_reply_windows w
                JOIN qq_official_endpoints e ON e.endpoint_key=w.endpoint_key
                WHERE w.account_key=? AND w.endpoint_key=? AND w.credential_kind='msg_id'
                  AND w.state='active' AND w.expires_at>? AND w.endpoint_generation=e.generation
                  AND w.route_epoch=e.route_epoch AND w.used_count+w.reserved_count<w.max_replies
                ORDER BY w.issued_at DESC,w.window_id LIMIT 1""", (self.account.key, endpoint.key, now)).fetchone()
            if window is None:
                return None
            expires = min(now + 30, row["expires_at"], window["expires_at"])
            if preview:
                candidate = cursor.execute("""SELECT * FROM qq_official_binding_candidates
                    WHERE candidate_id=? AND challenge_id=? AND account_key=? AND version=?
                      AND state='pending' AND expires_at>?""",
                    (effect.candidate_id, row["challenge_id"], self.account.key,
                     effect.candidate_version, now)).fetchone()
                if candidate is None:
                    return None
                group = EndpointKey.parse(candidate["endpoint_key"])
                submitter = ActorKey.parse(candidate["actor_key"])
                if group.account != self.account or group.kind != "group" or not submitter.matches_endpoint(group):
                    return None
                expires = min(expires, candidate["expires_at"])
                text = (f"待确认绑定群：{group.native_conversation_id}\n"
                    f"群内提交者：{submitter.native_user_id}\n候选：{candidate['candidate_id']}\n"
                    f"版本：{candidate['version']}\n授予提交者 owner：默认关闭\n"
                    f"确认：/bind confirm {candidate['candidate_id']} {candidate['version']}\n"
                    f"拒绝：/bind reject {candidate['candidate_id']} {candidate['version']}")
                rendered = render_plan(MessageBody((TextNode(0, text),)), endpoint, markdown=False, max_parts=1)
                return self.outbox.enqueue(self.token, rendered=rendered, requester=actor,
                    logical_key=f"binding_preview:{effect.candidate_id}:{effect.candidate_version}:{window['window_id']}",
                    source_scope_key=endpoint.scope.key, expires_at=expires,
                    source_native_message_id=window["credential_value"], transaction_cursor=cursor,
                    binding_preview=(effect.candidate_id, effect.candidate_version))
            text = f"绑定挑战：{effect.challenge_id}\n一次性验证码：{effect.code}\n请在有效期内到目标群提交绑定请求。"
            rendered = render_plan(MessageBody((TextNode(0, text),)), endpoint, markdown=False, max_parts=1)
            return self.outbox.enqueue(self.token, rendered=rendered, requester=actor,
                logical_key="binding_otp:" + effect.challenge_id, source_scope_key=endpoint.scope.key,
                expires_at=expires, purpose="binding_otp", challenge_id=effect.challenge_id,
                source_native_message_id=window["credential_value"], transaction_cursor=cursor)

    def _cancel_binding_pending(self, delivery_id):
        with self.outbox.deliveries.transaction() as cursor:
            assert_epoch(cursor, self.token)
            cursor.execute("""UPDATE deliveries SET status='cancelled',wait_reason='binding_send_expired',updated_at=?
                WHERE delivery_id=? AND account_key=? AND payload_mode='ephemeral' AND status='pending'""",
                (int(self.wall_clock()), delivery_id, self.account.key))

    async def emit_binding(self, effect):
        """OTP or verified owner preview, <=30s synchronous dispatch deadline.

        Returns runtime's success/failed/unknown vocabulary. Runtime owns the
        challenge transition. Cancellation drains in-flight DB writes for safety;
        it never extends the wire deadline or retries an OTP's physical request.
        Preview metadata is sealed in its ordinary plan; outbox revalidates the
        exact pending candidate/version/grant on every subsequent retry.
        """
        return await self.emit_ephemeral(self._binding_plan, effect)

    async def emit_ephemeral(self, prepare, *args):
        """Internal host plan port; no authority is conferred by this callback.

        Outbox validates the persisted source and one-attempt constraints. This
        is shared with binding OTP, never an independent queue or HTTP path.
        """
        delivery_id = None
        try:
            async with asyncio.timeout(30):
                if not self._recovered:
                    await self.recover()
                await self._fence()
                async with self._schedule_lock:
                    work = asyncio.create_task(self._calls(prepare, *args))
                    try:
                        ref = await asyncio.shield(work)
                    except asyncio.CancelledError:
                        ref = await _drain(work)
                        if ref is not None and ref.created:
                            delivery_id = ref.delivery_ids[0]
                            self._binding_ids.add(delivery_id)
                        raise
                    if ref is None or not ref.created:
                        return "failed"
                    delivery_id = ref.delivery_ids[0]
                    self._binding_ids.add(delivery_id)
                result = await self.send_delivery(delivery_id)
                if result.status == "committed":
                    return "success"
                return "unknown" if result.status in {"accepted", "ambiguous", "sending"} else "failed"
        except (asyncio.CancelledError, LeaseLost):
            raise
        except Exception:
            return "unknown"
        finally:
            if delivery_id is not None:
                async def cleanup():
                    try:
                        await self._calls(self._cancel_binding_pending, delivery_id)
                    except Exception:
                        pass
                    self.outbox.forget_ephemeral(delivery_id)
                    self._binding_ids.discard(delivery_id)
                await _drain(asyncio.create_task(cleanup()))
