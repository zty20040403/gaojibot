"""Bounded actual-endpoint ledger speakers, not a group membership list."""
from dataclasses import dataclass, field
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey


@dataclass(frozen=True)
class RecentSpeaker:
    actor: ActorKey = field(repr=False)
    display: str = field(repr=False)
    last_seen_at: int
    message_count: int
    principal_id: int | None = None


@dataclass(frozen=True)
class RecentSpeakers:
    speakers: tuple[RecentSpeaker,...]
    since: int
    until: int
    scanned_message_limit: int = 500
    is_membership_list: bool = False


def recent_speakers(cursor, *, endpoint, scope_key, now, current, limit=20, max_age_seconds=86400):
    if (not isinstance(endpoint,EndpointKey) or endpoint.account.platform!='qq-official'
            or type(now) is not int or now<0 or type(limit) is not int or not 1<=limit<=50
            or type(max_age_seconds) is not int or not 1<=max_age_seconds<=604800
            or not callable(current) or current(cursor,endpoint) is not True):
        raise PermissionError('recent_speakers_unavailable')
    rows=cursor.execute('''SELECT m.sender_actor_key,m.sender_display,m.sender_principal_id,m.occurred_at
        FROM messages m JOIN conversations c USING(conversation_id)
        JOIN conversation_visibility v USING(conversation_id)
        WHERE c.scope_key=? AND m.source_endpoint_key=? AND m.direction='inbound'
          AND m.canonical_message_id>=v.min_canonical_message_id AND m.occurred_at BETWEEN ? AND ?
        ORDER BY m.occurred_at DESC,m.canonical_message_id DESC LIMIT 500''',
        (scope_key,endpoint.key,max(0,now-max_age_seconds),now)).fetchall()
    found={}
    for row in rows:
        try:actor=ActorKey.parse(row['sender_actor_key'])
        except (ValueError,TypeError):continue
        if not actor.matches_endpoint(endpoint) or actor.identity_kind=='bot':continue
        if actor.key in found:
            old=found[actor.key]
            found[actor.key]=RecentSpeaker(actor,old.display,old.last_seen_at,old.message_count+1,old.principal_id)
        elif len(found)<limit:
            found[actor.key]=RecentSpeaker(actor,row['sender_display'],row['occurred_at'],1,row.get('sender_principal_id'))
    return RecentSpeakers(tuple(found.values()),max(0,now-max_age_seconds),now)
