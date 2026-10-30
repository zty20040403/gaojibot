"""M7 project command source: exact M4 command, explicit M5 owner grant."""
from __future__ import annotations

import hashlib
import inspect
import json

from ...application.commands import CommandResult
from ...application.project_commands import command_result as project_result
from ...application.project_grant_commands import command_result as grant_result
from ...conversation_scope import ConversationScope
from ...platform_types import ActorKey
from ...project_authorization import ForegroundProjectProvider
from ...tools import neutral_registry_for
from ...tools.state import TurnState
from .commands import CommandEffectDenied
from .project_authority import Denied, Operation, ProjectAuthority, ProjectGrantRef, ProjectRequest


class CommandProjectProvider(ForegroundProjectProvider):
    def __init__(self, *, resource_origins=None, **kwargs):
        self.resource_origins=resource_origins
        if 'expected_execution_kind' in kwargs:
            raise Denied()
        super().__init__(**kwargs, expected_execution_kind='command')

    def recipient(self, cursor, message_id):
        """Original visible inbound evidence; NOT a membership or grant claim."""
        self._current(cursor)
        if type(message_id) is not int or not 0 < message_id < 2**63:
            raise Denied()
        row=cursor.execute('''SELECT m.sender_actor_key,m.occurred_at,m.source_endpoint_key,
                c.scope_key,i.account_key,i.endpoint_key,m.sender_actor_key AS actor_key,i.echo_state,i.status,
                i.endpoint_generation,i.route_epoch
            FROM messages m JOIN conversations c ON c.conversation_id=m.conversation_id
            JOIN conversation_visibility v ON v.conversation_id=m.conversation_id
            JOIN qq_official_inbound_events i ON i.canonical_message_id=m.canonical_message_id
            WHERE m.canonical_message_id=? AND m.source_endpoint_key=? AND c.scope_key=?
              AND m.direction='inbound' AND m.canonical_message_id>=v.min_canonical_message_id
              AND i.account_key=? AND i.endpoint_key=? AND i.endpoint_generation=? AND i.route_epoch=?
              AND i.native_message_id=m.native_message_id AND i.echo_state='not_echo' AND i.status='processed' ''',
            (message_id,self.endpoint.key,self.endpoint.scope.key,self.endpoint.account.key,self.endpoint.key,
             self.handle.endpoint_generation,self.handle.route_epoch)).fetchone()
        if (row is None or row['source_endpoint_key']!=self.endpoint.key or row['endpoint_key']!=self.endpoint.key
                or row['account_key']!=self.endpoint.account.key or row['scope_key']!=self.handle.scope_key
                or row['endpoint_generation']!=self.handle.endpoint_generation or row['route_epoch']!=self.handle.route_epoch
                or row['sender_actor_key']!=row['actor_key'] or row['echo_state']!='not_echo' or row['status']!='processed'
                or type(row['occurred_at']) is not int or not 0<=self._now()-row['occurred_at']<=300):
            raise Denied()
        try:actor=ActorKey.parse(row['actor_key'])
        except (TypeError,ValueError):raise Denied() from None
        if actor.identity_kind=='bot' or not actor.matches_endpoint(self.endpoint):raise Denied()
        return actor

    def _recipient(self,cursor,*,endpoint,actor,evidence_ref,source):
        if evidence_ref=='project-create:'+self.handle.execution_id:
            return super()._recipient(cursor,endpoint=endpoint,actor=actor,evidence_ref=evidence_ref,source=source)
        if (source is not self or endpoint!=self.endpoint or not isinstance(evidence_ref,str)
                or not evidence_ref.startswith('message:')):return False
        raw=evidence_ref[8:]
        if not raw.isascii() or not raw.isdigit() or len(raw)>19:return False
        return actor==self.recipient(cursor,int(raw))

    def grant_command(self,cursor,command):
        self._current(cursor)
        row=cursor.execute('SELECT scope_key FROM projects WHERE project_id=?',(command.project_id,)).fetchone()
        if row is None:raise Denied()
        scope=ConversationScope.from_key(row['scope_key'])
        issuer=ProjectRequest(self.endpoint,self.actor,scope,self.route_epoch,self.handle.route_epoch,
                              command.project_id,Operation.MANAGE)
        origin=None
        if scope.key!=self.endpoint.scope.key:
            if self.resource_origins is None:raise Denied()
            def owner(cursor):
                self._current(cursor)
                self.authority._owner(cursor,issuer)
                return True
            origin=self.resource_origins.capture(cursor,request=issuer,authorize=owner,now=self._now())
        def management_source(c,*,request,source):
            if source is not self or request!=issuer:return False
            self._current(c)
            if origin is None:return self._source_guard(c,request=request,source=source)
            self.resource_origins.check_origin(c,request=request,origin_id=origin.origin_id)
            return True
        authority=ProjectAuthority(source_guard=management_source,route_guard=self.route_guard,
            recipient_evidence=self._recipient,fresh_actor_evidence=self._fresh,clock=self.clock,
            max_grant_ttl_seconds=self.authority.max_grant_ttl_seconds)
        if command.action=='issue':
            recipient=self.recipient(cursor,command.message_id)
            grant_id=command.grant_id or hashlib.sha256(json.dumps([
                'qq-project-command:v1',self.endpoint.account.key,self.endpoint.key,self.actor.key,
                self.handle.execution_id,command.project_id,recipient.key],ensure_ascii=True).encode()).hexdigest()[:32]
            def issuance_source(c,*,request,source):
                return (management_source(c,request=request,source=source)
                    and self.recipient(c,command.message_id)==recipient)
            authority=ProjectAuthority(source_guard=issuance_source,route_guard=self.route_guard,
                recipient_evidence=self._recipient,fresh_actor_evidence=self._fresh,clock=self.clock,
                max_grant_ttl_seconds=self.authority.max_grant_ttl_seconds)
            grant=authority.issue_project(cursor,issuer=issuer,recipient=recipient,
                capabilities=frozenset(Operation(value) for value in command.capabilities),grant_id=grant_id,
                expires_at=self._now()+command.ttl_seconds,evidence_ref=f'message:{command.message_id}',
                source=self,expected_revision=command.expected_revision)
            if origin is not None:
                self.resource_origins.bind_grant(cursor,request=issuer,origin_id=origin.origin_id,grant=grant,now=self._now())
            return {'ok':True,'grant':{'grant_id':grant.grant_id,'revision':grant.revision}}
        if command.action=='revoke':
            grant=authority.revoke_project(cursor,issuer=issuer,
                grant=ProjectGrantRef(command.grant_id,command.expected_revision),source=self)
            return {'ok':True,'grant':{'grant_id':grant.grant_id,'revision':grant.revision}}
        # Reading the management list is an owner action, not broad membership.
        authority._prepare(cursor,issuer,self)
        self.authority._owner(cursor,issuer)
        authority._project(cursor,issuer)
        rows=cursor.execute('''SELECT grant_id,revision,state,capabilities,expires_at
            FROM qq_official_project_grants WHERE project_id=? AND account_key=? AND endpoint_key=?
            AND project_scope_key=? ORDER BY grant_id LIMIT 100''',
            (command.project_id,self.endpoint.account.key,self.endpoint.key,scope.key)).fetchall()
        self._current(cursor)
        return {'ok':True,'grants':[dict(row) for row in rows]}


