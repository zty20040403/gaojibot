"""Completed migration resource facts, deliberately separate from permissions.

A ResourceOrigin is not an issued capability. It proves which project was
explicitly reviewed in a completed migration. Every operation still needs the
ordinary ProjectAuthority grant for the current actor, revision and deadline.
Historical actor mappings and staged-grant expiry are never current authority.
All methods enlist in the caller's transaction; no network or commits.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re

from .migration_routes import same_physical_scope
from .migration_types import MigrationDenied, digest, encoded
from .project_authority import Denied, ProjectGrantRef, ProjectRequest
from ...platform_types import EndpointKey


def _object(value):
    if not isinstance(value, str) or len(value.encode('utf-8')) > 262144:
        raise Denied()
    try:
        parsed = json.loads(value)
        if type(parsed) is not dict or encoded(parsed) != value:
            raise Denied()
        return parsed
    except (TypeError, ValueError, MigrationDenied):
        raise Denied() from None


def _hex(value, length):
    if not isinstance(value, str) or re.fullmatch('[a-f0-9]{'+str(length)+'}', value) is None:
        raise Denied()
    return value


@dataclass(frozen=True, repr=False)
class ResourceOrigin:
    origin_id: str
    project_id: int
    project_scope_key: str
    original_bot_id: str
    source_account_key: str
    source_endpoint_key: str
    target_account_key: str
    target_endpoint_key: str
    endpoint_generation: int
    endpoint_route_epoch: int
    logical_route_epoch: int
    migration_id: str
    route_id: str
    item_id: str
    preview_digest: str
    export_digest: str
    staged_grant_id: str
    staged_grant_revision: int
    staged_snapshot_digest: str

    def __repr__(self):
        return 'ResourceOrigin(<resource fact; not permission>)'


def origin_from_evidence(*, request, project, endpoint, route, journal, item, stage, aliases):
    """Pure validation for DB evidence; constructing a result grants nothing.

    Does not inspect current old mapping/grant validity or renew original jobs.
    An expired old item may still prove resource origin. Unknown/unreviewed
    content, revoked/quarantined items, rollback and changed routes cannot.
    """
    try:
        if not isinstance(request, ProjectRequest):
            raise Denied()
        source = EndpointKey.parse(route['source_endpoint_key'])
        target = request.endpoint
        if (source.account.platform != 'onebot-v11' or source.kind != 'group'
                or target.kind != 'group' or target.account.platform != 'qq-official'
                or not same_physical_scope(request.canonical_scope.key, source)
                or request.canonical_scope.key == target.scope.key
                or route['phase'] != 'completed' or route['active_endpoint_key'] != target.key
                or route['target_endpoint_key'] != target.key or route['target_account_key'] != target.account.key
                or route['source_account_key'] != source.account.key
                or route['source_scope_key'] != request.canonical_scope.key
                or route['target_scope_key'] != target.scope.key
                or route['epoch'] != request.route_epoch
                or endpoint['endpoint_key'] != target.key or endpoint['account_key'] != target.account.key
                or endpoint['membership_state'] != 'present' or endpoint['binding_state'] != 'active'
                or endpoint['generation'] != route['target_generation']
                or endpoint['route_epoch'] != route['target_endpoint_epoch']
                or endpoint['route_epoch'] != request.endpoint_route_epoch
                or aliases != {request.canonical_scope.key: route['route_id'], target.scope.key: route['route_id']}
                or project['project_id'] != request.project_id or project['scope_key'] != request.canonical_scope.key
                or project['status'] in {'trash', 'purged'}
                or project['bot_native_user_id'] != source.account.app_id
                or str(stage['original_bot_id']) != project['bot_native_user_id']):
            raise Denied()
        for value in (route['epoch'], endpoint['generation'], endpoint['route_epoch'], request.project_id):
            if type(value) is not int or not 1 <= value < 2**63:
                raise Denied()
        preview = _object(journal['preview_json'])
        if (journal['route_id'] != route['route_id'] or journal['approved_by'] is None
                or type(journal['approved_version']) is not int or journal['approved_version'] < 1
                or digest(preview) != journal['preview_digest']
                or item['migration_id'] != journal['migration_id'] or item['parent_item_id'] is not None
                or item['classification'] != 'unsent' or item['state'] not in {'reviewed', 'prepared', 'active', 'expired'}
                or item['kind'] not in {'project_task', 'file'}):
            raise Denied()
        for actual, frozen in (('source_endpoint_key','source_endpoint'),('source_scope_key','source_scope'),
                ('target_endpoint_key','target_endpoint'),('target_scope_key','target_scope'),
                ('target_generation','target_generation'),('target_endpoint_epoch','target_endpoint_epoch')):
            if route[actual] != preview[frozen]:
                raise Denied()
        reviewed = [record for record in preview['items'] if record['kind'] == item['kind']
                    and record['source_ref'] == item['source_ref']]
        if len(reviewed) != 1:
            raise Denied()
        frozen = reviewed[0]
        for field in ('kind','source_ref','source_revision','classification','export_json','export_digest'):
            if frozen[field] != item[field]:
                raise Denied()
        if (frozen['source_actor'] != item['source_actor_key']
                or frozen['mapping'] != {'mapping_id':item['mapping_id'],'revision':item['mapping_revision'],
                    'source':item['source_actor_key'],'target':item['target_actor_key'],
                    'target_endpoint':target.key,'expires_at':item['mapping_expires_at']}):
            raise Denied()
        data = _object(item['export_json'])
        if item['kind'] == 'project_task':
            included_project = data.get('project_id') if data.get('type') == 'project_task' else None
        else:
            included_project = (data.get('immutable_ref',{}).get('project_id')
                if data.get('type') == 'artifact_reference' and data.get('origin') == 'project' else None)
        if type(included_project) is not int or included_project != request.project_id:
            raise Denied()
        grant_ref = data.get('mapping',{}).get('project_grant')
        if (type(stage['grant_revision']) is not int or not 1<=stage['grant_revision']<2**63
                or grant_ref != {'grant_id':stage['grant_id'],'revision':stage['grant_revision']}):
            raise Denied()
        expected_stage = {
            'project_id':request.project_id,'source_endpoint_key':source.key,
            'source_scope_key':request.canonical_scope.key,'target_account_key':target.account.key,
            'target_endpoint_key':target.key,'expected_route_epoch':route['epoch'],
            'migration_id':journal['migration_id'],'source_actor_key':item['source_actor_key'],
            'target_actor_key':item['target_actor_key'],'mapping_id':item['mapping_id'],
            'mapping_revision':item['mapping_revision'],
        }
        if any(stage[field] != value for field,value in expected_stage.items()):
            raise Denied()
        old_snapshot = _object(stage['grant_snapshot_json'])
        for field,value in {
            'grant_id':stage['grant_id'],'revision':stage['grant_revision'],
            'project_id':request.project_id,'account_key':target.account.key,'endpoint_key':target.key,
            'actor_key':stage['target_actor_key'],'project_scope_key':request.canonical_scope.key,
            'route_epoch':route['epoch'],'endpoint_generation':endpoint['generation'],
            'endpoint_route_epoch':endpoint['route_epoch'],
        }.items():
            if old_snapshot[field] != value:
                raise Denied()
        material = dict(project_id=request.project_id,project_scope_key=request.canonical_scope.key,
            original_bot_id=project['bot_native_user_id'],source_account_key=source.account.key,
            source_endpoint_key=source.key,target_account_key=target.account.key,target_endpoint_key=target.key,
            endpoint_generation=endpoint['generation'],endpoint_route_epoch=endpoint['route_epoch'],
            logical_route_epoch=route['epoch'],migration_id=_hex(journal['migration_id'],32),
            route_id=_hex(route['route_id'],32),item_id=_hex(item['item_id'],32),
            preview_digest=_hex(journal['preview_digest'],64),export_digest=_hex(item['export_digest'],64),
            staged_grant_id=_hex(stage['grant_id'],32),staged_grant_revision=stage['grant_revision'],
            staged_snapshot_digest=digest(old_snapshot))
        return ResourceOrigin(origin_id=digest(material),**material)
    except (KeyError, TypeError, ValueError, AttributeError, MigrationDenied):
        raise Denied() from None


class ProjectResourceOrigins:
    def __init__(self, *, routes):
        self.routes = routes

    def _evidence(self, cursor, request, *, item_id=None):
        self.routes.lock(cursor,endpoints=(request.endpoint,),
            scopes=(request.canonical_scope.key,request.endpoint.scope.key))
        cursor.execute('SELECT pg_advisory_xact_lock(hashtextextended(?,0))',
            ('project-scope:'+request.canonical_scope.key,))
        route = self.routes._route(cursor,request.endpoint.scope.key)
        if route is None:
            raise Denied()
        project = cursor.execute('SELECT * FROM projects WHERE project_id=?',(request.project_id,)).fetchone()
        endpoint = cursor.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?',
            (request.endpoint.key,)).fetchone()
        journal = cursor.execute('SELECT * FROM migration_journals WHERE route_id=?',(route['route_id'],)).fetchone()
        if project is None or endpoint is None or journal is None:
            raise Denied()
        aliases = {row['scope_key']:row['route_id'] for row in cursor.execute(
            'SELECT * FROM migration_scope_aliases WHERE scope_key IN (?,?)',
            (request.canonical_scope.key,request.endpoint.scope.key)).fetchall()}
        rows = cursor.execute('''SELECT * FROM migration_items WHERE migration_id=?
            AND kind IN ('project_task','file') AND classification='unsent' ORDER BY item_id''',
            (journal['migration_id'],)).fetchall()
        if len(rows)>100:
            raise Denied()
        for item in rows:
            if item_id is not None and item['item_id'] != item_id:
                continue
            try:
                data = _object(item['export_json'])
                ref = data.get('mapping',{}).get('project_grant',{})
                stage = cursor.execute('''SELECT * FROM migration_project_grants
                    WHERE grant_id=? AND grant_revision=?''',(ref.get('grant_id'),ref.get('revision'))).fetchone()
                if stage is None:
                    continue
                return origin_from_evidence(request=request,project=project,endpoint=endpoint,route=route,
                    journal=journal,item=item,stage=stage,aliases=aliases)
            except Denied:
                continue
        raise Denied()

    def capture(self,cursor,*,request,authorize,now):
        """Register only validated facts after the caller's current owner check.

        The same transaction must subsequently issue the explicit ordinary grant.
        The caller supplies a host callback, never a model's permission boolean.
        """
        if type(now) is not int or not 0 <= now < 2**63:
            raise Denied()
        origin = self._evidence(cursor,request)
        if authorize(cursor) is not True:
            raise Denied()
        prior = cursor.execute('''SELECT * FROM qq_official_project_resource_origins
            WHERE project_id=? AND target_endpoint_key=? AND logical_route_epoch=?''',
            (request.project_id,request.endpoint.key,request.route_epoch)).fetchone()
        if prior is not None:
            current = self._evidence(cursor,request,item_id=prior['item_id'])
            if any(prior[key]!=value for key,value in asdict(current).items()):
                raise Denied()
            return current
        material = asdict(origin)
        columns = tuple(material)
        cursor.execute('INSERT INTO qq_official_project_resource_origins ('+','.join(columns)+',created_at) VALUES ('+
            ','.join('?' for _ in range(len(columns)+1))+')',(*material.values(),now))
        return origin

    def check_origin(self,cursor,*,request,origin_id):
        prior=cursor.execute('SELECT * FROM qq_official_project_resource_origins WHERE origin_id=?',
            (origin_id,)).fetchone()
        if prior is None:raise Denied()
        origin=self._evidence(cursor,request,item_id=prior['item_id'])
        if any(prior[key]!=value for key,value in asdict(origin).items()):raise Denied()
        return origin

    @staticmethod
    def _grant_digest(row):
        # Commitment only; the authoritative permission remains the ordinary
        # current grant, not a copied set of capabilities in this side table.
        return digest({key:row[key] for key in ('grant_id','revision','state','project_id','project_scope_key',
            'account_key','endpoint_key','actor_key','principal_id','endpoint_generation','route_epoch',
            'endpoint_route_epoch','issuer_actor_key','owner_grant_id','owner_revision','issuer_link_revision',
            'capabilities','evidence_kind','evidence_ref','issued_at','expires_at')})

    def bind_grant(self,cursor,*,request,origin_id,grant,now):
        if type(now) is not int or not 0<=now<2**63 or not isinstance(grant,ProjectGrantRef):raise Denied()
        origin=self.check_origin(cursor,request=request,origin_id=origin_id)
        row=cursor.execute('SELECT * FROM qq_official_project_grants WHERE grant_id=? FOR UPDATE',
            (grant.grant_id,)).fetchone()
        if (row is None or row['revision']!=grant.revision or row['state']!='active'
                or row['evidence_kind']!='owner_command' or not row['issued_at']<=now<row['expires_at']
                or any(row[key]!=value for key,value in {
                    'project_id':origin.project_id,'project_scope_key':origin.project_scope_key,
                    'account_key':origin.target_account_key,'endpoint_key':origin.target_endpoint_key,
                    'endpoint_generation':origin.endpoint_generation,'endpoint_route_epoch':origin.endpoint_route_epoch,
                    'route_epoch':origin.logical_route_epoch}.items())):raise Denied()
        commitment=self._grant_digest(row)
        prior=cursor.execute('SELECT * FROM qq_official_project_resource_grants WHERE grant_id=? AND grant_revision=?',
            (grant.grant_id,grant.revision)).fetchone()
        if prior is not None:
            if prior['origin_id']!=origin_id or prior['grant_digest']!=commitment:raise Denied()
            return
        cursor.execute('INSERT INTO qq_official_project_resource_grants VALUES (?,?,?,?,?)',
            (grant.grant_id,grant.revision,origin_id,commitment,now))

    def project_scope_guard(self,cursor,*,request,project,source):
        """Only a newly, explicitly bound grant revision may use these facts.

        A pre-existing console_migration grant retains its original mapping
        checks, even after another actor has registered this resource origin.
        """
        row=cursor.execute('''SELECT g.*,b.origin_id,b.grant_digest FROM qq_official_project_grants g
            JOIN qq_official_project_resource_grants b ON b.grant_id=g.grant_id AND b.grant_revision=g.revision
            WHERE g.project_id=? AND g.account_key=? AND g.endpoint_key=? AND g.actor_key=?''',
            (request.project_id,request.endpoint.account.key,request.endpoint.key,request.actor.key)).fetchone()
        if row is None:return False
        if row['evidence_kind']!='owner_command' or row['grant_digest']!=self._grant_digest(row):raise Denied()
        origin=self.check_origin(cursor,request=request,origin_id=row['origin_id'])
        if project['project_id']!=origin.project_id or project['scope_key']!=origin.project_scope_key:raise Denied()
        return True
