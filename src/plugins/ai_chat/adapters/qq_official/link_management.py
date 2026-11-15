"""Authenticated physical-side management; persistent consent is not an OTP."""
from dataclasses import dataclass
import hashlib
import hmac
import re

from ...platform_types import ActorKey,EndpointKey
from .link_commands import LinkCommand
from .linking import LinkDecision,LIVE


@dataclass(frozen=True)
class LinkView:
    link_id: str
    direction: str
    peer: str
    state: str
    generation: int


class LinkManagement:
    def __init__(self,reducer):self.reducer,self.authority = reducer,reducer.authority

    def current(self,cursor,token,edge):
        """Only already activated, receipt-backed consent survives its code TTL.

        Frozen approver credentials must still match; a regrant, new route or
        membership loss cannot restore them. No actor authority crosses the edge.
        Account leases still fence each read, but an established edge may persist
        across epochs; its consumed OTP and pending questions may not.
        """
        row = self.reducer.get(cursor,token,edge['challenge_id'])
        if (edge['state']!='active' or row is None or row['state']!='active' or row['delivery_state']!='committed'
            or not row['delivery_receipt_hash'] or edge['account_key']!=token.account_key):return False
        names = ('source_endpoint_key','target_endpoint_key','source_scope_key','target_scope_key','source_generation',
            'target_generation','source_route_epoch','target_route_epoch')
        if any(edge[name]!=row[name] for name in names):return False
        for side in ('source','target'):
            proof = self.reducer.proof(row,side)
            endpoint,actor = EndpointKey.parse(proof['endpoint_key']),ActorKey.parse(proof['actor_key'])
            present = self.authority._endpoint(cursor,token,endpoint)
            if (not present or not actor.matches_endpoint(endpoint) or actor.identity_kind not in {'c2c_user','group_member'}
                or (present['generation'],present['route_epoch'],present['canonical_scope_key'])
                    !=(proof['endpoint_generation'],proof['route_epoch'],proof['scope_key'])):return False
            if not any(hmac.compare_digest(proof['authority_hash'],self.authority._proof_hash(proof,credential))
                for credential in self.authority._credentials(cursor,endpoint,actor)):return False
        return True

    def checked_at(self,cursor,token,proof):
        if not self.authority.current(cursor,token,proof):return None
        now = self.reducer.now()
        return now if proof['received_at']<=now<proof['expires_at'] else None

    @staticmethod
    def revoke(cursor,edge,now):
        if edge['state']=='active':
            cursor.execute("UPDATE qq_official_conversation_links SET state='revoked',generation=generation+1,revoked_at=? WHERE link_id=?",
                (now,edge['link_id']))
            return dict(edge)|dict(state='revoked',generation=edge['generation']+1)
        return edge

    @staticmethod
    def view(edge,endpoint):
        outgoing = edge['source_endpoint_key']==endpoint
        peer = EndpointKey.parse(edge['target_endpoint_key'] if outgoing else edge['source_endpoint_key'])
        label = ('群会话 ' if peer.kind=='group' else '私聊会话 ')+hashlib.sha256(peer.key.encode()).hexdigest()[:12]
        return LinkView(edge['link_id'],'outgoing' if outgoing else 'incoming',label,edge['state'],edge['generation'])

    def apply(self,cursor,token,message,command):
        if not isinstance(command,LinkCommand) or not command.valid or command.code:return LinkDecision('unsupported')
        expected = {'status':r'LC-[0-9a-f]{32}','cancel':r'LC-[0-9a-f]{32}','revoke':r'[0-9a-f]{32}'}
        if not (command.action=='list' and not command.reference or command.action in expected
                and isinstance(command.reference,str) and re.fullmatch(expected[command.action],command.reference)):
            return LinkDecision('unsupported')
        proof = self.authority.proof(cursor,token,message)
        if proof is None:return LinkDecision('denied')
        if not self.reducer.rate(cursor,token,'account',120,60):return LinkDecision('limited')
        endpoint = proof['endpoint_key']
        if command.action=='list':
            rows = cursor.execute('''SELECT * FROM qq_official_conversation_links WHERE account_key=?
                AND (source_endpoint_key=? OR target_endpoint_key=?) ORDER BY created_at DESC,link_id DESC LIMIT 33 FOR UPDATE''',
                (token.account_key,endpoint,endpoint)).fetchall()
            stale = [edge for edge in rows[:32] if edge['state']=='active' and not self.current(cursor,token,edge)]
            now = self.checked_at(cursor,token,proof)
            if now is None:return LinkDecision('denied')
            invalid = {edge['link_id']:self.revoke(cursor,edge,now) for edge in stale}
            return LinkDecision('listed',entries=tuple(self.view(invalid.get(edge['link_id'],edge),endpoint) for edge in rows[:32]),more=len(rows)>32)
        if command.action=='revoke':
            edge = cursor.execute('''SELECT * FROM qq_official_conversation_links WHERE link_id=? AND account_key=?
                AND (source_endpoint_key=? OR target_endpoint_key=?) FOR UPDATE''',(command.reference,token.account_key,endpoint,endpoint)).fetchone()
            now = self.checked_at(cursor,token,proof)
            if now is None:return LinkDecision('denied')
            if edge is None:return LinkDecision('invalid')
            self.revoke(cursor,edge,now)
            return LinkDecision('revoked',link_id=edge['link_id'])
        row = self.reducer.get(cursor,token,command.reference)
        if row is None or endpoint not in (row['source_endpoint_key'],row['target_endpoint_key']):return LinkDecision('invalid')
        edge = cursor.execute('SELECT * FROM qq_official_conversation_links WHERE challenge_id=?',(row['challenge_id'],)).fetchone()
        stale_edge = edge and edge['state']=='active' and not self.current(cursor,token,edge)
        stale_code = row['state'] in LIVE and not self.reducer.current(cursor,token,row)
        now = self.checked_at(cursor,token,proof)
        if now is None:return LinkDecision('denied')
        if stale_edge:self.revoke(cursor,edge,now)
        if stale_code or command.action=='cancel' and row['state'] in LIVE:
            return self.reducer.close(cursor,row)
        if command.action=='cancel' and row['state']=='active':
            return LinkDecision('use_unlink',row['challenge_id'],edge['link_id']) if edge else LinkDecision('invalid')
        return self.reducer.result(cursor,row)
