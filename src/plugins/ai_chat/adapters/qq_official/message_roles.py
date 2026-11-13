"""Recover only the authenticated top-level sender role, never forwarded text."""
from src.qq_official.events import parse_dispatch
from .codec import DecodedMessage, decode_message


def source_role(cursor, endpoint, actor, canonical, *, now):
    row = cursor.execute('''SELECT i.payload_json,i.received_at,i.native_message_id FROM qq_official_inbound_events i
        JOIN qq_official_endpoints e ON e.endpoint_key=i.endpoint_key
        JOIN messages m ON m.canonical_message_id=i.canonical_message_id
        WHERE i.account_key=? AND i.endpoint_key=? AND i.canonical_message_id=? AND m.sender_actor_key=?
          AND i.status='processed' AND i.payload_expires_at>? AND i.payload_json IS NOT NULL
          AND i.endpoint_generation=e.generation AND i.route_epoch=e.route_epoch''',
        (endpoint.account.key,endpoint.key,canonical,actor.key,now)).fetchone()
    if row is None:
        return ''
    try:
        decoded = decode_message(parse_dispatch(row['payload_json']),endpoint.account,row['received_at'])
    except ValueError:
        return ''
    if (not isinstance(decoded,DecodedMessage) or decoded.endpoint!=endpoint or decoded.actor!=actor
            or decoded.native_message_id!=row['native_message_id']):
        return ''
    return decoded.platform_role
