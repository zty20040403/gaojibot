"""Bounded protected diagnostics over existing durable records; never authority."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

from .official_metrics import SAFE_STATES, finite

SECTIONS=frozenset(('executions','deliveries','bindings'))
TASK_STATES=frozenset(('received','queued','planning','running','verifying','revising','completed','partial','failed','cancelled','cancelling','interrupted'))
REASONS=frozenset(('waiting_reply_window','recipient_evidence_unavailable','receipt_needs_finalization',
    'quota_exhausted','quota_reset_unverified','server_cooldown','membership_unknown','endpoint_unbound',
    'endpoint_revoked','delivery_expired','turn_deadline_expired','execution_owner_lost','cancelled',
    'previous_part_failed','previous_part_cancelled','restart_outcome_unknown','ephemeral_payload_lost',
    'ephemeral_source_expired','unsolicited_window_unavailable','scheduled','progress_suppressed',
    'mode_changed','receipt_conflict','bot_identity_unavailable','accepted_ledger_pending','source_denied',
    'session_lost','sequence_gap','invalid_session','gateway_gap','auth_failed','rate_limited'))
ATTEMPT_STATES=frozenset(('reserved','dispatched','accepted','committed','confirmed_unaccepted','cancelled_unsent','ambiguous'))
MAX_ROWS=1000


class DetailsUnavailable(ValueError):
    def __init__(self):super().__init__('official_diagnostics_unavailable')


def _reason(value):
    return finite(value,REASONS,'other') if value else None


def _integer(value):
    return value if type(value) is int and 0<=value<2**63 else None


class OfficialDetails:
    def __init__(self,runtime,*,clock):
        self.runtime,self.clock=runtime,clock
        self.account=runtime.loaded.account.key
        self._pepper=runtime.loaded.binding_pepper
        if not isinstance(self._pepper,bytes) or len(self._pepper)<32:raise DetailsUnavailable()

    def _ref(self,kind,value):
        if value is None:return None
        return kind+'_'+hmac.new(self._pepper,b'qq-diagnostics-ref-v1\0'+self.account.encode()+b'\0'+kind.encode()+b'\0'+str(value).encode(),hashlib.sha256).hexdigest()[:24]

    def _cursor(self,section,offset,as_of):
        # This contains only enumeration/offset/time, no identity or payload.
        raw=json.dumps([section,offset,as_of],separators=(',',':')).encode()
        signature=hmac.new(self._pepper,b'qq-diagnostics-page-v1\0'+self.account.encode()+b'\0'+raw,hashlib.sha256).digest()
        return base64.urlsafe_b64encode(raw+signature).decode().rstrip('=')

    def _page(self,section,limit,cursor,now):
        if section not in SECTIONS or type(limit) is not int or not 1<=limit<=50:raise DetailsUnavailable()
        if cursor is None:return 0,now
        try:
            if not isinstance(cursor,str) or len(cursor)>256:raise ValueError
            raw=base64.b64decode(cursor+'='*(-len(cursor)%4),altchars=b'-_',validate=True)
            body,signature=raw[:-32],raw[-32:]
            expected=hmac.new(self._pepper,b'qq-diagnostics-page-v1\0'+self.account.encode()+b'\0'+body,hashlib.sha256).digest()
            if not hmac.compare_digest(signature,expected):raise ValueError
            selected,offset,as_of=json.loads(body)
            if selected!=section or type(offset) is not int or not 0<offset<MAX_ROWS or type(as_of) is not int or not now-300<=as_of<=now:raise ValueError
            return offset,as_of
        except (ValueError,TypeError,UnicodeError):raise DetailsUnavailable() from None

    def read(self,*,section,limit=20,cursor=None):
        now=_integer(int(self.clock()))
        if now is None:raise DetailsUnavailable()
        offset,as_of=self._page(section,limit,cursor,now)
        count=min(limit,MAX_ROWS-offset)
        with self.runtime.store.transaction() as c:
            c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            c.execute("SET LOCAL statement_timeout = '1000ms'")
            checkpoint=c.execute('''SELECT session_id,durable_seq,fencing_epoch,pause_reason,updated_at
                FROM qq_official_accounts WHERE account_key=?''',(self.account,)).fetchone()
            if checkpoint is None:raise DetailsUnavailable()
            health=getattr(getattr(self.runtime,'_gateway',None),'health',{})
            health=health if type(health) is dict else {}
            checkpoint={'session_present':checkpoint['session_id'] is not None,
                'durable_seq':_integer(checkpoint['durable_seq']),'fencing_epoch':_integer(checkpoint['fencing_epoch']),
                'gap':health.get('message_gap') is True,'gap_reason':_reason(checkpoint['pause_reason'] or health.get('reason')),
                'updated_at':_integer(checkpoint['updated_at'])}
            items=getattr(self,'_'+section)(c,as_of,count+1,offset)
        more=len(items)>count
        return {'section':section,'observed_at':now,'as_of':as_of,'checkpoint':checkpoint,'items':items[:count],
            'next_cursor':self._cursor(section,offset+count,as_of) if more and offset+count<MAX_ROWS else None,
            'truncated':more and offset+count>=MAX_ROWS,'bounded_rows':MAX_ROWS}

    def _executions(self,c,as_of,count,offset):
        rows=c.execute('''SELECT * FROM (
            SELECT execution_id AS identity,endpoint_key,requester_actor_key,state,execution_kind,result_status,
                reason_code,deadline_at,deadline_at AS result_deadline_at,created_at,updated_at,plan_id,journal_turn_id
            FROM qq_official_turn_executions WHERE account_key=? AND created_at<=?
            UNION ALL
            SELECT job_id::text AS identity,endpoint_key,actor_key AS requester_actor_key,state,'agent' AS execution_kind,
                state AS result_status,'' AS reason_code,deadline_at,NULL::bigint AS result_deadline_at,
                created_at,COALESCE(finished_at,started_at,created_at) AS updated_at,NULL::text AS plan_id,NULL::bigint AS journal_turn_id
            FROM background_jobs WHERE account_key=? AND created_at<=?
            ) q ORDER BY created_at DESC,execution_kind DESC,identity DESC LIMIT ? OFFSET ?''',
            (self.account,as_of,self.account,as_of,count,offset)).fetchall()
        return [{'ref':self._ref('task' if r['execution_kind']=='agent' else 'execution',r['identity']),'endpoint_ref':self._ref('endpoint',r['endpoint_key']),
            'actor_ref':self._ref('actor',r['requester_actor_key']),'state':finite(r['state'],SAFE_STATES['execution']|TASK_STATES,'other'),
            'kind':finite(r['execution_kind'],('chat','command','agent'),'other'),'result_status':finite(r['result_status'],('succeeded','silence','crashed','aborted','denied','cancelled','completed','partial','failed'),None),
            'reason':_reason(r['reason_code']),'deadline_at':_integer(r['deadline_at']),'result_deadline_at':_integer(r['result_deadline_at']),'created_at':_integer(r['created_at']),
            'updated_at':_integer(r['updated_at']),'plan_ref':self._ref('plan',r['plan_id']),
            'journal_present':r['journal_turn_id'] is not None} for r in rows]

    def _deliveries(self,c,as_of,count,offset):
        rows=c.execute('''SELECT d.delivery_id,d.plan_id,d.endpoint_key,d.requester_actor_key,d.part_index,p.part_count,
            d.status,d.wait_reason,d.expires_at,d.next_attempt_at,d.attempts,d.created_at,d.updated_at,
            a.state AS attempt_state,a.acceptance,a.send_by_at
            FROM deliveries d JOIN delivery_plans p ON p.plan_id=d.plan_id AND p.account_key=d.account_key
            LEFT JOIN delivery_attempts a ON a.attempt_id=d.current_attempt_id AND a.account_key=d.account_key
            WHERE d.account_key=? AND d.created_at<=? ORDER BY d.created_at DESC,d.delivery_id DESC LIMIT ? OFFSET ?''',
            (self.account,as_of,count,offset)).fetchall()
        items=[]
        for r in rows:
            receipts=c.execute('''SELECT r.receipt_id,r.kind,r.native_message_id,r.ref_idx,r.observed_at,r.applied_at
                FROM delivery_receipts r JOIN delivery_attempts a ON a.attempt_id=r.attempt_id AND a.account_key=r.account_key
                WHERE a.delivery_id=? AND r.account_key=? ORDER BY r.observed_at DESC,r.receipt_id DESC LIMIT 9''',
                (r['delivery_id'],self.account)).fetchall()
            items.append({'ref':self._ref('delivery',r['delivery_id']),'plan_ref':self._ref('plan',r['plan_id']),
                'endpoint_ref':self._ref('endpoint',r['endpoint_key']),'actor_ref':self._ref('actor',r['requester_actor_key']),
                'part_index':_integer(r['part_index']),'part_count':_integer(r['part_count']),
                'state':finite(r['status'],SAFE_STATES['delivery'],'other'),'wait_reason':_reason(r['wait_reason']),
                'expires_at':_integer(r['expires_at']),'next_attempt_at':_integer(r['next_attempt_at']),
                'attempt_count':_integer(r['attempts']),'created_at':_integer(r['created_at']),'updated_at':_integer(r['updated_at']),
                'attempt':None if r['attempt_state'] is None else {'state':finite(r['attempt_state'],ATTEMPT_STATES,'other'),
                    'acceptance':finite(r['acceptance'],('unknown','confirmed_no','confirmed_yes'),'unknown'),'send_by_at':_integer(r['send_by_at'])},
                'receipts':[{'ref':self._ref('receipt',x['receipt_id']),'kind':finite(x['kind'],('response','verified_echo'),'other'),
                    'native_ref':self._ref('message',x['native_message_id']),'ref_index_present':x['ref_idx'] is not None,
                    'observed_at':_integer(x['observed_at']),'applied_at':_integer(x['applied_at'])} for x in receipts[:8]],
                'receipts_truncated':len(receipts)>8})
        return items

    def _bindings(self,c,as_of,count,offset):
        rows=c.execute('''SELECT e.endpoint_key,e.kind,e.binding_state,e.membership_state,e.generation,e.route_epoch,
            e.capability_revision,e.updated_at,b.binding_id,b.admin_account_id,b.c2c_actor_key,b.state AS admin_state,
            b.revision,b.updated_at AS admin_updated_at,
            (o.state='active' AND o.revision=b.owner_revision) AS owner_current,
            (b.state='active' AND o.state='active' AND o.revision=b.owner_revision AND a.enabled=1
                AND a.role='admin' AND a.version=b.admin_account_version AND e.membership_state='present'
                AND e.binding_state<>'revoked') AS current
            FROM qq_official_endpoints e LEFT JOIN qq_official_admin_bindings b ON b.c2c_endpoint_key=e.endpoint_key AND b.account_key=e.account_key
            LEFT JOIN admin_accounts a ON a.account_id=b.admin_account_id
            LEFT JOIN qq_official_owner_grants o ON o.grant_id=b.owner_grant_id AND o.account_key=b.account_key
            WHERE e.account_key=? AND e.created_at<=? ORDER BY e.created_at DESC,e.endpoint_key DESC LIMIT ? OFFSET ?''',
            (self.account,as_of,count,offset)).fetchall()
        return [{'ref':self._ref('endpoint',r['endpoint_key']),'kind':finite(r['kind'],('group','private'),'other'),
            'state':finite(r['binding_state'],('active','unbound','revoked'),'other'),
            'membership':finite(r['membership_state'],('unknown','present','absent'),'unknown'),
            'generation':_integer(r['generation']),'route_epoch':_integer(r['route_epoch']),
            'capability_revision':_integer(r['capability_revision']),'updated_at':_integer(r['updated_at']),
            'admin_binding':None if r['binding_id'] is None else {'ref':self._ref('binding',r['binding_id']),
                'admin_ref':self._ref('admin',r['admin_account_id']),'actor_ref':self._ref('actor',r['c2c_actor_key']),
                'state':finite(r['admin_state'],('active','revoked'),'other'),'revision':_integer(r['revision']),
                'current':r['current'] is True,'owner_current':r['owner_current'] is True,'updated_at':_integer(r['admin_updated_at'])}} for r in rows]
