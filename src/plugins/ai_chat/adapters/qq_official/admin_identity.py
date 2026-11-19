"""Console-session ↔ authenticated C2C possession binding, never first contact owner."""
from __future__ import annotations
from dataclasses import dataclass,field
import hashlib
import hmac
import json
import re

from src.plugins.ai_chat.platform_types import AccountKey,ActorKey,EndpointKey
from src.plugins.ai_chat.adapters.qq_official.authority import ConsoleAuthorization,audit
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch


class IdentityBindingDenied(PermissionError):
    def __init__(self):super().__init__('official_identity_binding_unavailable')


@dataclass(frozen=True)
class IdentityInput:
    kind: str
    challenge_id: str|None=field(default=None,repr=False)
    secret: str|None=field(default=None,repr=False)


def prepare_identity_input(dispatch):
    text=dispatch.data.get('content','')
    if not isinstance(text,str) or '/admin-bind' not in text.lower():return IdentityInput('ordinary')
    match=re.fullmatch(r'/admin-bind ([0-9a-f]{32})\.([0-9a-f]{32})',text.strip())
    if match is None:return IdentityInput('quarantine')
    return IdentityInput('control',match[1],match[2])


def _code_digest(pepper,account_key,challenge_id,secret):
    return hmac.new(pepper,b'qq-admin-identity-v1\0'+account_key.encode()+b'\0'+
        challenge_id.encode()+b'\0'+secret.encode(),hashlib.sha256).hexdigest()


def _source_commitment(pepper,*,inbox_id,endpoint,actor,native_id,challenge_id,code_digest):
    data=json.dumps([inbox_id,endpoint.key,actor.key,native_id,challenge_id,code_digest],separators=(',',':'))
    return hmac.new(pepper,b'qq-admin-identity-source-v1\0'+data.encode(),hashlib.sha256).hexdigest()


def command_commitment(pepper,*,inbox_id,endpoint,actor,native_id,command):
    return _source_commitment(pepper,inbox_id=inbox_id,endpoint=endpoint,actor=actor,native_id=native_id,
        challenge_id=command.challenge_id,
        code_digest=_code_digest(pepper,endpoint.account.key,command.challenge_id,command.secret))


class AdminIdentityBinding:
    def __init__(self,runtime,token):
        if token.account_key!=runtime.loaded.account.key:raise IdentityBindingDenied()
        self.runtime,self.token=runtime,token
        self.account=AccountKey.parse(token.account_key)
        self.clock=runtime.store.clock
        self._pepper=runtime.loaded.binding_pepper
        if not isinstance(self._pepper,bytes) or len(self._pepper)<32:raise IdentityBindingDenied()
        self.owners=runtime.store.authority

    def _now(self):
        value=self.clock()
        if type(value) not in (int,float) or not 0<=value<2**63-180:raise IdentityBindingDenied()
        return int(value)

    def _fence(self,c):
        if self.runtime._token is not self.token or self.runtime._stopping:raise IdentityBindingDenied()
        assert_epoch(c,self.token)

    def _digest(self,challenge_id,secret):
        return _code_digest(self._pepper,self.account.key,challenge_id,secret)

    def observe(self,c,*,inbox_id,command):
        """Called only after the actual TxA receipt INSERT, with its same cursor."""
        self._fence(c);now=self._now()
        if type(command) is not IdentityInput or command.kind!='control':return 'denied'
        source=c.execute('''SELECT i.*,e.kind,e.binding_state,e.membership_state,e.generation,
            e.route_epoch AS current_route FROM qq_official_inbound_events i
            JOIN qq_official_endpoints e ON e.endpoint_key=i.endpoint_key AND e.account_key=i.account_key
            WHERE i.inbox_id=? AND i.account_key=? AND i.status='processed' AND i.reason_code='admin_identity_control'
              AND i.echo_state='not_echo' AND i.payload_json IS NULL AND i.ingest_job_id IS NULL
              AND EXISTS(SELECT 1 FROM qq_official_gateway_receipts r WHERE r.inbox_id=i.inbox_id
                AND r.account_key=i.account_key AND r.disposition='control')''',(inbox_id,self.account.key)).fetchone()
        if source is None:return 'denied'
        try:
            endpoint=EndpointKey.parse(source['endpoint_key']);actor=ActorKey.parse(source['identity_control_actor_key'])
        except (ValueError,TypeError):return 'denied'
        if (endpoint.kind!='private' or actor.identity_kind!='c2c_user' or not actor.matches_endpoint(endpoint)
            or source['binding_state']=='revoked' or source['membership_state']!='present'
            or source['generation']!=source['endpoint_generation'] or source['current_route']!=source['route_epoch']
            or type(source['identity_control_occurred_at']) is not int
            or not now-180<=source['identity_control_occurred_at']<=now
            or not now-180<=source['received_at']<=now):return 'denied'
        expected=command_commitment(self._pepper,inbox_id=inbox_id,endpoint=endpoint,actor=actor,native_id=source['native_message_id'],command=command)
        if not isinstance(source['identity_control_commitment'],str) or not hmac.compare_digest(source['identity_control_commitment'],expected):return 'denied'
        bucket=hmac.new(self._pepper,b'qq-admin-identity-rate-v1\0'+actor.key.encode(),hashlib.sha256).hexdigest()
        c.execute('''INSERT INTO qq_official_binding_rate_limits(bucket,window_start,hits) VALUES (?,?,0)
            ON CONFLICT DO NOTHING''',(bucket,now))
        rate=c.execute('SELECT * FROM qq_official_binding_rate_limits WHERE bucket=? FOR UPDATE',(bucket,)).fetchone()
        start,hits=(now,0) if now-rate['window_start']>=60 else (rate['window_start'],rate['hits'])
        c.execute('UPDATE qq_official_binding_rate_limits SET window_start=?,hits=? WHERE bucket=?',(start,min(hits+1,1000),bucket))
        if hits>=10:return 'limited'
        owner=self.owners.c2c_grant(c,actor,lock=True)
        if owner is None:return 'denied'
        row=c.execute('''SELECT * FROM qq_official_admin_identity_challenges
            WHERE challenge_id=? AND account_key=? FOR UPDATE''',(command.challenge_id,self.account.key)).fetchone()
        if (row is None or row['state']!='pending' or not row['created_at']<=source['received_at']<row['expires_at']
            or now>=row['expires_at'] or not hmac.compare_digest(row['code_digest'],self._digest(command.challenge_id,command.secret))):return 'denied'
        authorization=ConsoleAuthorization(row['admin_account_id'],row['admin_version'],row['session_hash'])
        if not self.owners.console_current(c,authorization):return 'denied'
        c.execute('''UPDATE qq_official_admin_identity_challenges SET state='observed',source_inbox_id=?,
            endpoint_key=?,actor_key=?,endpoint_generation=?,route_epoch=?,owner_grant_id=?,owner_revision=?,observed_at=?
            WHERE challenge_id=? AND state='pending' ''',(inbox_id,endpoint.key,actor.key,source['endpoint_generation'],
            source['route_epoch'],owner.grant_id,owner.revision,now,row['challenge_id']))
        audit(c,account=self.account,action='admin_identity_observe',actor=actor.key,target=row['challenge_id'],outcome='observed',now=now)
        return 'observed'
