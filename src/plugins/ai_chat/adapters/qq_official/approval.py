"""Owner private-code approval using the shared authorization store and sender."""
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
from .validation import _positive, _text
from .lease import assert_epoch
from .renderer import render_plan
from .plans import content_payload


IDENTITY_FIELDS = ('official_account_key','official_endpoint_key','official_actor_key',
    'official_admin_binding_id','official_binding_revision','official_source_endpoint_key',
    'official_source_actor_key','official_source_scope_key','official_execution_id','official_task_id',
    'official_source_revision','official_route_epoch','official_endpoint_route_epoch',
    'official_source_deadline_at','official_policy_json','official_policy_digest')
SOURCE_FIELDS = IDENTITY_FIELDS[5:]
# Persisted approval requests may still name these historical host operations.
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
    def __init__(self, *, token, secret: bytes, owners, source_port=None, clock=time.time):
        if not isinstance(secret, bytes) or len(secret)<32:
            denied()
        self.token, self.secret, self.clock = token, secret, clock
        self.source_port = source_port or DenySourcePort()
        self.admins = AdminBindingStore(owners=owners, clock=clock)

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
        review = (f"\n高风险操作须两位不同管理员批准。另一位请私聊：复核 {request['approval_id']}\n"
                  if request['quorum'] > 1 else f"\n复核原操作：{request['parent_approval_id']}；记一票，不单独执行命令。\n" if request.get('parent_approval_id') else '')
        text = (f"待确认操作：{request['approval_id']}\n{request['summary']}\n\n"
                f"一次性口令：{code}\n3 分钟有效，仅本人可用，成功一次立即失效。\n"
                f"直接回复 {code} 记录本人的批准；达到所需人数才会执行。{review}\n"
                f"取消请回复：取消 {request['approval_id']}\n重发请回复：重发 {request['approval_id']}")
        endpoint = EndpointKey.parse(request['official_endpoint_key'])
        return render_plan(MessageBody((TextNode(0,text),)),
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
        window = cursor.execute("SELECT window_id FROM qq_official_reply_windows WHERE account_key=? AND endpoint_key=? ORDER BY received_at DESC LIMIT 1",
            (self.token.account_key, request['official_endpoint_key'])).fetchone()
        rendered = self.render_code(request,code)
        if rendered.status!='ready' or len(rendered.parts)!=1:
            return False
        source_id = secrets.token_hex(16)
        cursor.execute('''INSERT INTO qq_official_admin_otp_sources(source_id,approval_id,code_generation,
            account_key,endpoint_key,actor_key,window_id,account_epoch,body_commitment,
            approval_expires_at,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            (source_id,request['approval_id'],request['code_generation'],self.token.account_key,
             request['official_endpoint_key'],request['official_actor_key'],window['window_id'] if window else None,self.token.epoch,
             self.body_commitment(source_id,request,rendered),request['expires_at'],now,
             min(now+30,request['expires_at'])))
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
