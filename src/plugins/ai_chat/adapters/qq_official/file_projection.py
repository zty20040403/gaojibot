"""Read attachment delivery facts without granting upload or send authority."""
from __future__ import annotations

from src.plugins.ai_chat.agent.artifact_delivery import ArtifactDeliveryView
from .send_types import OutboxInvariantError


def apply_file_receipt(cursor, *, delivery, attempt, now):
    cursor.execute("""UPDATE qq_official_file_deliveries
        SET state='message_accepted',reason_code='',updated_at=? WHERE active_plan_id=?""",
        (now, delivery['plan_id']))


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
