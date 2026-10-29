"""Native wiki source over existing pages/publications and durable jobs.

Collection capability is an explicit host port, default denied. This never
starts newcomer briefs and never borrows directed historical scope authority.
"""
from __future__ import annotations

import hashlib
import inspect
import json

from ...application.commands import CommandResult
from ...application.wiki_commands import format_page
from ...conversation_scope import ConversationScope
from ...group_wiki import GroupWikiStore, WikiLeaseLost
from ...platform_types import ActorKey, EndpointKey
from . import identity_store
from .keys import account_job_kind
from .lease import assert_epoch


def _deny(*args,**kwargs):return False


class OfficialWikiStore(GroupWikiStore):
    def __init__(self,*args,token,admission,owners,clock,collection_allowed=_deny,**kwargs):
        super().__init__(*args,**kwargs)
        self.token,self.admission,self.owners,self.clock=token,admission,owners,clock
        self.account_key=token.account_key
        self.job_kind=account_job_kind('wiki',self.account_key)
        self.collection_allowed=collection_allowed

    def _now(self):
        value=self.clock()
        if type(value) not in {int,float} or not 0<=value<2**63:raise WikiLeaseLost('wiki_clock_invalid')
        return int(value)

    def _group(self,scope):
        if (not isinstance(scope,ConversationScope) or scope.platform!='qq-official' or scope.kind!='group'
                or scope.account is None or scope.account.key!=self.account_key):
            raise WikiLeaseLost('wiki_scope_unavailable')

    def _job_scope(self,job):
        scope=ConversationScope.from_key(job.payload['scope_key'])
        self._group(scope)
        if job.kind!=self.job_kind:raise WikiLeaseLost('wiki_job_account_changed')
        return scope

    def _job_scope_key(self,scope):
        self._group(scope)
        return 'qq-wiki:'+hashlib.sha256(scope.key.encode()).hexdigest()

    def _job_key(self,scope,version,slot,revision):
        return 'qq-wiki:'+hashlib.sha256(json.dumps([scope.key,version,slot,revision],ensure_ascii=True).encode()).hexdigest()

    def _endpoint(self,cursor,scope):
        self._group(scope)
        assert_epoch(cursor,self.token)
        endpoint=EndpointKey(scope.account,'group',scope.native_conversation_id)
        row=cursor.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=? AND account_key=? FOR UPDATE',
            (endpoint.key,self.account_key)).fetchone()
        if row is None or row['membership_state']!='present' or row['binding_state']!='active':
            raise WikiLeaseLost('wiki_endpoint_unavailable')
        return endpoint,row

    def _guard(self,cursor,scope,*,job=None):
        endpoint,route=self._endpoint(cursor,scope)
        row=cursor.execute('''SELECT q.*,s.bot_id FROM qq_official_wiki_configurations q
            JOIN group_wiki_settings s ON s.scope_key=q.scope_key AND s.version=q.config_version
            WHERE q.scope_key=? AND q.account_key=? AND q.endpoint_key=? AND s.enabled''',
            (scope.key,self.account_key,endpoint.key)).fetchone()
        if (row is None or row['endpoint_generation']!=route['generation']
                or row['endpoint_route_epoch']!=route['route_epoch']
                or (job is not None and (job.kind!=self.job_kind or job.payload['config_version']!=row['config_version']))):
            raise WikiLeaseLost('wiki_configuration_changed')
        author=identity_store.snapshot(cursor,endpoint.account)
        if author is None or author.bot_actor.native_user_id!=row['bot_id']:
            raise WikiLeaseLost('wiki_bot_identity_unavailable')
        actor=ActorKey.parse(row['actor_key'])
        if actor.identity_kind!='group_member' or not actor.matches_endpoint(endpoint):raise WikiLeaseLost('wiki_actor_changed')
        owner=self.owners.owner_for_actor(cursor,endpoint,actor)
        link=cursor.execute('SELECT revision FROM qq_official_owner_actor_links WHERE endpoint_key=? AND actor_key=? AND state=\'active\'',
                            (endpoint.key,actor.key)).fetchone()
        if (owner is None or owner.grant_id!=row['owner_grant_id'] or owner.revision!=row['owner_revision']
                or link is None or link['revision']!=row['owner_link_revision']):
            raise WikiLeaseLost('wiki_owner_authority_changed')
        source_check=getattr(self.admission,'require_execution',None)
        if source_check is None:raise WikiLeaseLost('wiki_source_authority_unavailable')
        source=source_check(cursor,row['source_execution_id'],endpoint=endpoint,actor=actor,operation='execute')
        if source['logical_route_epoch']!=row['logical_route_epoch']:raise WikiLeaseLost('wiki_logical_route_changed')
        if self.collection_allowed(cursor,endpoint,self._now()) is not True:
            raise WikiLeaseLost('wiki_collection_capability_unavailable')
        return dict(row)

    def configure_command(self,cursor,*,life,inbound,enabled,expected_version):
        # Only current command source can create a new long-lived config.
        current=life._current(cursor)
        if (current['state']!='executing' or current['execution_kind']!='command'
                or life.handle.endpoint!=inbound.endpoint or life.handle.actor!=inbound.actor
                or inbound.scope!=inbound.endpoint.scope or type(enabled) is not bool):
            raise WikiLeaseLost('wiki_command_source_invalid')
        endpoint,route=self._endpoint(cursor,inbound.endpoint.scope)
        if self.collection_allowed(cursor,endpoint,self._now()) is not True and enabled:
            raise WikiLeaseLost('wiki_collection_capability_unavailable')
        owner=self.owners.owner_for_actor(cursor,endpoint,inbound.actor)
        link=cursor.execute('SELECT revision FROM qq_official_owner_actor_links WHERE endpoint_key=? AND actor_key=? AND state=\'active\'',
                            (endpoint.key,inbound.actor.key)).fetchone()
        if owner is None or link is None:raise WikiLeaseLost('wiki_owner_authority_unavailable')
        source_check=getattr(self.admission,'require_execution',None)
        if source_check is None:raise WikiLeaseLost('wiki_source_authority_unavailable')
        source=source_check(cursor,life.handle.execution_id,endpoint=endpoint,actor=inbound.actor,operation='execute')
        author=identity_store.snapshot(cursor,endpoint.account)
        if author is None:raise WikiLeaseLost('wiki_bot_identity_unavailable')
        cursor.execute('SELECT pg_advisory_xact_lock(hashtext(?))',('group-wiki:'+endpoint.scope.key,))
        previous=cursor.execute('SELECT version FROM group_wiki_settings WHERE scope_key=? FOR UPDATE',(endpoint.scope.key,)).fetchone()
        version=previous['version'] if previous else 0
        if type(expected_version) is not int or version!=expected_version:raise WikiLeaseLost('wiki_version_changed')
        now=self._now();version+=1
        cursor.execute('''INSERT INTO group_wiki_settings(scope_key,bot_id,enabled,newcomer_enabled,version,updated_at)
            VALUES (?,?,?,FALSE,?,?) ON CONFLICT(scope_key) DO UPDATE SET bot_id=excluded.bot_id,enabled=excluded.enabled,
            newcomer_enabled=FALSE,version=excluded.version,updated_at=excluded.updated_at,checked_slot='' ''',
            (endpoint.scope.key,author.bot_actor.native_user_id,enabled,version,now))
        cursor.execute('''INSERT INTO qq_official_wiki_configurations(scope_key,config_version,account_key,endpoint_key,
            actor_key,source_execution_id,endpoint_generation,endpoint_route_epoch,logical_route_epoch,
            owner_grant_id,owner_revision,owner_link_revision,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (endpoint.scope.key,version,self.account_key,endpoint.key,inbound.actor.key,life.handle.execution_id,
             route['generation'],route['route_epoch'],source['logical_route_epoch'],owner.grant_id,owner.revision,link['revision'],now))
        cursor.execute('''UPDATE durable_jobs SET status='cancelled',lease_owner='',lease_until=NULL,finished_at=?,updated_at=?
            WHERE kind=? AND scope_key=? AND status IN ('pending','running')''',(now,now,self.job_kind,self._job_scope_key(endpoint.scope)))
        return version

    def enabled_groups(self):
        with self._lock:
            return [dict(row) for row in self._connection.execute('''SELECT s.* FROM group_wiki_settings s
                JOIN qq_official_wiki_configurations q ON q.scope_key=s.scope_key AND q.config_version=s.version
                WHERE q.account_key=? AND s.enabled ORDER BY s.scope_key''',(self.account_key,)).fetchall()]

    def _fenced(self,cursor,job,timestamp,*,kind=None):
        if kind is not None and kind!=self.job_kind:raise WikiLeaseLost('wiki_newcomer_disabled')
        result=super()._fenced(cursor,job,timestamp,kind=self.job_kind)
        current=cursor.execute('SELECT lease_until,lease_generation FROM durable_jobs WHERE job_id=?',(job.job_id,)).fetchone()
        if (current is None or current['lease_generation']!=job.lease_generation
                or (current['lease_until'] or 0)<=self._now()):
            raise WikiLeaseLost('wiki_job_fence_changed')
        return result

    def execution(self,job,*,now=None):
        return super().execution(job,now=self._now() if now is None else now)

    def reserve_generation(self,job,*,now=None):
        return super().reserve_generation(job,now=self._now() if now is None else now)

    def prepare_generation(self,job,*,now=None):
        return super().prepare_generation(job,now=self._now() if now is None else now)

    def publish(self,job,items,*,now=None):
        return super().publish(job,items,now=self._now() if now is None else now)

    def configure(self,*args,**kwargs):raise WikiLeaseLost('wiki_requires_actual_command_source')
    def enqueue_newcomer(self,*args,**kwargs):raise WikiLeaseLost('wiki_newcomer_disabled')


class WikiCommands:
    def __init__(self,*,store,life,inbound,database_calls):
        self.store,self.life,self.inbound,self.calls=store,life,inbound,database_calls

    async def __call__(self,inbound,parsed):
        if inbound is not self.inbound:raise WikiLeaseLost('wiki_command_source_changed')
        if inbound.endpoint.kind!='group':return CommandResult('denied','请在对应群里使用 /群百科，不读取其他群的内容。')
        action=parsed.argument.strip()
        if action.startswith('新人简报'):
            return CommandResult('unsupported','尚未验证当前账号的成员事件，新人简报保持关闭。')
        def run():
            with self.life.effect_scope(inbound) as cursor:
                config=self.store.settings(inbound.endpoint.scope)
                if action in {'开启','关闭'}:
                    self.store.configure_command(cursor,life=self.life,inbound=inbound,enabled=action=='开启',expected_version=config['version'])
                    return CommandResult('ok','已开启群百科，将在夜间整理已有章节。' if action=='开启' else '已关闭群百科，并取消待执行的整理任务。')
                if not config['enabled']:return CommandResult('ok','本群百科尚未开启；当前本地 owner 可发送 /群百科 开启。')
                self.store._guard(cursor,inbound.endpoint.scope)
                return CommandResult('ok',format_page(self.store.page(inbound.endpoint.scope,query=action),action))
        try:
            result=self.calls(run)
            return await result if inspect.isawaitable(result) else result
        except WikiLeaseLost:
            return CommandResult('denied','群百科来源、权限、配置版本或事件能力不可用。')
