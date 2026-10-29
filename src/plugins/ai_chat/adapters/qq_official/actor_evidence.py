"""Recent observed presence, never a membership or task-authority grant."""
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey


def recent_actor_evidence(cursor, *, endpoint, actor, now, max_age_seconds=300):
    """Caller separately owns its current account, route and source authority."""
    if (not isinstance(endpoint,EndpointKey) or endpoint.account.platform!='qq-official'
            or not isinstance(actor,ActorKey) or actor.identity_kind=='bot' or not actor.matches_endpoint(endpoint)
            or type(now) is not int or not 0<=now<2**63
            or type(max_age_seconds) is not int or not 1<=max_age_seconds<=300):
        return False
    return cursor.execute('''SELECT 1 FROM qq_official_inbound_events i
        JOIN qq_official_endpoints e ON e.endpoint_key=i.endpoint_key AND e.account_key=i.account_key
        JOIN messages m ON m.canonical_message_id=i.canonical_message_id
        WHERE i.account_key=? AND i.endpoint_key=? AND m.sender_actor_key=?
          AND i.status='processed' AND i.echo_state='not_echo'
          AND e.membership_state='present' AND e.binding_state='active'
          AND i.endpoint_generation=e.generation AND i.route_epoch=e.route_epoch
          AND m.source_endpoint_key=i.endpoint_key AND m.native_message_id=i.native_message_id
          AND m.direction='inbound' AND i.received_at BETWEEN ? AND ? AND m.occurred_at BETWEEN ? AND ?
          AND EXISTS (SELECT 1 FROM qq_official_gateway_receipts r
            WHERE r.account_key=i.account_key AND r.inbox_id=i.inbox_id AND r.disposition IN ('inbox','duplicate'))
        LIMIT 1''', (endpoint.account.key,endpoint.key,actor.key,now-max_age_seconds,now,
                    now-max_age_seconds,now)).fetchone() is not None
