"""Explicit task-recipient mappings; never personal identity or principal links."""
from __future__ import annotations

import re
import secrets

from src.plugins.ai_chat.platform_types import ActorKey
from .migration_console import _time
from .migration_preview import Selection
from .migration_types import MappingEvidence, MigrationDenied, digest


def mapping_request(*, selection, source_actor, target_actor, evidence_inbox_id,
                    expected_revision, expires_at):
    return dict(source_endpoint=selection.source.key,source_scope=selection.source_scope.key,
        source_actor=source_actor.key,target_endpoint=selection.target.key,target_actor=target_actor.key,
        evidence_inbox_id=evidence_inbox_id,expected_revision=expected_revision,expires_at=expires_at)


def _key(selection, actor):
    return digest([selection.source.key,selection.source_scope.key,actor.key,selection.target.key])


def _selection(source, scope, actor, target):
    value=Selection(source,scope,target)
    if (source.kind!='group' or target.kind!='group' or not actor.matches_endpoint(source)
            or actor.identity_kind=='bot'):
        raise MigrationDenied('migration_mapping_namespace')
    return value


class MigrationMappingStore:
    def __init__(self, *, authority, routes, max_age_seconds=600, max_lifetime_seconds=86400):
        if (type(max_age_seconds) is not int or not 1<=max_age_seconds<=3600
                or type(max_lifetime_seconds) is not int or not 1<=max_lifetime_seconds<=30*86400):
            raise ValueError('invalid_mapping_policy')
        self.authority,self.routes=authority,routes
        self.max_age_seconds,self.max_lifetime_seconds=max_age_seconds,max_lifetime_seconds

    def _target(self,cursor,endpoint,*,lock=False):
        sql='SELECT * FROM qq_official_endpoints WHERE endpoint_key=? AND account_key=?'
        row=cursor.execute(sql+(' FOR UPDATE' if lock else ''),(endpoint.key,endpoint.account.key)).fetchone()
        if row is None or row['binding_state']!='active' or row['membership_state']!='present':
            raise MigrationDenied('migration_target_unavailable')
        return row

    def _evidence(self,cursor,selection,actor,inbox_id,route,now):
        if not isinstance(inbox_id,str) or not 1<=len(inbox_id)<=64:
            raise MigrationDenied()
        row=cursor.execute('''SELECT i.received_at FROM qq_official_inbound_events i
            JOIN messages m ON m.canonical_message_id=i.canonical_message_id
            WHERE i.inbox_id=? AND i.account_key=? AND i.endpoint_key=?
            AND i.status='processed' AND i.echo_state='not_echo'
            AND i.endpoint_generation=? AND i.route_epoch=? AND i.native_message_id=m.native_message_id
            AND m.source_endpoint_key=i.endpoint_key AND m.sender_actor_key=? AND m.direction='inbound'
            AND EXISTS (SELECT 1 FROM qq_official_gateway_receipts r WHERE r.account_key=i.account_key
                AND r.inbox_id=i.inbox_id AND r.disposition IN ('inbox','duplicate'))''',
            (inbox_id,selection.target.account.key,selection.target.key,route['generation'],route['route_epoch'],actor.key)).fetchone()
        if row is None or not now-self.max_age_seconds<=row['received_at']<=now:
            raise MigrationDenied('migration_target_evidence_required')
        return row['received_at']

    def put(self,cursor,grant,*,selection,source_actor,target_actor,evidence_inbox_id,
            expected_revision,expires_at,now):
        now=_time(now)
        selection=_selection(selection.source,selection.source_scope,source_actor,selection.target)
        if (not target_actor.matches_endpoint(selection.target) or target_actor.identity_kind=='bot'
                or type(expected_revision) is not int or not 0<=expected_revision<=2**63-2
                or type(expires_at) is not int or not now<expires_at<=now+self.max_lifetime_seconds):
            raise MigrationDenied()
        request=mapping_request(selection=selection,source_actor=source_actor,target_actor=target_actor,
            evidence_inbox_id=evidence_inbox_id,expected_revision=expected_revision,expires_at=expires_at)
        # Use the same account/route lock order as Journal before admin row locks.
        self.routes.lock(cursor,endpoints=(selection.source,selection.target),
            scopes=(selection.source_scope.key,selection.target.scope.key))
        self.authority.begin(cursor,grant,action='mapping_put',request=request,now=now)
        admin=self.authority.control(cursor,grant,action='mapping_put',preview_digest='',now=now)
        route=self._target(cursor,selection.target,lock=True)
        received=self._evidence(cursor,selection,target_actor,evidence_inbox_id,route,now)
        key=_key(selection,source_actor)
        row=cursor.execute('SELECT * FROM migration_actor_mappings WHERE mapping_key=? FOR UPDATE',(key,)).fetchone()
        if row is not None and (row['source_endpoint_key'],row['source_scope_key'],row['source_actor_key'],row['target_endpoint_key'])!=(
                selection.source.key,selection.source_scope.key,source_actor.key,selection.target.key):
            raise MigrationDenied('migration_mapping_conflict')
        if (row['revision'] if row else 0)!=expected_revision:
            raise MigrationDenied('migration_mapping_revision_changed')
        mapping_id=row['mapping_id'] if row else secrets.token_hex(16)
        revision=expected_revision+1
        if row is None:
            cursor.execute('''INSERT INTO migration_actor_mappings
                (mapping_id,mapping_key,source_endpoint_key,source_scope_key,source_actor_key,
                 target_account_key,target_endpoint_key,target_actor_key,revision,state,evidence_inbox_id,
                 evidence_received_at,endpoint_generation,endpoint_route_epoch,expires_at,
                 created_by_admin,updated_by_admin,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,'active',?,?,?,?,?,?,?,?,?)''',
                (mapping_id,key,selection.source.key,selection.source_scope.key,source_actor.key,
                 selection.target.account.key,selection.target.key,target_actor.key,revision,evidence_inbox_id,
                 received,route['generation'],route['route_epoch'],expires_at,admin.account_id,admin.account_id,now,now))
        else:
            cursor.execute('''UPDATE migration_actor_mappings SET target_actor_key=?,revision=?,state='active',
                evidence_inbox_id=?,evidence_received_at=?,endpoint_generation=?,endpoint_route_epoch=?,expires_at=?,
                updated_by_admin=?,updated_at=? WHERE mapping_id=?''',
                (target_actor.key,revision,evidence_inbox_id,received,route['generation'],route['route_epoch'],
                 expires_at,admin.account_id,now,mapping_id))
        self._history(cursor,mapping_id,admin,grant.request_digest,now)
        self.authority.audit(cursor,grant,resource_id=mapping_id,now=now)
        return self.lookup(cursor,selection.source,selection.source_scope,source_actor,selection.target,now)

    def _history(self,cursor,mapping_id,admin,request_digest,now):
        cursor.execute('''INSERT INTO migration_actor_mapping_revisions
            (mapping_id,revision,target_actor_key,state,evidence_inbox_id,evidence_received_at,
             endpoint_generation,endpoint_route_epoch,expires_at,admin_account_id,admin_version,request_digest,created_at)
            SELECT mapping_id,revision,target_actor_key,state,evidence_inbox_id,evidence_received_at,
             endpoint_generation,endpoint_route_epoch,expires_at,?,?,?,? FROM migration_actor_mappings
             WHERE mapping_id=?''',(admin.account_id,admin.account_version,request_digest,now,mapping_id))

    def revoke(self,cursor,grant,*,mapping_id,expected_revision,now):
        from src.plugins.ai_chat.platform_types import EndpointKey
        from src.plugins.ai_chat.conversation_scope import ConversationScope
        if (not isinstance(mapping_id,str) or not re.fullmatch('[a-f0-9]{32}',mapping_id)
                or type(expected_revision) is not int or not 1<=expected_revision<=2**63-2):
            raise MigrationDenied()
        now=_time(now)
        row=cursor.execute('SELECT * FROM migration_actor_mappings WHERE mapping_id=?',(mapping_id,)).fetchone()
        if row is None:
            raise MigrationDenied()
        source,target=EndpointKey.parse(row['source_endpoint_key']),EndpointKey.parse(row['target_endpoint_key'])
        scope=ConversationScope.from_key(row['source_scope_key'])
        self.routes.lock(cursor,endpoints=(source,target),scopes=(scope.key,target.scope.key))
        self.authority.begin(cursor,grant,action='mapping_revoke',
            request=dict(mapping_id=mapping_id,expected_revision=expected_revision),now=now)
        admin=self.authority.control(cursor,grant,action='mapping_revoke',preview_digest='',now=now)
        row=cursor.execute('SELECT * FROM migration_actor_mappings WHERE mapping_id=? FOR UPDATE',(mapping_id,)).fetchone()
        if row['revision']!=expected_revision:
            raise MigrationDenied('migration_mapping_revision_changed')
        cursor.execute('''UPDATE migration_actor_mappings SET state='revoked',revision=revision+1,
            updated_by_admin=?,updated_at=? WHERE mapping_id=?''',(admin.account_id,now,mapping_id))
        self._history(cursor,mapping_id,admin,grant.request_digest,now)
        self.authority.audit(cursor,grant,resource_id=mapping_id,now=now)
        return expected_revision+1

    def lookup(self,cursor,source_endpoint,source_scope,source_actor,target_endpoint,now):
        """Snapshot-safe read; caller's effect transaction owns current route locks."""
        selection=_selection(source_endpoint,source_scope,source_actor,target_endpoint)
        now=_time(now)
        route=self._target(cursor,target_endpoint)
        row=cursor.execute('SELECT * FROM migration_actor_mappings WHERE mapping_key=?',(_key(selection,source_actor),)).fetchone()
        if (row is None or row['state']!='active' or row['expires_at']<=now
                or (row['source_endpoint_key'],row['source_scope_key'],row['source_actor_key'],row['target_endpoint_key'],row['target_account_key'])!=(
                    source_endpoint.key,source_scope.key,source_actor.key,target_endpoint.key,target_endpoint.account.key)
                or (row['endpoint_generation'],row['endpoint_route_epoch'])!=(route['generation'],route['route_epoch'])):
            raise MigrationDenied('actor_mapping_unavailable')
        actor=ActorKey.parse(row['target_actor_key'])
        if not actor.matches_endpoint(target_endpoint) or actor.identity_kind=='bot':
            raise MigrationDenied()
        return MappingEvidence(row['mapping_id'],row['revision'],source_actor,actor,target_endpoint,
            row['expires_at'],source_endpoint,source_scope)
