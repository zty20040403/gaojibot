"""Explicit local wiki collection evidence; not member or project permission."""
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from ...platform_types import EndpointKey


@dataclass(frozen=True,repr=False)
class WikiCollectionEvidence:
    endpoint: EndpointKey
    endpoint_generation: int
    route_epoch: int
    evidence_inbox_id: str
    verified_at: int
    expires_at: int

    def __post_init__(self):
        if (not isinstance(self.endpoint,EndpointKey) or self.endpoint.account.platform!='qq-official'
                or self.endpoint.kind!='group'
                or any(type(x) is not int or not 0<x<2**63 for x in (self.endpoint_generation,self.route_epoch,self.expires_at))
                or type(self.verified_at) is not int or not 0<=self.verified_at<self.expires_at
                or self.expires_at-self.verified_at>86400
                or not isinstance(self.evidence_inbox_id,str) or not 1<=len(self.evidence_inbox_id)<=128
                or not self.evidence_inbox_id.isascii() or not all(c.isalnum() or c in '_-' for c in self.evidence_inbox_id)):
            raise ValueError('wiki_collection_evidence_invalid')

    def current(self,cursor,endpoint,now):
        if endpoint!=self.endpoint or type(now) is not int or not self.verified_at<=now<self.expires_at:return False
        row=cursor.execute('''SELECT i.received_at FROM qq_official_inbound_events i
            JOIN qq_official_endpoints e ON e.endpoint_key=i.endpoint_key AND e.account_key=i.account_key
            WHERE i.inbox_id=? AND i.account_key=? AND i.endpoint_key=?
              AND i.endpoint_generation=? AND i.route_epoch=? AND e.generation=i.endpoint_generation
              AND e.route_epoch=i.route_epoch AND e.membership_state='present' AND e.binding_state='active'
              AND i.event_type='GROUP_MESSAGE_CREATE' AND i.status='processed' AND i.echo_state='not_echo'
              AND EXISTS (SELECT 1 FROM qq_official_gateway_receipts r WHERE r.account_key=i.account_key
                AND r.inbox_id=i.inbox_id AND r.event_type=i.event_type AND r.disposition='inbox')''',
            (self.evidence_inbox_id,endpoint.account.key,endpoint.key,self.endpoint_generation,self.route_epoch)).fetchone()
        return row is not None and 0<=row['received_at']<=self.verified_at


@dataclass(frozen=True)
class WikiConfiguration:
    enabled: bool = False
    profile: str | None = None
    hour: int = 3
    timezone: str = 'Asia/Shanghai'
    check_seconds: int = 300
    collection_evidence: tuple[WikiCollectionEvidence,...] = ()

    def __post_init__(self):
        if (type(self.enabled) is not bool or type(self.hour) is not int or not 0<=self.hour<=23
                or type(self.check_seconds) is not int or not 30<=self.check_seconds<=3600
                or (self.profile is not None and (not isinstance(self.profile,str) or not 1<=len(self.profile)<=128))
                or (self.enabled and not self.profile) or type(self.collection_evidence) is not tuple
                or len(self.collection_evidence)>128 or any(type(x) is not WikiCollectionEvidence for x in self.collection_evidence)
                or len({x.endpoint.key for x in self.collection_evidence})!=len(self.collection_evidence)):
            raise ValueError('wiki_configuration_invalid')
        ZoneInfo(self.timezone)

    def allowed(self,cursor,endpoint,now):
        return self.enabled and any(item.current(cursor,endpoint,now) for item in self.collection_evidence if item.endpoint==endpoint)
