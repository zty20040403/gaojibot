"""Explicit console grants for an exact old project and reviewed target actor.

Staging is not execution authority. A grant pins the future completed route
epoch; the original route guard keeps it unusable before that switch completes.
Planning binds it once to the journal whose preview contains the exact grant.
"""
from dataclasses import asdict
import json
import secrets
from types import SimpleNamespace

from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from .admin_bindings import AdminBindingStore, AdminBindingRef, AdminSnapshot
from .migration_routes import same_physical_scope
from .migration_types import MigrationDenied, MigrationItemProof, require_issued
from .project_authority import Operation, ProjectAuthority, ProjectGrantRef, ProjectRequest, ProjectSnapshot


def _grant_material(row):
    """Immutable issuance evidence; historical snapshots never grant authority."""
    return {key: row[key] for key in (
        'grant_id','project_id','account_key','endpoint_key','actor_key','principal_id',
        'project_scope_key','endpoint_generation','route_epoch','endpoint_route_epoch',
        'issuer_actor_key','owner_grant_id','owner_revision','issuer_link_revision',
        'capabilities','evidence_kind','evidence_ref','revision','issued_at','expires_at')}


class ProjectMigrationGrants:
    def __init__(self,services):
        self.services=services
        self.control=services.control
        self.admins=AdminBindingStore(clock=services.clock)

    async def mutate(self,*,cookie,csrf,action,request):
        if action not in {'project_grant_stage','project_grant_revoke'}:
            raise MigrationDenied('project_grant_action_unavailable')
        grant=self.control.issue(cookie=cookie,csrf=csrf,action=action,request=request)
        return await self.services.database_calls(self._mutate,grant,request)

    def _mutate(self,control,request):
        now=int(self.services.clock())
        with self.services.transaction() as cursor:
            if control.action=='project_grant_revoke':
                return self._revoke(cursor,control,request,now)
            required={'source_endpoint','source_scope','source_actor','target_endpoint','target_actor',
                'issuer_actor','project_id','mapping_id','mapping_revision','capabilities','expires_at'}
            if type(request) is not dict or set(request) not in (required,required|{'expected_revision'}):
                raise MigrationDenied('project_grant_request_invalid')
            expected=request.get('expected_revision',0)
            if type(expected) is not int or not 0<=expected<2**63-1:
                raise MigrationDenied('project_grant_revision_invalid')
            source,target=EndpointKey.parse(request['source_endpoint']),EndpointKey.parse(request['target_endpoint'])
            source_actor,actor,issuer=(ActorKey.parse(request[key]) for key in ('source_actor','target_actor','issuer_actor'))
            scope=ConversationScope.from_key(request['source_scope'])
            if (source.kind!='group' or target.kind!='group' or source.account.platform!='onebot-v11'
                    or target.account.platform!='qq-official' or not same_physical_scope(scope.key,source)
                    or not source_actor.matches_endpoint(source) or not actor.matches_endpoint(target)
                    or not issuer.matches_endpoint(target) or type(request['project_id']) is not int
                    or request['project_id']<=0):
                raise MigrationDenied('project_grant_selection_invalid')
            self.services.routes.lock(cursor,endpoints=(source,target),scopes=(scope.key,target.scope.key))
            cursor.execute('SELECT pg_advisory_xact_lock(hashtextextended(?,0))',('project-scope:'+scope.key,))
            admin=self.admins.resolve_admin(cursor,endpoint=target,actor=issuer)
            self.control.begin(cursor,control,action=control.action,request=request,now=now)
            if (admin.admin_account_id,admin.admin_version)!=(control.account_id,control.account_version):
                raise MigrationDenied('project_grant_owner_required')
            self.services.baseline_source(cursor,source,now)
            route=self.services.routes._route(cursor,scope.key) or self.services.routes._route(cursor,target.scope.key)
            if route is not None and (route['phase']!='planned' or route['source_endpoint_key']!=source.key
                    or route['target_endpoint_key']!=target.key or route['source_scope_key']!=scope.key):
                raise MigrationDenied('project_grant_route_unavailable')
            epoch=(route['epoch'] if route else 0)+2
            project=cursor.execute('SELECT * FROM projects WHERE project_id=? FOR UPDATE',(request['project_id'],)).fetchone()
            if (project is None or project['scope_key']!=scope.key or project['bot_native_user_id']!=source.account.app_id
                    or project['status'] in {'trash','purged'}):
                raise MigrationDenied('project_grant_origin_changed')
            mapping=self.services.mappings.lookup(cursor,source,scope,source_actor,target,now)
            if (mapping.mapping_id,mapping.revision,mapping.target)!=(request['mapping_id'],request['mapping_revision'],actor):
                raise MigrationDenied('project_grant_mapping_changed')
            expires=request['expires_at']
            if type(expires) is not int or not now<expires<=min(now+self.services.project_grant_ttl_seconds,mapping.expires_at):
                raise MigrationDenied('project_grant_deadline_invalid')
            if type(request['capabilities']) is not list or not request['capabilities']:
                raise MigrationDenied('project_grant_capabilities_invalid')
            capabilities=frozenset(Operation(value) for value in request['capabilities'])
            if len(capabilities)!=len(request['capabilities']):raise MigrationDenied('project_grant_capabilities_invalid')
            authority=ProjectAuthority(clock=lambda:now)
            principal=authority._principal(cursor,actor)
            physical=cursor.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?',(target.key,)).fetchone()
            issuer_request=ProjectRequest(target,issuer,scope,epoch,physical['route_epoch'],project['project_id'],Operation.MANAGE)
            owner,link=authority._owner(cursor,issuer_request)
            prior=cursor.execute('''SELECT * FROM qq_official_project_grants
                WHERE project_id=? AND endpoint_key=? AND actor_key=? FOR UPDATE''',
                (project['project_id'],target.key,actor.key)).fetchone()
            if prior is None:
                if expected:raise MigrationDenied('project_grant_revision_changed')
                identifier=secrets.token_hex(16)
            else:
                if (not expected or prior['revision']!=expected or
                        (prior['state']!='revoked' and prior['expires_at']>now)):
                    raise MigrationDenied('project_grant_already_exists')
                previous=cursor.execute('''SELECT * FROM migration_project_grants WHERE grant_id=?
                    ORDER BY grant_revision DESC LIMIT 1''',(prior['grant_id'],)).fetchone()
                if (previous is None or prior['evidence_kind']!='console_migration'
                        or prior['evidence_ref']!='migration-project:'+prior['grant_id']
                        or (prior['principal_id'],prior['project_scope_key'],prior['owner_grant_id'])!=(
                            principal,scope.key,owner.grant_id)
                        or (previous['source_endpoint_key'],previous['source_scope_key'],previous['source_actor_key'],
                            previous['original_bot_id'],previous['admin_account_id'])!=(
                            source.key,scope.key,source_actor.key,source.account.app_id,admin.admin_account_id)):
                    raise MigrationDenied('project_grant_origin_changed')
                identifier=prior['grant_id']
            revision=expected+1
            if prior is None:
                cursor.execute('''INSERT INTO qq_official_project_grants
                (grant_id,project_id,account_key,endpoint_key,actor_key,principal_id,project_scope_key,
                 endpoint_generation,route_epoch,endpoint_route_epoch,issuer_actor_key,owner_grant_id,owner_revision,
                 issuer_link_revision,capabilities,evidence_kind,evidence_ref,revision,state,issued_at,expires_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'console_migration',?,1,'active',?,?,?)''',
                (identifier,project['project_id'],target.account.key,target.key,actor.key,principal,scope.key,
                 physical['generation'],epoch,physical['route_epoch'],issuer.key,owner.grant_id,owner.revision,
                 link,sorted(cap.value for cap in capabilities),'migration-project:'+identifier,now,expires,now))
            else:
                cursor.execute('''UPDATE qq_official_project_grants SET endpoint_generation=?,route_epoch=?,
                    endpoint_route_epoch=?,issuer_actor_key=?,owner_revision=?,issuer_link_revision=?,capabilities=?,
                    revision=?,state='active',issued_at=?,expires_at=?,updated_at=?,revoked_at=NULL
                    WHERE grant_id=? AND revision=?''',
                    (physical['generation'],epoch,physical['route_epoch'],issuer.key,owner.revision,link,
                     sorted(cap.value for cap in capabilities),revision,now,expires,now,identifier,expected))
            current=cursor.execute('SELECT * FROM qq_official_project_grants WHERE grant_id=?',(identifier,)).fetchone()
            cursor.execute('''INSERT INTO migration_project_grants
                (grant_id,grant_revision,project_id,source_endpoint_key,source_scope_key,source_actor_key,
                 original_bot_id,target_account_key,target_endpoint_key,target_actor_key,mapping_id,mapping_revision,
                 expected_route_epoch,admin_account_id,admin_version,admin_binding_id,admin_binding_revision,created_at,
                 grant_snapshot_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (identifier,revision,project['project_id'],source.key,scope.key,source_actor.key,source.account.app_id,
                 target.account.key,target.key,actor.key,mapping.mapping_id,mapping.revision,epoch,
                 admin.admin_account_id,admin.admin_version,admin.binding.binding_id,admin.binding.revision,now,
                 json.dumps(_grant_material(current),sort_keys=True,separators=(',',':'),ensure_ascii=False)))
            self.control.audit(cursor,control,resource_id=identifier,now=now)
            return {'grant_id':identifier,'revision':revision,'state':'pending_migration','expires_at':expires}

    def _load(self,cursor,grant,now,*,readonly=False):
        if not isinstance(grant,ProjectGrantRef):raise MigrationDenied('project_grant_reference_required')
        stage=cursor.execute('SELECT * FROM migration_project_grants WHERE grant_id=? AND grant_revision=?',
                            (grant.grant_id,grant.revision)).fetchone()
        if stage is None:raise MigrationDenied('project_grant_unavailable')
        source,target=EndpointKey.parse(stage['source_endpoint_key']),EndpointKey.parse(stage['target_endpoint_key'])
        scope=ConversationScope.from_key(stage['source_scope_key'])
        if not readonly:
            self.services.routes.lock(cursor,endpoints=(source,target),scopes=(scope.key,target.scope.key))
            cursor.execute('SELECT pg_advisory_xact_lock(hashtextextended(?,0))',('project-scope:'+scope.key,))
        current=cursor.execute('SELECT * FROM qq_official_project_grants WHERE grant_id=?',(grant.grant_id,)).fetchone()
        if (stage is None or current is None or stage['grant_revision']!=grant.revision
                or current['revision']!=grant.revision or current['state']!='active'
                or not current['issued_at']<=now<current['expires_at'] or current['evidence_kind']!='console_migration'
                or current['evidence_ref']!='migration-project:'+grant.grant_id
                or current['route_epoch']!=stage['expected_route_epoch']
                or json.loads(stage['grant_snapshot_json'])!=_grant_material(current)):
            raise MigrationDenied('project_grant_unavailable')
        actor,issuer=ActorKey.parse(stage['target_actor_key']),ActorKey.parse(current['issuer_actor_key'])
        mapping=self.services.mappings.lookup(cursor,source,scope,ActorKey.parse(stage['source_actor_key']),target,now)
        if (mapping.mapping_id,mapping.revision,mapping.target)!=(stage['mapping_id'],stage['mapping_revision'],actor):
            raise MigrationDenied('project_grant_mapping_changed')
        if readonly:
            owner=self.admins.owners.owner_for_actor(cursor,target,issuer)
            row=cursor.execute('''SELECT b.* FROM qq_official_admin_bindings b JOIN admin_accounts a
                ON a.account_id=b.admin_account_id JOIN qq_official_endpoints e ON e.endpoint_key=b.c2c_endpoint_key
                WHERE b.binding_id=? AND b.state='active' AND a.enabled=1 AND a.role='admin'
                AND a.version=b.admin_account_version AND e.account_key=b.account_key
                AND e.kind='private' AND e.membership_state='present' AND e.binding_state<>'revoked' ''',
                (stage['admin_binding_id'],)).fetchone()
            if (owner is None or row is None or row['account_key']!=target.account.key
                    or (row['owner_grant_id'],row['owner_revision'],row['c2c_actor_key'])!=
                        (owner.grant_id,owner.revision,owner.c2c_actor_key)):
                raise MigrationDenied('project_grant_issuer_changed')
            admin=AdminSnapshot(row['admin_account_id'],row['admin_account_version'],
                AdminBindingRef(row['binding_id'],row['revision']),EndpointKey.parse(row['c2c_endpoint_key']),
                ActorKey.parse(row['c2c_actor_key']),row['owner_grant_id'],row['owner_revision'])
        else:
            admin=self.admins.resolve_admin(cursor,endpoint=target,actor=issuer)
            locked=cursor.execute('SELECT * FROM qq_official_project_grants WHERE grant_id=? FOR UPDATE',(grant.grant_id,)).fetchone()
            if dict(locked)!=dict(current):raise MigrationDenied('project_grant_changed')
        if (admin.admin_account_id,admin.admin_version,admin.binding.binding_id,admin.binding.revision)!=(
                stage['admin_account_id'],stage['admin_version'],stage['admin_binding_id'],stage['admin_binding_revision']):
            raise MigrationDenied('project_grant_issuer_changed')
        request=ProjectRequest(target,issuer,scope,current['route_epoch'],current['endpoint_route_epoch'],current['project_id'],Operation.MANAGE)
        authority=ProjectAuthority(clock=lambda:now)
        if readonly:
            owner=authority.owners.owner_for_actor(cursor,target,issuer)
            link_row=cursor.execute("SELECT revision FROM qq_official_owner_actor_links WHERE endpoint_key=? AND actor_key=? AND state='active'",(target.key,issuer.key)).fetchone()
            link=link_row['revision'] if link_row is not None else None
        else:owner,link=authority._owner(cursor,request)
        if owner is None:raise MigrationDenied('project_grant_owner_changed')
        if (owner.grant_id,owner.revision,link)!=(current['owner_grant_id'],current['owner_revision'],current['issuer_link_revision']):
            raise MigrationDenied('project_grant_owner_changed')
        physical=cursor.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?',(target.key,)).fetchone()
        if (physical['generation'],physical['route_epoch'])!=(current['endpoint_generation'],current['endpoint_route_epoch']):
            raise MigrationDenied('project_grant_endpoint_changed')
        principal=cursor.execute('''SELECT principal_id FROM principal_identities WHERE platform=? AND account_key=?
            AND identity_kind=? AND identity_namespace=? AND native_user_id=?''',
            (actor.account.platform,actor.account.key,actor.identity_kind,actor.identity_namespace,actor.native_user_id)).fetchone()
        if principal is None or principal['principal_id']!=current['principal_id']:raise MigrationDenied('project_grant_actor_changed')
        project=cursor.execute('SELECT * FROM projects WHERE project_id=?',(current['project_id'],)).fetchone()
        if (project is None or project['scope_key']!=stage['source_scope_key']
                or project['bot_native_user_id']!=stage['original_bot_id'] or project['status'] in {'trash','purged'}):
            raise MigrationDenied('project_grant_origin_changed')
        return dict(stage),dict(current)

    def preview(self,cursor,selection,ref,mapping,project_id,operation):
        row=cursor.execute('''SELECT s.grant_id,s.grant_revision FROM migration_project_grants s
            JOIN qq_official_project_grants g ON g.grant_id=s.grant_id AND g.revision=s.grant_revision
            WHERE s.project_id=? AND s.source_endpoint_key=? AND s.target_endpoint_key=? AND s.target_actor_key=?''',
            (project_id,selection.source.key,selection.target.key,mapping.target.key)).fetchone()
        if row is None:raise MigrationDenied('project_grant_required')
        grant=ProjectGrantRef(row['grant_id'],row['grant_revision'])
        stage,current=self._load(cursor,grant,int(self.services.clock()),readonly=True)
        if (stage['source_scope_key']!=selection.source_scope.key or stage['mapping_id']!=mapping.mapping_id
                or stage['mapping_revision']!=mapping.revision or stage['source_actor_key']!=mapping.source.key
                or 'project.'+operation not in current['capabilities']):
            raise MigrationDenied('project_grant_scope_changed')
        return grant

    def bind_plan(self,cursor,*,migration_id,now):
        journal,route=self.services.journal._load(cursor,migration_id)
        if route['phase']!='planned':raise MigrationDenied('project_grant_plan_phase')
        for item in cursor.execute('SELECT * FROM migration_items WHERE migration_id=?',(migration_id,)).fetchall():
            export=json.loads(item['export_json'])
            reference=export.get('mapping',{}).get('project_grant')
            if reference is None:continue
            grant=ProjectGrantRef(**reference)
            stage,current=self._load(cursor,grant,now)
            if (stage['source_endpoint_key'],stage['source_scope_key'],stage['target_endpoint_key'],stage['expected_route_epoch'])!=(
                    route['source_endpoint_key'],route['source_scope_key'],route['target_endpoint_key'],route['epoch']+2):
                raise MigrationDenied('project_grant_plan_changed')
            if stage['migration_id'] not in (None,migration_id):raise MigrationDenied('project_grant_already_bound')
            cursor.execute('''UPDATE migration_project_grants SET migration_id=?
                WHERE grant_id=? AND grant_revision=? AND migration_id IS NULL''',
                (migration_id,grant.grant_id,grant.revision))

    def require_staged(self,cursor,*,proof,project_id,grant,phase,now,job=None,operation=None):
        require_issued(proof,MigrationItemProof)
        return self._require(cursor,proof=proof,project_id=project_id,grant=grant,phase=phase,now=now,operation=operation)

    def require_binding(self,cursor,*,migration_id,item_id,project_id,grant,phase,now,operation=None):
        """Re-read a domain's stored binding; does not grant a worker lease."""
        _,route=self.services.journal._load(cursor,migration_id)
        item=cursor.execute('SELECT * FROM migration_items WHERE item_id=? AND migration_id=?',(item_id,migration_id)).fetchone()
        if item is None:raise MigrationDenied('project_grant_item_missing')
        source=SimpleNamespace(migration_id=migration_id,item_id=item_id,
            target_endpoint=EndpointKey.parse(route['target_endpoint_key']),target_actor=ActorKey.parse(item['target_actor_key']),
            mapping_id=item['mapping_id'],mapping_revision=item['mapping_revision'],route_epoch=route['epoch'],
            endpoint_route_epoch=route['target_endpoint_epoch'],export_digest=item['export_digest'])
        return self._require(cursor,proof=source,project_id=project_id,grant=grant,phase=phase,now=now,operation=operation)

    def _require(self,cursor,*,proof,project_id,grant,phase,now,operation):
        if phase not in {'prepare','execute','deliver'}:raise MigrationDenied('project_grant_phase_invalid')
        item=cursor.execute('SELECT * FROM migration_items WHERE item_id=? AND migration_id=?',
            (proof.item_id,proof.migration_id)).fetchone()
        if (item is None or item['state']=='revoked' or item['kind'] not in {'project_task','file'}
                or item['export_digest']!=proof.export_digest):raise MigrationDenied('project_grant_item_changed')
        # Source/job authority belongs to the domain; this additionally pins the
        # same journal, mapping, project and future route under its transaction.
        # Evidence reuse is confined to this synchronous authorization call.
        # Stamp BEFORE validation: a slow query cannot renew it.
        def clock_second():
            value=self.services.clock()
            if type(value) not in (int,float) or not 0<=value<=2**63-1:
                raise MigrationDenied('project_grant_clock_invalid')
            return int(value)
        if type(now) is not int or not 0<=now<=2**63-1:
            raise MigrationDenied('project_grant_clock_invalid')
        checked_at=clock_second()
        stage,current=self._load(cursor,grant,now)
        if (stage['migration_id'],stage['project_id'],stage['target_endpoint_key'],stage['target_actor_key'],
                stage['mapping_id'],stage['mapping_revision'],stage['expected_route_epoch'])!=(
                proof.migration_id,project_id,proof.target_endpoint.key,proof.target_actor.key,
                proof.mapping_id,proof.mapping_revision,proof.route_epoch):
            raise MigrationDenied('project_grant_item_changed')
        _,route=self.services.journal._load(cursor,proof.migration_id)
        allowed={'switching','verifying'} if phase=='prepare' else {'completed'}
        operation=operation or (Operation.DELIVER if phase=='deliver' else Operation.EXECUTE)
        if operation not in {Operation.EXECUTE,Operation.DELIVER}:raise MigrationDenied('project_grant_operation_invalid')
        if route['phase'] not in allowed or operation.value not in current['capabilities']:
            raise MigrationDenied('project_grant_not_active')
        if phase!='prepare':
            request=ProjectRequest(proof.target_endpoint,proof.target_actor,ConversationScope.from_key(stage['source_scope_key']),
                proof.route_epoch,proof.endpoint_route_epoch,project_id,operation)
            active=True
            def checked_source(c,*,request:ProjectRequest,source):
                # This closure is never a public source port or returned token.
                # The complete ordinary ProjectAuthority checks below still run.
                if not active or c is not cursor or source is not proof or request!=expected_request:
                    return False
                if clock_second()!=checked_at or checked_at!=now:
                    # No time extension: after a second boundary use the full
                    # original public check, which rereads mapping/admin/expiry.
                    return self.project_scope_guard(c,request=request,
                        project={'project_id':project_id},source=source)
                return (route['phase']=='completed'
                    and stage['expected_route_epoch']==route['epoch']==request.route_epoch
                    and stage['source_scope_key']==request.canonical_scope.key
                    and route['active_endpoint_key']==request.endpoint.key
                    and current['issued_at']<=checked_at<current['expires_at'])
            expected_request=request
            authority=ProjectAuthority(clock=self.services.clock,route_guard=lambda c,**kw:self._route(c,**kw),
                source_guard=checked_source,fresh_actor_evidence=self._fresh)
            try:
                return authority.require_project(cursor,request=request,grant=grant,source=proof)
            finally:
                active=False
        return ProjectSnapshot(grant,current['principal_id'],project_id,operation,current['expires_at'])

    def _route(self,cursor,**kwargs):
        self.services.routes.assert_active_route(cursor,**kwargs)
        return True

    @staticmethod
    def _fresh(cursor,*,request,now,**kwargs):
        from .actor_evidence import recent_actor_evidence
        return recent_actor_evidence(cursor,endpoint=request.endpoint,actor=request.actor,now=now,max_age_seconds=300)

    def project_scope_guard(self,cursor,*,request,project,source):
        # M7: a completed, explicitly included resource fact is independent of
        # old recipient mapping/grant expiry. Ordinary ProjectAuthority still
        # checks the current actor's exact grant/revision for every operation.
        origins=getattr(self.services,'project_resource_origins',None)
        if origins is not None and origins.project_scope_guard(cursor,request=request,project=project,source=source):
            return True
        row=cursor.execute('''SELECT s.grant_id,s.grant_revision FROM migration_project_grants s
            JOIN qq_official_project_grants g ON g.grant_id=s.grant_id AND g.revision=s.grant_revision
            WHERE s.project_id=? AND s.target_endpoint_key=? AND s.target_actor_key=?''',
            (request.project_id,request.endpoint.key,request.actor.key)).fetchone()
        if row is None:return False
        stage,_=self._load(cursor,ProjectGrantRef(row['grant_id'],row['grant_revision']),int(self.services.clock()))
        if stage['migration_id'] is None:return False
        _,route=self.services.journal._load(cursor,stage['migration_id'])
        return (route['phase']=='completed' and stage['expected_route_epoch']==route['epoch']==request.route_epoch
            and stage['source_scope_key']==request.canonical_scope.key and project['project_id']==request.project_id
            and route['active_endpoint_key']==request.endpoint.key)

    def _revoke(self,cursor,control,request,now):
        if type(request) is not dict or set(request)!={'grant_id','expected_revision'}:
            raise MigrationDenied('project_grant_request_invalid')
        ref=ProjectGrantRef(request['grant_id'],request['expected_revision'])
        row=cursor.execute('''SELECT * FROM migration_project_grants WHERE grant_id=?
            ORDER BY grant_revision DESC LIMIT 1''',(ref.grant_id,)).fetchone()
        if row is None:raise MigrationDenied('project_grant_unavailable')
        source,target=EndpointKey.parse(row['source_endpoint_key']),EndpointKey.parse(row['target_endpoint_key'])
        self.services.routes.lock(cursor,endpoints=(source,target),scopes=(row['source_scope_key'],target.scope.key))
        cursor.execute('SELECT pg_advisory_xact_lock(hashtextextended(?,0))',('project-scope:'+row['source_scope_key'],))
        if (control.account_id,control.account_version)!=(row['admin_account_id'],row['admin_version']):
            raise MigrationDenied('project_grant_owner_required')
        current=cursor.execute('SELECT * FROM qq_official_project_grants WHERE grant_id=?',(ref.grant_id,)).fetchone()
        admin=self.admins.resolve_admin(cursor,endpoint=target,actor=ActorKey.parse(current['issuer_actor_key']))
        self.control.begin(cursor,control,action=control.action,request=request,now=now)
        current=cursor.execute('SELECT * FROM qq_official_project_grants WHERE grant_id=? FOR UPDATE',(ref.grant_id,)).fetchone()
        if admin.admin_account_id!=control.account_id or current['revision']!=ref.revision:
            raise MigrationDenied('project_grant_revision_changed')
        if current['state']!='revoked':
            cursor.execute("UPDATE qq_official_project_grants SET state='revoked',revision=revision+1,revoked_at=?,updated_at=? WHERE grant_id=?",(now,now,ref.grant_id))
        self.control.audit(cursor,control,resource_id=ref.grant_id,now=now)
        return asdict(ProjectGrantRef(ref.grant_id,current['revision']+(current['state']!='revoked')))
