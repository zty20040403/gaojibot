"""Host-only authenticated role observations; never accept chat/tool role fields.

The gateway supplies DecodedMessage after transport authentication/deduplication.
Observe before link reduction, including ordinary messages that withdraw roles.
No body, raw frame, code or display-name data is retained here.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

from ...platform_types import ActorKey,EndpointKey
from .authority import OwnerAuthority
from .codec import DecodedMessage
from .lease import assert_epoch


class LinkAuthority:
    def __init__(self,pepper: bytes,*,clock=time.time,authority=None):
        if not isinstance(pepper,bytes) or len(pepper)<32:raise ValueError('link_pepper_required')
        self._pepper = pepper; self.clock = clock; self.owners = authority or OwnerAuthority(clock=clock)

    def __repr__(self):return 'LinkAuthority(<protected pepper>)'

    def _hash(self,label,values):
        return hmac.new(self._pepper,json.dumps([label,*values],separators=(',',':')).encode(),hashlib.sha256).hexdigest()

    def receipt(self,message):
        return self._hash('qq-link-receipt:v1',[message.endpoint.key,message.actor.key,message.native_message_id,
            message.event_id,message.platform_occurred_at,message.received_at])

    def _endpoint(self,cursor,token,endpoint):
        assert_epoch(cursor,token)
        if endpoint.account.key!=token.account_key:raise PermissionError('link_account_mismatch')
        row = cursor.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=? AND account_key=?',
            (endpoint.key,token.account_key)).fetchone()
        return row if row and row['binding_state']=='active' and row['membership_state']=='present' else None

    @staticmethod
    def _message(message):
        return (isinstance(message,DecodedMessage) and message.endpoint.account.platform=='qq-official'
            and message.actor.identity_kind in {'c2c_user','group_member'} and message.actor.matches_endpoint(message.endpoint))

    def _fresh(self,message):
        return (self._message(message) and message.timestamp_source=='platform'
            and type(message.platform_occurred_at) is int and type(message.received_at) is int
            and 0<=message.platform_occurred_at<=message.received_at<=int(self.clock())
            and int(self.clock())-message.platform_occurred_at<600)

    def observe(self,cursor,token,message):
        if not self._message(message):raise ValueError('link_authenticated_message_required')
        endpoint = self._endpoint(cursor,token,message.endpoint)
        if endpoint is None or message.endpoint.kind!='group':return
        fresh = self._fresh(message)
        role = message.platform_role if fresh and message.platform_role in {'owner','admin'} else ''
        stamp = message.platform_occurred_at if message.timestamp_source=='platform' else message.received_at
        if type(stamp) is not int or stamp<0 or stamp>int(self.clock()):raise ValueError('link_observation_time_invalid')
        old = cursor.execute('SELECT * FROM qq_official_link_roles WHERE endpoint_key=? AND actor_key=? FOR UPDATE',
            (message.endpoint.key,message.actor.key)).fetchone()
        same_route = old and (old['endpoint_generation'],old['route_epoch'])==(endpoint['generation'],endpoint['route_epoch'])
        if same_route and stamp<old['observed_at']:return
        # Conflicting same-second roles withdraw authority; arrival order cannot grant it.
        if same_route and stamp==old['observed_at'] and role!=old['role']:role = ''
        revision = 1 if not old else old['revision']+int(not same_route or role!=old['role'])
        cursor.execute('''INSERT INTO qq_official_link_roles
            (account_key,endpoint_key,actor_key,endpoint_generation,route_epoch,role,revision,observed_at,receipt_hash)
            VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(endpoint_key,actor_key) DO UPDATE SET
            endpoint_generation=excluded.endpoint_generation,route_epoch=excluded.route_epoch,role=excluded.role,
            revision=excluded.revision,observed_at=excluded.observed_at,receipt_hash=excluded.receipt_hash''',
            (token.account_key,message.endpoint.key,message.actor.key,endpoint['generation'],endpoint['route_epoch'],
             role,revision,stamp,self.receipt(message)))

    def _credentials(self,cursor,endpoint,actor):
        owner = self.owners.owner_for_actor(cursor,endpoint,actor)
        if owner:yield ['owner',owner.grant_id,owner.revision]
        if endpoint.kind=='group':
            role = cursor.execute('SELECT * FROM qq_official_link_roles WHERE endpoint_key=? AND actor_key=?',
                (endpoint.key,actor.key)).fetchone()
            current = cursor.execute('SELECT generation,route_epoch FROM qq_official_endpoints WHERE endpoint_key=?',(endpoint.key,)).fetchone()
            if (role and role['role'] in {'owner','admin'}
                and (role['endpoint_generation'],role['route_epoch'])==(current['generation'],current['route_epoch'])):
                yield ['role',role['role'],role['revision']]

    def _proof_hash(self,proof,credential):
        return self._hash('qq-link-authority:v1',[proof[key] for key in ('account_epoch','endpoint_key','actor_key','scope_key',
            'endpoint_generation','route_epoch','receipt_hash','received_at','expires_at')]+[credential])

    def proof(self,cursor,token,message,*,expires_at=None):
        if not self._message(message):raise ValueError('link_authenticated_message_required')
        endpoint = self._endpoint(cursor,token,message.endpoint)
        if endpoint is None or not self._fresh(message) or message.evidence.message_type not in {None,0}:return None
        deadline = message.platform_occurred_at+600 if expires_at is None else expires_at
        if type(deadline) is not int or not message.received_at<deadline<=message.platform_occurred_at+600:return None
        proof = dict(account_epoch=token.epoch,endpoint_key=message.endpoint.key,actor_key=message.actor.key,
            scope_key=endpoint['canonical_scope_key'],endpoint_generation=endpoint['generation'],route_epoch=endpoint['route_epoch'],
            receipt_hash=self.receipt(message),received_at=message.received_at,expires_at=deadline)
        for credential in self._credentials(cursor,message.endpoint,message.actor):
            if credential[0]=='role':
                observed = cursor.execute('SELECT role,receipt_hash FROM qq_official_link_roles WHERE endpoint_key=? AND actor_key=?',
                    (message.endpoint.key,message.actor.key)).fetchone()
                if observed['role']!=message.platform_role or observed['receipt_hash']!=proof['receipt_hash']:continue
            proof['authority_hash'] = self._proof_hash(proof,credential)
            return proof if self.current(cursor,token,proof) else None
        return None

    def current(self,cursor,token,proof):
        endpoint,actor = EndpointKey.parse(proof['endpoint_key']),ActorKey.parse(proof['actor_key'])
        row = self._endpoint(cursor,token,endpoint)
        if (not row or not actor.matches_endpoint(endpoint) or actor.identity_kind=='bot' or proof['account_epoch']!=token.epoch
            or not proof['received_at']<=int(self.clock())<proof['expires_at']
            or not 0<proof['expires_at']-proof['received_at']<=600
            or (row['generation'],row['route_epoch'],row['canonical_scope_key'])
                !=(proof['endpoint_generation'],proof['route_epoch'],proof['scope_key'])):return False
        allowed = any(hmac.compare_digest(proof['authority_hash'],self._proof_hash(proof,credential))
            for credential in self._credentials(cursor,endpoint,actor))
        return allowed and int(self.clock())<proof['expires_at']
