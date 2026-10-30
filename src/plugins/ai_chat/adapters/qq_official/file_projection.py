"""Read attachment delivery facts without granting upload or send authority."""
from __future__ import annotations

from src.plugins.ai_chat.agent.artifact_delivery import ArtifactDeliveryView
from .protected_plans import load_protected_ref
from .send_types import OutboxInvariantError


def apply_file_receipt(cursor, *, delivery, attempt, now):
    """Called after genuine M3 evidence was atomically applied to the ledger.

    Keep the historical fact even after cancellation, credential expiration or
    byte deletion. No source lease, local vault or publication read is needed.
    """
    if delivery['payload_mode'] != 'protected':
        return
    row = cursor.execute('''SELECT * FROM qq_official_file_message_plans
        WHERE plan_id=? AND delivery_id=? FOR SHARE''',
        (delivery['plan_id'], delivery['delivery_id'])).fetchone()
    if row is None:
        # A generic protected port need not be an official file catalog. Absence
        # never invents an artifact or advances any file projection.
        return
    ref = load_protected_ref(cursor, delivery)
    if (row['artifact_id'] != ref.artifact_id or row['credential_ref'] != ref.credential_ref
            or row['account_key'] != delivery['account_key'] or row['endpoint_key'] != delivery['endpoint_key']
            or row['requester_actor_key'] != delivery['requester_actor_key']):
        raise OutboxInvariantError('file_receipt_association_conflict')
    evidence = cursor.execute('''SELECT 1 FROM delivery_receipts r
        JOIN delivery_attempts a ON a.attempt_id=r.attempt_id
        JOIN deliveries d ON d.delivery_id=a.delivery_id AND d.current_attempt_id=a.attempt_id
        WHERE r.attempt_id=? AND r.applied_at IS NOT NULL AND d.delivery_id=?
          AND a.state='committed' AND a.acceptance='confirmed_yes' AND d.status='committed'
          AND r.account_key=d.account_key AND r.endpoint_key=d.endpoint_key
          AND r.native_message_id=d.native_message_id LIMIT 1''',
        (attempt['attempt_id'], delivery['delivery_id'])).fetchone()
    if evidence is None:
        raise OutboxInvariantError('file_receipt_not_applied')
    cursor.execute('''UPDATE qq_official_file_deliveries
        SET state='message_accepted',reason_code='',updated_at=?
        WHERE artifact_id=? AND active_plan_id=? AND credential_ref=?''',
        (now, ref.artifact_id, delivery['plan_id'], ref.credential_ref))
    if cursor.rowcount != 1:
        raise OutboxInvariantError('file_receipt_active_plan_conflict')


def project_task_files(cursor, task, revision):
    """Exact owned task identity filters; returns metadata only, never credentials."""
    rows = cursor.execute('''SELECT a.*,f.state AS file_state,f.updated_at,
        d.status AS message_state FROM qq_official_file_artifacts a
        JOIN qq_official_file_deliveries f ON f.artifact_id=a.artifact_id
        LEFT JOIN deliveries d ON d.plan_id=f.active_plan_id AND d.part_index=0
        WHERE a.source_task_id=? AND a.source_task_revision=? AND a.account_key=?
          AND a.endpoint_key=? AND a.requester_actor_key=? ORDER BY a.created_at,a.artifact_id LIMIT 33''',
        (task.task_id, revision, task.endpoint.account.key, task.endpoint.key, task.requester_actor.key)).fetchall()
    if len(rows) > 32:
        raise OutboxInvariantError('file_projection_limit')
    result = []
    for row in rows:
        state = row['file_state']
        if state != 'message_accepted':
            state = {'ambiguous': 'delivery_unknown', 'failed': 'failed',
                     'cancelled': 'cancelled'}.get(row['message_state'], state)
        result.append(ArtifactDeliveryView(task.task_id, revision, task.endpoint, task.requester_actor,
            row['artifact_id'], row['sha256'], row['filename'], row['byte_size'], row['draft'], state, row['updated_at'],
            draft_reason=row['draft_reason']))
    return tuple(result)
