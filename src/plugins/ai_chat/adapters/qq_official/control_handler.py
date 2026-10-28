"""Existing durable-job handler: source decision -> sole M3 plan, no new queue."""
from __future__ import annotations

import asyncio
import hashlib
import re
import json

from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.adapters.qq_official.renderer import render_plan
from src.plugins.ai_chat.adapters.qq_official.outbox_types import PlanRef
from src.plugins.ai_chat.storage.jobs import DurableJob
from src.plugins.ai_chat.workers.durable_jobs import JobDeferred
from .control_receipts import ControlEvidenceError, control_job_kind, load_authority


def handoff_control_reply(cursor, *, token, job, outbox, now):
    assert_epoch(cursor, token)
    if not isinstance(job, DurableJob) or set(job.payload) != {'control_id'}:
        raise ControlEvidenceError('invalid_control_job')
    control_id = job.payload['control_id']
    if not isinstance(control_id, str) or re.fullmatch(r'[a-f0-9]{32}', control_id) is None:
        raise ControlEvidenceError('invalid_control_job')
    row = cursor.execute('SELECT * FROM qq_official_control_decisions WHERE control_id=? AND account_key=?',
                         (control_id, token.account_key)).fetchone()
    if row is None:
        raise ControlEvidenceError('control_job_source_missing')
    cursor.execute('SELECT endpoint_key FROM qq_official_endpoints WHERE endpoint_key=? FOR UPDATE', (row['endpoint_key'],))
    cursor.execute('SELECT inbox_id FROM qq_official_inbound_events WHERE inbox_id=? FOR UPDATE', (row['inbox_id'],))
    current = cursor.execute('SELECT * FROM durable_jobs WHERE job_id=? FOR UPDATE', (job.job_id,)).fetchone()
    if (current is None or row['job_id'] != job.job_id or job.kind != control_job_kind(token.account_key)
            or current['kind'] != job.kind or current['scope_key'] != job.scope_key
            or current['scope_key'] != 'qq.control.lane:' + hashlib.sha256(row['endpoint_key'].encode()).hexdigest()
            or json.loads(current['payload_json']) != job.payload
            or current['idempotency_key'] != 'qq.control:' + control_id or job.idempotency_key != current['idempotency_key']
            or current['status'] != 'running' or current['lease_owner'] != job.lease_owner
            or current['lease_generation'] != job.lease_generation or current['attempts'] != job.attempts
            or current['lease_until'] is None or current['lease_until'] <= now or not job.lease_owner):
        raise ControlEvidenceError('control_job_lease_lost')
    if row['plan_id'] is not None:
        return PlanRef(row['plan_id'], (row['delivery_id'],), False)
    view = load_authority(cursor, token, control_id=control_id, now=now)
    if view is None:
        cursor.execute("UPDATE qq_official_control_decisions SET state='terminal',updated_at=? WHERE control_id=? AND plan_id IS NULL",
                       (now, control_id))
        return None
    rendered = render_plan(view.body, view.endpoint, max_parts=1, markdown=False)
    return outbox.enqueue(token, rendered=rendered, requester=view.actor, logical_key='control:' + control_id,
        source_scope_key=view.endpoint.scope.key, source_native_message_id=row['source_native_message_id'],
        expires_at=view.expires_at, control_reply_id=control_id, transaction_cursor=cursor)


class ControlJobHandler:
    def __init__(self, *, token, outbox, sender, database_calls, ensure_current, clock):
        self.token, self.outbox, self.sender = token, outbox, sender
        self.calls, self.ensure_current, self.clock = database_calls, ensure_current, clock

    async def __call__(self, job):
        try:
            await self.ensure_current()
            def handoff():
                with self.outbox.deliveries.transaction() as cursor:
                    return handoff_control_reply(cursor, token=self.token, job=job, outbox=self.outbox, now=int(self.clock()))
            result = await self.calls(handoff)
            if result is not None:
                self.sender.wake()
            return {'status': 'handed_off' if result is not None else 'suppressed'}
        except asyncio.CancelledError:
            raise
        except ControlEvidenceError:
            raise JobDeferred('official_control_evidence_unavailable', 3) from None
        except Exception:
            # The generic worker persists and logs exception strings. Never
            # propagate a database/transport error that may embed private IDs.
            raise JobDeferred('official_control_handoff_unavailable', 3) from None