class ProjectCommands:
    def __init__(self,*,inbound,provider,context,effect_scope,database_call):
        if (type(provider) is not CommandProjectProvider or provider.endpoint!=inbound.endpoint
                or provider.actor!=inbound.actor or context.inbound is not inbound or context.event is not None
                or context.platform.project_provider is not provider):
            raise CommandEffectDenied('project_command_source_invalid')
        self.inbound,self.provider,self.context=inbound,provider,context
        self.effect_scope,self.calls=effect_scope,database_call

    async def __call__(self,inbound,parsed):
        if inbound is not self.inbound:raise CommandEffectDenied('project_command_source_changed')
        words=parsed.argument.split(None,1)
        if parsed.name=='project_grant' or (words and words[0] in {'权限','授权','授予','撤销','更新'}):
            argument=parsed.argument
            if words and words[0]=='权限':argument=words[1] if len(words)>1 else ''
            async def call(command):
                def run():
                    with self.effect_scope(inbound) as cursor:
                        try:return self.provider.grant_command(cursor,command)
                        except Denied:return {'ok':False,'error':'当前入口无此项目的授权管理权限，或来源证据、版本、期限已失效。'}
                result=self.calls(run)
                return await result if inspect.isawaitable(result) else result
            return CommandResult('ok',await grant_result(argument,call))
        turn=TurnState(self.context)
        registry=neutral_registry_for(self.context)
        async def call(name,args):
            result=json.loads(await registry.execute(turn,name,args))
            return result
        return CommandResult('ok',await project_result(parsed.argument,call))
