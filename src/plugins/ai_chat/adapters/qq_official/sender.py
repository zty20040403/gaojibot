"""Ordered per-conversation sending with in-memory limits and explicit outcomes."""
from __future__ import annotations
import asyncio
from functools import partial
import logging
import re
from threading import BoundedSemaphore
import time
import traceback
from weakref import WeakValueDictionary

from src.plugins.ai_chat.billing_context import drain
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from src.qq_official.errors import QQAPIError
from .binding import BindingEffect
from .lease import LeaseLost, assert_epoch
from .send_types import SendOutcome
from .rate_limit import RateLimiter
from .renderer import render_plan
from .official_metrics import observe

log = logging.getLogger(__name__)
PASSIVE_UNAVAILABLE = 'proactive_disabled_no_reply_window'
MAX_IN_FLIGHT = 32
_SEND_SLOTS = BoundedSemaphore(MAX_IN_FLIGHT)
_WAIT_SLOTS = BoundedSemaphore(64)


class SenderUnavailable(RuntimeError):
    """Local wait budget exhausted, not a durable delivery failure."""


def _stored_outcome(row, delivery_id):
    if row is None:
        return SendOutcome('failed', 'delivery_not_found', delivery_id)
    return SendOutcome(row['status'], row['wait_reason'] or '', delivery_id)


