"""Official transport for the existing SecurityStore approval state machine.

Internal host ports only. These dataclasses do not authenticate a user, execution
or model request. The source port must resolve real persisted handles and route
proofs; its default denies. No operation queue or network client lives here.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass, field

from src.bot_security.store import SecurityError, canonical, digest, public_account
from ...conversation_scope import ConversationScope
from ...message_ir import MessageBody, TextNode
from ...platform_types import ActorKey, EndpointKey
from .admin_bindings import AdminBindingStore
from .project_authority import _positive, _text
from .lease import assert_epoch
from .renderer import render_plan
from .plans import content_payload


IDENTITY_FIELDS = ('official_account_key','official_endpoint_key','official_actor_key',
    'official_admin_binding_id','official_binding_revision','official_source_endpoint_key',
    'official_source_actor_key','official_source_scope_key','official_execution_id','official_task_id',
    'official_source_revision','official_route_epoch','official_endpoint_route_epoch',
    'official_source_deadline_at','official_policy_json','official_policy_digest')
SOURCE_FIELDS = IDENTITY_FIELDS[5:]
_OPS = frozenset({'fleet.job.create','fleet.runbook.start','fleet.operation.approve','fleet.artifact.upload',
                 'fleet.operation.prepare','fleet.operation.read'})


def denied():
    raise SecurityError('official_approval_not_authorized')


@dataclass(frozen=True)
class Recipient:
    endpoint: EndpointKey = field(repr=False)
    actor: ActorKey = field(repr=False)

    def __post_init__(self):
        if (not isinstance(self.endpoint, EndpointKey) or self.endpoint.account.platform != 'qq-official'
                or self.endpoint.kind != 'private' or not isinstance(self.actor, ActorKey)
                or self.actor.identity_kind != 'c2c_user' or not self.actor.matches_endpoint(self.endpoint)):
            denied()


@dataclass(frozen=True)
class HostAction:
    operation: str
    target_id: str = field(repr=False)
    arguments_digest: str = field(repr=False)
    evidence_json: str = field(default='',repr=False)

    def __post_init__(self):
        if (self.operation not in _OPS or not _text(self.target_id, 256)
                or not isinstance(self.arguments_digest, str)
                or re.fullmatch('[a-f0-9]{64}', self.arguments_digest) is None):
            denied()
        if self.evidence_json:
            try:
                evidence=json.loads(self.evidence_json)
                valid=(isinstance(evidence,dict) and canonical(evidence)==self.evidence_json
                    and len(self.evidence_json.encode())<=8192 and digest(self.evidence_json)==self.arguments_digest)
            except (ValueError,TypeError):
                valid=False
            if not valid:denied()

    def data(self):
        return {'operation':self.operation,'target_id':self.target_id,'arguments_digest':self.arguments_digest,
                'evidence_json':self.evidence_json}


@dataclass(frozen=True)
class TaskPolicy:
    actions: tuple[HostAction, ...] = field(repr=False)

    def __post_init__(self):
        if (type(self.actions) is not tuple or not 1 <= len(self.actions) <= 32
                or any(not isinstance(action, HostAction) for action in self.actions)
                or len(set(self.actions)) != len(self.actions)):
            denied()

    def data(self):
        return {'v':1,'actions':[action.data() for action in self.actions]}

    def permits(self, *, operation, target_id, arguments):
        try:
            encoded = canonical(arguments)
            if not isinstance(arguments, dict) or len(encoded.encode()) > 65536:
                return False
            return any(action.operation==operation and action.target_id==target_id
                and action.arguments_digest==digest(encoded) for action in self.actions)
        except (SecurityError, TypeError, ValueError):
            return False

    @classmethod
    def parse(cls, value):
        if (not isinstance(value, dict) or set(value) != {'v','actions'} or type(value['v']) is not int
                or value['v'] != 1 or not isinstance(value['actions'], list)):
            denied()
        try:
            return cls(tuple(HostAction(**entry) for entry in value['actions']))
        except (TypeError, ValueError):
            denied()


@dataclass(frozen=True)
class FrozenSource:
    endpoint: EndpointKey = field(repr=False)
    actor: ActorKey = field(repr=False)
    scope: ConversationScope = field(repr=False)
    execution_id: str | None = field(default=None, repr=False)
    task_id: int | None = field(default=None, repr=False)
    revision: int = 1
    route_epoch: int = 0
    endpoint_route_epoch: int = 1
    deadline_at: int = 0

    def __post_init__(self):
        if (not isinstance(self.endpoint, EndpointKey) or self.endpoint.account.platform != 'qq-official'
                or not isinstance(self.actor, ActorKey) or self.actor.identity_kind == 'bot'
                or not self.actor.matches_endpoint(self.endpoint)
                or not isinstance(self.scope, ConversationScope)
                or not _positive(self.revision) or not _positive(self.endpoint_route_epoch)
                or type(self.route_epoch) is not int or not 0 <= self.route_epoch <= 2**63-1
                or not _positive(self.deadline_at)
                or (self.execution_id is None) == (self.task_id is None)
                or (self.execution_id is not None and (not isinstance(self.execution_id,str)
                    or re.fullmatch('[a-f0-9]{32}',self.execution_id) is None))
                or (self.task_id is not None and not _positive(self.task_id))):
            denied()
        for value, limit in ((self.endpoint.account.key,512),(self.endpoint.key,1000),
                              (self.actor.key,1280),(self.scope.key,1000)):
            if not _text(value, limit):
                denied()

    def data(self):
        return {'endpoint':self.endpoint.key,'actor':self.actor.key,'scope':self.scope.key,
            'execution_id':self.execution_id,'task_id':self.task_id,'revision':self.revision,
            'route_epoch':self.route_epoch,'endpoint_route_epoch':self.endpoint_route_epoch,'deadline_at':self.deadline_at}


@dataclass(frozen=True)
class ApprovalContext:
    recipient: Recipient = field(repr=False)
    source: FrozenSource = field(repr=False)
    policy: TaskPolicy = field(repr=False)

    def __post_init__(self):
        if (not isinstance(self.recipient, Recipient) or not isinstance(self.source,FrozenSource)
                or not isinstance(self.policy,TaskPolicy)
                or self.recipient.endpoint.account != self.source.endpoint.account):
            denied()

    def payload(self):
        return {'source':self.source.data(),'policy':self.policy.data()}


class DenySourcePort:
    def require_current(self, cursor, *, source, policy, operation, now):
        denied()


class OfficialApprovalTransport:
    def __init__(self, *, token, secret: bytes, source_port=None, clock=time.time):
        if not isinstance(secret, bytes) or len(secret)<32:
            denied()
        self.token, self.secret, self.clock = token, secret, clock
        self.source_port = source_port or DenySourcePort()
        self.admins = AdminBindingStore(clock=clock)

    def now(self):
        value = self.clock()
        if type(value) not in (int,float) or not 0 <= value <= 2**63-1:
            denied()
        return int(value)

    def begin(self, cursor):
        assert_epoch(cursor,self.token)

    def store_where(self, alias=''):
        return alias+"identity_transport='qq_official' AND "+alias+'official_account_key=?', (self.token.account_key,)

    def recipient_where(self, recipient, alias=''):
        if not isinstance(recipient, Recipient) or recipient.endpoint.account.key != self.token.account_key:
            denied()
        clause, args = self.store_where(alias)
        return (clause+' AND '+alias+'official_endpoint_key=? AND '+alias+'official_actor_key=?',
                (*args,recipient.endpoint.key,recipient.actor.key))

    def recipient_bucket(self, recipient):
        _, args = self.recipient_where(recipient)
        return digest(canonical(list(args)))

    def account_for_recipient(self, cursor, recipient):
        self.recipient_where(recipient)
        admin = self.admins.resolve_admin(cursor, endpoint=recipient.endpoint, actor=recipient.actor)
        row = cursor.execute('SELECT * FROM admin_accounts WHERE account_id=? AND version=?',
                             (admin.admin_account_id,admin.admin_version)).fetchone()
        if row is None:
            denied()
        return public_account(dict(row))

    def request_fields(self, cursor, account, context, *, kind, payload):
        if not isinstance(context,ApprovalContext) or kind not in {'server_task','quorum_vote'}:
            denied()
        self.recipient_where(context.recipient)
        self.source_port.require_current(cursor,source=context.source,policy=context.policy,
                                         operation='approve',now=self.now())
        admin = self.admins.resolve_admin(cursor,endpoint=context.recipient.endpoint,actor=context.recipient.actor)
        if admin.admin_account_id != account['account_id'] or admin.admin_version != account['version']:
            denied()
        if kind == 'server_task':
            origin = self.admins.resolve_admin(cursor,endpoint=context.source.endpoint,actor=context.source.actor)
            if origin.admin_account_id != admin.admin_account_id or payload != context.payload():
                denied()
        source = context.source
        policy = canonical(context.policy.data())
        return dict(identity_transport='qq_official',qq_id=None,bot_id=None,
            official_account_key=self.token.account_key,official_endpoint_key=context.recipient.endpoint.key,
            official_actor_key=context.recipient.actor.key,official_admin_binding_id=admin.binding.binding_id,
            official_binding_revision=admin.binding.revision,official_source_endpoint_key=source.endpoint.key,
            official_source_actor_key=source.actor.key,official_source_scope_key=source.scope.key,
            official_execution_id=source.execution_id,official_task_id=source.task_id,
            official_source_revision=source.revision,official_route_epoch=source.route_epoch,
            official_endpoint_route_epoch=source.endpoint_route_epoch,official_source_deadline_at=source.deadline_at,
            official_policy_json=policy,official_policy_digest=digest(policy),expires_at=min(self.now()+180,source.deadline_at))

    def context(self, request, recipient=None):
        try:
            source = FrozenSource(EndpointKey.parse(request['official_source_endpoint_key']),
                ActorKey.parse(request['official_source_actor_key']),ConversationScope.parse(request['official_source_scope_key']),
                request['official_execution_id'],request['official_task_id'],request['official_source_revision'],
                request['official_route_epoch'],request['official_endpoint_route_epoch'],request['official_source_deadline_at'])
            policy = TaskPolicy.parse(json.loads(request['official_policy_json']))
            if canonical(policy.data()) != request['official_policy_json'] or digest(request['official_policy_json']) != request['official_policy_digest']:
                denied()
            return ApprovalContext(recipient or Recipient(EndpointKey.parse(request['official_endpoint_key']),
                ActorKey.parse(request['official_actor_key'])),source,policy)
        except (ValueError,TypeError,KeyError):
            denied()

    def current(self, cursor, request):
        if request.get('identity_transport')!='qq_official' or request['official_account_key']!=self.token.account_key:
            return False
        try:
            context = self.context(request)
            admin = self.admins.resolve_admin(cursor,endpoint=context.recipient.endpoint,actor=context.recipient.actor)
            if (admin.admin_account_id!=request['account_id'] or admin.admin_version!=request['account_version']
                    or admin.binding.binding_id!=request['official_admin_binding_id']
                    or admin.binding.revision!=request['official_binding_revision']):
                return False
            if self.now()>=context.source.deadline_at:
                return False
            self.source_port.require_current(cursor,source=context.source,policy=context.policy,operation='approve',now=self.now())
            return True
        except (PermissionError,SecurityError,ValueError,TypeError):
            return False

    def code_binding(self, request):
        return ['qq-official-approval-v1',*[request[name] for name in IDENTITY_FIELDS]]

    def same_channel(self, first, second):
        return (first['official_account_key']==second['official_account_key']
                and all(first[name]==second[name] for name in SOURCE_FIELDS))

    def same_identity(self, first, second):
        return first.get('identity_transport')=='qq_official' and all(first[name]==second[name] for name in IDENTITY_FIELDS)

    def eligible_count(self, cursor, request):
        return cursor.execute('''SELECT COUNT(*) AS n FROM qq_official_admin_bindings b
            JOIN admin_accounts a ON a.account_id=b.admin_account_id
            JOIN qq_official_owner_grants g ON g.grant_id=b.owner_grant_id
            JOIN qq_official_endpoints e ON e.endpoint_key=b.c2c_endpoint_key
            WHERE b.account_key=? AND b.state='active' AND a.enabled=1 AND a.role='admin'
            AND a.version=b.admin_account_version AND g.state='active' AND g.revision=b.owner_revision
            AND g.c2c_actor_key=b.c2c_actor_key AND e.kind='private' AND e.membership_state='present'
            AND e.binding_state<>'revoked' ''',(self.token.account_key,)).fetchone()['n']

    def review_context(self, cursor, parent, recipient):
        if not self.current(cursor,parent):
            denied()
        return self.context(parent,recipient)

    @staticmethod
    def render_code(request,code):
        from src.bot_security.service import MobileAuthorization
        endpoint = EndpointKey.parse(request['official_endpoint_key'])
        return render_plan(MessageBody((TextNode(0,MobileAuthorization.code_text(request,code)),)),
                           endpoint,markdown=False,max_parts=1)

    def body_commitment(self, source_id, request, rendered):
        if rendered.status!='ready' or len(rendered.parts)!=1:
            denied()
        values = [source_id,request['approval_id'],request['code_generation'],
                  request['official_account_key'],request['official_actor_key'],content_payload(rendered,0)]
        return hmac.new(self.secret,canonical(values).encode(),hashlib.sha256).hexdigest()

    def issued(self,cursor,request,code,code_hasher):
        now = self.now()
        if (not self.current(cursor,request) or request['expires_at']<=now
                or not hmac.compare_digest(request['code_hash'],code_hasher(request,code))):
            return False
        window = cursor.execute('''SELECT w.* FROM qq_official_reply_windows w
            JOIN qq_official_endpoints e ON e.endpoint_key=w.endpoint_key
            WHERE w.account_key=? AND w.endpoint_key=? AND w.credential_kind='msg_id'
            AND w.state='active' AND w.expires_at>? AND w.endpoint_generation=e.generation
            AND w.route_epoch=e.route_epoch AND w.used_count+w.reserved_count<w.max_replies
            ORDER BY w.issued_at DESC,w.window_id LIMIT 1 FOR SHARE OF w''',
            (self.token.account_key,request['official_endpoint_key'],now)).fetchone()
        if window is None:
            return False
        rendered = self.render_code(request,code)
        if rendered.status!='ready' or len(rendered.parts)!=1:
            return False
        source_id = secrets.token_hex(16)
        cursor.execute('''INSERT INTO qq_official_admin_otp_sources(source_id,approval_id,code_generation,
            account_key,endpoint_key,actor_key,window_id,account_epoch,body_commitment,
            approval_expires_at,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            (source_id,request['approval_id'],request['code_generation'],self.token.account_key,
             request['official_endpoint_key'],request['official_actor_key'],window['window_id'],self.token.epoch,
             self.body_commitment(source_id,request,rendered),request['expires_at'],now,
             min(now+30,request['expires_at'],window['expires_at'])))
        return True

    def delivered(self,cursor,approval_id,generation,success,current):
        row = cursor.execute('''SELECT * FROM admin_approvals WHERE approval_id=? AND code_generation=?
            AND identity_transport='qq_official' AND official_account_key=? AND status='sending' FOR UPDATE''',
            (approval_id,generation,self.token.account_key)).fetchone()
        if row is None:
            return
        source = cursor.execute('''SELECT s.*,d.status AS delivery_status FROM qq_official_admin_otp_sources s
            LEFT JOIN deliveries d ON d.delivery_id=s.delivery_id
            WHERE s.approval_id=? AND s.code_generation=?''',(approval_id,generation)).fetchone()
        okay = (success is True and source is not None and source['cancelled_at'] is None
            and source['expires_at']>self.now() and source['account_epoch']==self.token.epoch
            and source['delivery_status']=='committed' and current(cursor,dict(row)))
        cursor.execute('''UPDATE admin_approvals SET status=?,code_hash=CASE WHEN ? THEN code_hash ELSE '' END,
            updated_at=? WHERE approval_id=? AND code_generation=? AND status='sending' ''',
            ('pending' if okay else 'cancelled',okay,self.now(),approval_id,generation))
        if not okay and source is not None:
            cursor.execute('UPDATE qq_official_admin_otp_sources SET cancelled_at=? WHERE source_id=?',
                           (self.now(),source['source_id']))

    def recover(self,cursor):
        """Startup only: RAM OTPs from earlier owners are not reconstructed."""
        self.begin(cursor)
        cursor.execute('''UPDATE admin_approvals SET status='cancelled',code_hash='',updated_at=?
            WHERE identity_transport='qq_official' AND official_account_key=? AND status='sending' ''',
            (self.now(),self.token.account_key))

    def _source(self, cursor, source_id, *, now):
        if not isinstance(source_id, str) or re.fullmatch('[a-f0-9]{32}', source_id) is None:
            denied()
        row = cursor.execute('''SELECT * FROM qq_official_admin_otp_sources
            WHERE source_id=? AND account_key=? FOR UPDATE''', (source_id,self.token.account_key)).fetchone()
        if (row is None or row['cancelled_at'] is not None or row['expires_at']<=now
                or row['account_epoch']!=self.token.epoch):
            denied()
        request = cursor.execute('SELECT * FROM admin_approvals WHERE approval_id=? FOR UPDATE',
                                 (row['approval_id'],)).fetchone()
        if (request is None or request['status']!='sending' or request['code_generation']!=row['code_generation']
                or request['expires_at']!=row['approval_expires_at'] or not self.current(cursor,dict(request))):
            denied()
        return dict(row),dict(request)

    def validate_plan(self,cursor,*,token,source_id,rendered,requester,logical_key,
                      source_scope_key,source_native_message_id,expires_at,now):
        if token != self.token:
            denied()
        source, request = self._source(cursor,source_id,now=now)
        window = cursor.execute('SELECT * FROM qq_official_reply_windows WHERE window_id=?',
                                (source['window_id'],)).fetchone()
        endpoint = EndpointKey.parse(source['endpoint_key'])
        if (rendered.endpoint != endpoint or requester.key != source['actor_key']
                or logical_key != 'approval_otp:'+source_id or source_scope_key != endpoint.scope.key
                or expires_at != source['expires_at'] or window is None
                or window['credential_value'] != source_native_message_id
                or window['endpoint_key']!=endpoint.key or window['account_key']!=token.account_key
                or not hmac.compare_digest(self.body_commitment(source_id,request,rendered),source['body_commitment'])):
            denied()
        return source

    def bind_plan(self,cursor,*,source_id,plan_id,delivery_id):
        source,_ = self._source(cursor,source_id,now=self.now())
        if source['plan_id'] is not None:
            if source['plan_id']!=plan_id or source['delivery_id']!=delivery_id:
                denied()
            return
        cursor.execute('''UPDATE qq_official_admin_otp_sources SET plan_id=?,delivery_id=?
            WHERE source_id=? AND plan_id IS NULL AND attempt_id IS NULL''',(plan_id,delivery_id,source_id))
        if cursor.rowcount!=1:
            denied()

    def delivery_current(self,cursor,*,token,delivery,now):
        try:
            if token!=self.token or delivery['purpose']!='approval_otp' or delivery['payload_mode']!='ephemeral':
                return False
            plan = cursor.execute('SELECT * FROM delivery_plans WHERE plan_id=?',(delivery['plan_id'],)).fetchone()
            if plan is None:
                return False
            source, _ = self._source(cursor,plan['approval_source_id'],now=now)
            return (source['plan_id']==delivery['plan_id'] and source['delivery_id']==delivery['delivery_id']
                and source['account_key']==delivery['account_key'] and source['endpoint_key']==delivery['endpoint_key']
                and source['actor_key']==delivery['requester_actor_key']
                and source['expires_at']==delivery['expires_at']==plan['approval_source_expires_at']
                and (source['attempt_id'] is None or source['attempt_id']==delivery['current_attempt_id']))
        except (SecurityError,PermissionError,ValueError,TypeError):
            return False

    def choice_current(self,cursor,*,delivery,choice):
        row = cursor.execute('SELECT window_id FROM qq_official_admin_otp_sources WHERE delivery_id=? AND plan_id=?',
                             (delivery['delivery_id'],delivery['plan_id'])).fetchone()
        return row is not None and choice.send_mode=='passive' and choice.window_id==row['window_id']

    def bind_attempt(self,cursor,*,token,delivery,attempt_id,now):
        if not self.delivery_current(cursor,token=token,delivery=delivery,now=now):
            denied()
        cursor.execute('''UPDATE qq_official_admin_otp_sources SET attempt_id=?
            WHERE delivery_id=? AND plan_id=? AND attempt_id IS NULL''',
            (attempt_id,delivery['delivery_id'],delivery['plan_id']))
        if cursor.rowcount!=1:
            denied()


class ApprovalOtpSender:
    """Host-only bridge into the existing M3 sender; code is RAM-only.

    The callback cannot authorize itself: persisted approval generation, current
    admin/source, exact window, HMAC body and M3 plan are checked in one TX.
    """
    def __init__(self, *, sender, transport, store):
        self.sender,self.transport,self.store=sender,transport,store

    def _prepare(self, request, code, text):
        with self.sender.outbox.deliveries.transaction() as cursor:
            self.transport.begin(cursor)
            row = cursor.execute('''SELECT * FROM qq_official_admin_otp_sources
                WHERE approval_id=? AND code_generation=? AND account_key=?''',
                (request['approval_id'],request['code_generation'],self.transport.token.account_key)).fetchone()
            if row is None:
                return None
            source,current = self.transport._source(cursor,row['source_id'],now=self.transport.now())
            if (not isinstance(code,str) or re.fullmatch('[0-9]{6}',code) is None
                    or not hmac.compare_digest(current['code_hash'],self.store._code_hash(current,code))):
                denied()
            rendered = self.transport.render_code(current,code)
            from src.bot_security.service import MobileAuthorization
            if text!=MobileAuthorization.code_text(current,code):
                denied()
            window=cursor.execute('SELECT * FROM qq_official_reply_windows WHERE window_id=?',
                                  (source['window_id'],)).fetchone()
            if window is None:
                denied()
            return self.sender.outbox.enqueue(self.transport.token,rendered=rendered,
                requester=ActorKey.parse(source['actor_key']),logical_key='approval_otp:'+source['source_id'],
                source_scope_key=rendered.endpoint.scope.key,expires_at=source['expires_at'],purpose='approval_otp',
                source_native_message_id=window['credential_value'],transaction_cursor=cursor,
                approval_source_id=source['source_id'])

    async def send_code(self,request,code,text):
        return await self.sender.emit_ephemeral(self._prepare,request,code,text)

    async def send_receipt(self,request):
        # Typed control receipt integration is intentionally not connected by
        # this source: never replay an OTP or grant general notification rights.
        return 'failed'


from contextlib import contextmanager

@contextmanager
def task_effect_scope(store, *, context, operation, target_id, arguments):
    """Exact task grant inside current source/route and quorum locks.

    The existing operation runner owns once-only effect dispatch. This port
    authorizes only the frozen task policy; it neither creates work nor retries
    an ambiguous host operation. Admit a durable effect CAS under this cursor,
    commit, and release ALL SQL locks before external I/O. Never hold this
    scope over a network call.
    """
    if store.transport is None or not isinstance(context,ApprovalContext):
        denied()
    with store.transaction() as cursor:
        transport=store.transport
        account=transport.account_for_recipient(cursor,context.recipient)
        row=cursor.execute('''SELECT * FROM admin_approvals WHERE identity_transport='qq_official'
            AND official_account_key=? AND account_id=? AND kind='server_task' AND payload_hash=?
            ORDER BY created_at DESC LIMIT 1 FOR UPDATE''',
            (transport.token.account_key,account['account_id'],digest(canonical(context.payload())))).fetchone()
        if (row is None or row['status']!='succeeded' or not store._current(cursor,dict(row))
                or not store._quorum_valid(cursor,dict(row))
                or transport.context(row)!=context or row['payload_json']!=canonical(context.payload())
                or not context.policy.permits(operation=operation,target_id=target_id,arguments=arguments)):
            denied()
        transport.source_port.require_current(cursor,source=context.source,policy=context.policy,
            operation='execute',now=transport.now())
        yield cursor
