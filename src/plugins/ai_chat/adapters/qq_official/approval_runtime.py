"""Explicit, epoch-scoped composition of the EXISTING approval state machine.

Only the trusted runtime factory constructs this service. Command bodies and OTP
codes remain RAM-only; durable gateway receipts authorize reduction in TxA. This
is not another approval worker/queue and exposes no model-callable authority.
"""
from __future__ import annotations

import asyncio
import inspect

from src.bot_security.store import SecurityError
from .approval_control import ControlResult
from .approval_transport import OfficialApprovalTransport, ApprovalOtpSender


class OfficialApprovalService:
    def __init__(self, *, token, mobile, host_admission=None, host_session_factory=None, close=None):
        transport = mobile.store.transport
        if (not isinstance(transport, OfficialApprovalTransport) or transport.token != token
                or not isinstance(mobile.official_sender, ApprovalOtpSender)
                or mobile.official_sender.transport is not transport
                or mobile.official_sender.store is not mobile.store
                or (host_admission is None) != (host_session_factory is None)
                or host_admission is not None and (not callable(host_admission) or not callable(host_session_factory))
                or close is not None and not callable(close)):
            raise SecurityError('official_approval_service_invalid')
        self.token, self.mobile, self.transport = token, mobile, transport
        self.host_admission, self.host_session_factory = host_admission, host_session_factory
        self._close, self._closed = close, False

    def attach(self, runtime, token):
        if (self._closed or token != self.token or runtime.sender is not self.mobile.official_sender.sender
                or runtime.outbox is not runtime.sender.outbox
                or runtime.outbox.approval_authority not in (None, self.transport)):
            raise SecurityError('official_approval_service_mismatch')
        runtime.outbox.approval_authority = self.transport
        runtime.store.approval_store = self.mobile.store
        # Restart never reconstructs an OTP. Only fresh commands can create a
        # new approved source; old sending codes are cancelled under the epoch.
        with runtime.store.transaction() as cursor:
            self.transport.recover(cursor)

    async def consume(self, result):
        if self._closed or not isinstance(result, ControlResult) or result.duplicate:
            return
        if result.decision == 'sending' and result.request is not None and result.code is not None:
            try:
                await self.mobile._send_code(result.request, result.code)
            except asyncio.CancelledError:
                raise
            except Exception:
                # _send_code settles failed/unknown; neither event nor exception
                # is logged, persisted or retried with the raw code.
                pass
        self.mobile.wake.set()

    async def run(self):
        if self._closed:
            raise SecurityError('official_approval_service_closed')
        await self.mobile.run_forever()

    async def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._close is not None:
                result = self._close()
                if inspect.isawaitable(result):
                    await result
        finally:
            await asyncio.to_thread(self.mobile.store.close)