class OfficialSender:
    def __init__(self, account, token, outbox, rest, ledger, *, database_calls, ensure_current,
                 journal=None, rate_gate=None, clock=time.monotonic, wall_clock=time.time,
                 sleep=asyncio.sleep, poll_seconds=0.5, request_timeout=30, observer=None):
        self.account, self.token, self.outbox, self.rest = account, token, outbox, rest
        self.ledger, self.journal = ledger, journal
        self._calls, self.ensure_current = database_calls, ensure_current
        self.clock, self.wall_clock, self.sleep = clock, wall_clock, sleep
        self.rate_gate = rate_gate or RateLimiter(clock=clock, sleep=sleep)
        self.poll_seconds, self.request_timeout, self.observer = poll_seconds, request_timeout, observer
        self._tasks, self._passive = {}, {}
        self._endpoints = WeakValueDictionary()
        self._receipts, self._unrecorded = set(), {}
        self._wake, self._stopped, self._recover_lock = asyncio.Event(), asyncio.Event(), asyncio.Lock()
        self._recovered = self._running = False

    def wake(self):
        self._wake.set()

    async def refresh_group_state(self, endpoint):
        if endpoint.kind != 'group':
            return
        try:
            state = await self.rest.group_bot_state(endpoint.native_conversation_id)
            allowed = state.get('allow_proactive_msg')
            if type(allowed) is not bool:
                return
            def save():
                with self.outbox.deliveries.transaction() as cursor:
                    assert_epoch(cursor, self.token)
                    cursor.execute('''UPDATE qq_official_endpoints SET proactive_state=?,
                        proactive_probe_attempt_id=NULL,proactive_probe_at=NULL,updated_at=? WHERE endpoint_key=? AND account_key=?''',
                        ('allowed' if allowed else 'denied', int(self.wall_clock()), endpoint.key, self.account.key))
            await self._calls(save)
            self.wake()
        except LeaseLost:
            self._halt()
            raise
        except Exception as error:
            frames = traceback.extract_tb(error.__traceback__)[-4:]
            logging.getLogger(__name__).warning('QQ bot_state failed: %s %s', type(error).__name__,
                ' > '.join(f'{frame.name}:{frame.lineno}' for frame in frames))

    async def refresh_bound_groups(self):
        def groups():
            with self.outbox.deliveries.transaction() as cursor:
                return cursor.execute('''SELECT endpoint_key FROM qq_official_endpoints
                    WHERE account_key=? AND kind='group' AND binding_state='active'
                    AND membership_state='present' ''', (self.account.key,)).fetchall()
        for row in await self._calls(groups):
            await self.refresh_group_state(EndpointKey.parse(row['endpoint_key']))

    async def _fence(self):
        if self._stopped.is_set():
            raise LeaseLost("Official sender stopped")
        try:
            await self.ensure_current()
        except LeaseLost:
            self._halt()
            raise

    async def recover(self):
        async with self._recover_lock:
            if not self._recovered:
                await self._fence()
                self._receipts.update(await self._calls(self.outbox.recover, self.token))
                self._recovered = True
            for delivery_id, (reservation, body) in tuple(self._unrecorded.items()):
                receipt = await self._calls(self.outbox.capture_receipt, reservation, body)
                self._receipts.add(receipt)
                self._unrecorded.pop(delivery_id, None)
            for receipt in tuple(self._receipts):
                await self._calls(self.outbox.finalize, self.token, receipt, ledger=self.ledger, journal=self.journal)
                self._receipts.discard(receipt)

    async def _record(self, reservation, body):
        self._unrecorded[reservation.delivery_id] = (reservation, body)
        receipt = await self._calls(self.outbox.capture_receipt, reservation, body)
        self._receipts.add(receipt)
        self._unrecorded.pop(reservation.delivery_id, None)
        result = await self._calls(self.outbox.finalize, self.token, receipt, ledger=self.ledger, journal=self.journal)
        self._receipts.discard(receipt)
        return result

    def _submit(self, delivery_id):
        if self._stopped.is_set():
            raise LeaseLost("Official sender stopped")
        if delivery_id in self._tasks:
            return self._tasks[delivery_id]
        if not _SEND_SLOTS.acquire(blocking=False):
            return None
        task = self._tasks[delivery_id] = asyncio.create_task(self._deliver(delivery_id))
        task.add_done_callback(partial(self._finished, delivery_id))
        return task

    def _finished(self, delivery_id, task):
        del self._tasks[delivery_id]
        _SEND_SLOTS.release()
        if not task.cancelled() and (error := task.exception()) and not isinstance(error, LeaseLost):
            log.error('QQ send failed delivery=%s: %s %s', delivery_id, type(error).__name__,
                ' > '.join(f'{f.name}:{f.lineno}' for f in traceback.extract_tb(error.__traceback__)[-4:]))

    async def _deliver(self, delivery_id):
        try:
            if not self._recovered or self._unrecorded or self._receipts:
                await self.recover()
            row = await self._calls(self.outbox.dispatch_row, self.token, delivery_id)
        except Exception:
            self._halt()
            raise
        if row is None or row['status'] != 'pending':
            return _stored_outcome(row, delivery_id)
        if not row['dispatchable']:
            return None
        async with self._endpoints.setdefault(row['endpoint_key'], asyncio.Lock()):
            try:
                return await self._send(delivery_id)
            except LeaseLost:
                self._halt()
                raise
            except Exception as error:
                # A platform response awaiting capture is evidence, not an unknown send.
                if delivery_id in self._unrecorded:
                    raise
                try:
                    await drain(asyncio.create_task(self._calls(self.outbox.settle_send_error, self.token, delivery_id)))
                except Exception as settlement:
                    self._halt()
                    log.error('QQ send settlement failed delivery=%s: %s', delivery_id, type(settlement).__name__)
                    raise error from settlement
                raise

    async def send_delivery(self, delivery_id):
        if not _WAIT_SLOTS.acquire(blocking=False):
            self.wake()
            raise SenderUnavailable('QQ outbox wait capacity exhausted')
        try:
            while True:
                task = self._submit(delivery_id)
                if task is not None and (result := await asyncio.shield(task)) is not None:
                    return result
                await self._fence()
                try:
                    async with asyncio.timeout(self.poll_seconds):
                        await self._stopped.wait()
                except TimeoutError:
                    pass
        finally:
            _WAIT_SLOTS.release()

    async def _credential(self, endpoint, route, *, proactive_only=False):
        if route['proactive_state'] != 'denied':
            return 'proactive', None, None
        if proactive_only:
            return None
        source = await self._calls(self.outbox.latest_inbound, endpoint)
        if source is None or self.wall_clock() - source['received_at'] >= 300:
            return None
        msg_id = source['native_message_id']
        current = self._passive.get(endpoint.key)
        if current is None or current['id'] != msg_id:
            history = await self._calls(self.outbox.passive_history, endpoint, msg_id)
            current = self._passive[endpoint.key] = {'id':msg_id, 'used':history['used'], 'seq':history['seq']}
        if current['used'] >= (5 if endpoint.kind == 'group' else 4):
            return None
        current['seq'] += 1
        current['used'] += 1
        return 'passive', msg_id, current['seq']

    async def _send(self, delivery_id):
        auth_retry = plain_retry = conn_retry = False
        while True:
            await self._fence()
            row = await self._calls(self.outbox.get, self.token, delivery_id)
            if row is None or row['status'] != 'pending':
                return _stored_outcome(row, delivery_id)
            endpoint = EndpointKey.parse(row['endpoint_key'])
            await self.rate_gate.acquire(endpoint)
            await self._fence()
            route = await self._calls(self.outbox.route, endpoint)
            try:
                prepared = await self.rest.prepare_send(endpoint.kind, endpoint.native_conversation_id)
            except QQAPIError:
                return await self._calls(self.outbox.fail, self.token, delivery_id, 'token_unavailable')
            bot_origin = ActorKey.parse(row['requester_actor_key']).identity_kind == 'bot'
            proactive_only = bot_origin or (row['purpose'] == 'unsolicited' and row['source_canonical_message_id'] is not None)
            credential = await self._credential(endpoint, route, proactive_only=proactive_only)
            if credential is None:
                log.warning('QQ send failed: 这个群没开主动发言，请群主打开')
                return await self._calls(self.outbox.fail, self.token, delivery_id, PASSIVE_UNAVAILABLE)
            mode, msg_id, seq = credential
            reservation = await self._calls(self.outbox.prepare, self.token, delivery_id,
                send_mode=mode, msg_id=msg_id, msg_seq=seq)
            if isinstance(reservation, SendOutcome):
                if mode == 'passive':
                    self._passive[endpoint.key]['used'] -= 1
                return reservation
            denied = await self._calls(self.outbox.attempt_denial, self.token, reservation)
            if denied:
                if mode == 'passive':
                    self._passive[endpoint.key]['used'] -= 1
                return await self._calls(self.outbox.fail, self.token, delivery_id,
                    denied, reservation=reservation)
            started = self.clock()
            payload = reservation.payload()
            error = None
            observe(self.observer, 'send', 'started', mode=mode)
            try:
                async with asyncio.timeout(self.request_timeout):
                    response = await self.rest.send_message(endpoint.kind, endpoint.native_conversation_id, payload, prepared=prepared)
            except QQAPIError as caught:
                error = caught
            except (TimeoutError, asyncio.CancelledError) as caught:
                log.info('QQ send mode=%s seconds=%.2f result=unknown code=timeout', mode, self.clock() - started)
                error = QQAPIError('transport_ambiguous', stage='send')
                await drain(asyncio.create_task(self._calls(self.outbox.fail, self.token, delivery_id,
                    'possibly_not_sent', reservation=reservation, error=error)))
                if isinstance(caught, asyncio.CancelledError):
                    raise
                return SendOutcome('ambiguous', 'possibly_not_sent', delivery_id)
            elapsed = self.clock() - started
            observe(self.observer, 'send', 'unknown' if error and error.accepted is not False else 'confirmed_no' if error else 'accepted',
                mode=mode, seconds=elapsed)
            log.info('QQ send mode=%s seconds=%.2f result=%s code=%s', mode, elapsed,
                error.category if error else 'accepted', error.code if error else 0)
            if error is None:
                return await drain(asyncio.create_task(self._record(reservation, response)))
            if error.accepted is not False:
                return await self._calls(self.outbox.fail, self.token, delivery_id,
                    'possibly_not_sent', reservation=reservation, error=error)
            if mode == 'passive':
                self._passive[endpoint.key]['used'] -= 1
            retry, plain, delay = False, False, 0
            if error.code == 40034006:
                reason = 'content_rejected'
            elif error.code == 40034105 and mode == 'proactive':
                await self._calls(self.outbox.deny_proactive, self.token, endpoint)
                retry, reason = not proactive_only, 'proactive_denied'
            elif error.code == 40034128:
                if mode == 'passive':
                    self._passive[endpoint.key]['used'] = 5 if endpoint.kind == 'group' else 4
                reason = PASSIVE_UNAVAILABLE
            elif error.status == 401 or error.category == 'authentication':
                token_manager = getattr(self.rest, 'token_manager', None)
                if token_manager is not None:
                    token_manager.invalidate()
                retry, reason = not auth_retry, 'authentication'
                auth_retry = True
            elif error.status == 429 or error.category == 'rate_limited':
                retry, delay, reason = True, max(1, error.retry_after or 3), 'rate_limited'
            elif payload.get('msg_type') == 2 and not plain_retry and row['variant'] != 'plain_fallback':
                plain_retry, retry, plain, reason = True, True, True, 'markdown_rejected'
            elif error.category == 'connection_failed' and not conn_retry:
                conn_retry, retry, reason = True, True, 'connection_failed'
            else:
                reason = error.category
            result = await self._calls(self.outbox.fail, self.token, delivery_id, reason,
                reservation=reservation, error=error, retry=retry, plain=plain)
            if not retry:
                return result
            if delay:
                await self.sleep(delay)

    async def run(self):
        try:
            while not self._stopped.is_set():
                self._wake.clear()
                await self._fence()
                await self.recover()
                self._running = not self._stopped.is_set()
                due = await self._calls(self.outbox.list_due, self.token, limit=MAX_IN_FLIGHT)
                for delivery_id in due:
                    if self._stopped.is_set() or self._submit(delivery_id) is None:
                        break
                try:
                    async with asyncio.timeout(self.poll_seconds):
                        await self._wake.wait()
                except TimeoutError:
                    pass
        finally:
            await self.stop()

    def _halt(self):
        self._running = False
        self._stopped.set()
        self.wake()
        for task in self._tasks.values():
            if task is not asyncio.current_task() and not task.cancelling():
                task.cancel()

    async def stop(self):
        self._halt()
        await drain(asyncio.gather(*self._tasks.values(), return_exceptions=True))

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
            source = cursor.execute("""SELECT native_message_id FROM qq_official_inbound_events
                WHERE endpoint_key=? AND native_message_id IS NOT NULL AND echo_state='not_echo'
                ORDER BY accepted_order DESC LIMIT 1""", (endpoint.key,)).fetchone()
            if source is None:
                return None
            expires = min(now + 30, row['expires_at'])
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
                    logical_key=f"binding_preview:{effect.candidate_id}:{effect.candidate_version}:{source['native_message_id']}",
                    source_scope_key=endpoint.scope.key, expires_at=expires,
                    source_native_message_id=source["native_message_id"], transaction_cursor=cursor,
                    binding_preview=(effect.candidate_id, effect.candidate_version))
            text = f"请在目标群里 @ 我并发送：\n/bind {effect.code}\n验证码 10 分钟内有效，提交后立即绑定。"
            rendered = render_plan(MessageBody((TextNode(0, text),)), endpoint, markdown=False, max_parts=1)
            return self.outbox.enqueue(self.token, rendered=rendered, requester=actor,
                logical_key="binding_otp:" + effect.challenge_id, source_scope_key=endpoint.scope.key,
                expires_at=expires, purpose="binding_otp", challenge_id=effect.challenge_id,
                source_native_message_id=source["native_message_id"], transaction_cursor=cursor)

    def _cancel_binding_pending(self, delivery_id):
        with self.outbox.deliveries.transaction() as cursor:
            assert_epoch(cursor, self.token)
            cursor.execute("""UPDATE deliveries SET status='cancelled',wait_reason='binding_send_expired',updated_at=?
                WHERE delivery_id=? AND account_key=? AND payload_mode='ephemeral' AND status='pending'""",
                (int(self.wall_clock()), delivery_id, self.account.key))

    async def emit_binding(self, effect):
        if effect.kind == 'owner_link_code':
            await self._fence()
            await self.rate_gate.acquire(effect.endpoint)
            await self._fence()
            prepared = await self.rest.prepare_send('private', effect.endpoint.native_conversation_id)
            await self._fence()
            try:
                await self.rest.send_message('private', effect.endpoint.native_conversation_id,
                    {'msg_type': 0, 'content': f'群绑定码：{effect.code}。5 分钟内在目标群发送此码，使用后即失效。',
                     'msg_id': effect.source_native_message_id, 'msg_seq': 1}, prepared=prepared)
            except QQAPIError as error:
                return 'failed' if error.accepted is False else 'unknown'
            return 'success'
        return await self.emit_ephemeral(self._binding_plan, effect)

    async def emit_ephemeral(self, prepare, *args):
        delivery_id = None
        try:
            async with asyncio.timeout(30):
                ref = await self._calls(prepare, *args)
                if ref is None or not ref.created:
                    return 'failed'
                delivery_id = ref.delivery_ids[0]
                result = await self.send_delivery(delivery_id)
                if result.status == 'committed':
                    return 'success'
                return 'unknown' if result.status in {'accepted','ambiguous','sending'} else 'failed'
        except (asyncio.CancelledError, LeaseLost):
            raise
        except Exception:
            return 'unknown'
        finally:
            if delivery_id is not None:
                await self._calls(self._cancel_binding_pending, delivery_id)
                self.outbox.forget_ephemeral(delivery_id)
