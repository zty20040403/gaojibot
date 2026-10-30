"""Cookie-authenticated migration control; issued values are not portable grants.

The host supplies the real Journal and baseline source. No request can construct
an execution source, identity mapping, runtime readiness proof, or verification.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hmac
import math
import re
import time
import weakref

from src.bot_security.store import SecurityError, digest as secret_digest
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from .migration_types import ControlEvidence, MigrationDenied, digest


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value < 2**63:
        raise MigrationDenied()
    return int(value)


def _hex(value, *, empty=False):
    if not isinstance(value, str) or not (empty and value == '' or re.fullmatch('[a-f0-9]{64}', value)):
        raise MigrationDenied()
    return value


@dataclass(frozen=True, eq=False, repr=False)
class ControlGrant:
    account_id: str
    account_version: int
    session_hash: str = field(repr=False)
    csrf_hash: str = field(repr=False)
    action: str
    request_digest: str
    preview_digest: str
    expires_at: int


class MigrationControlAuthority:
    """Exact request + current session + same-transaction authority for Journal."""
    def __init__(self, security, *, clock=time.time):
        self.security, self.clock = security, clock
        self._issued = weakref.WeakKeyDictionary()

    def issue(self, *, cookie, csrf, action, request, preview_digest=''):
        if (not isinstance(cookie, str) or not 1 <= len(cookie) <= 256
                or not isinstance(csrf, str) or not 1 <= len(csrf) <= 256
                or not isinstance(action, str) or not re.fullmatch('[a-z_]{1,40}', action)):
            raise MigrationDenied()
        _hex(preview_digest, empty=True)
        try:
            principal = self.security.session(cookie, csrf)
        except SecurityError:
            raise MigrationDenied() from None
        if principal.get('role') != 'admin':
            raise MigrationDenied()
        grant = ControlGrant(principal['account_id'], principal['version'], principal['session_hash'],
            secret_digest(csrf), action, digest(request), preview_digest, _time(self.clock()) + 30)
        self._issued[grant] = None
        return grant

    def _current(self, cursor, grant, now):
        if not isinstance(grant, ControlGrant) or grant not in self._issued or grant.expires_at <= _time(now):
            raise MigrationDenied()
        # Match SecurityStore's global -> admin/session order. Effect callers
        # acquire their account/endpoint/route/domain scope locks before here.
        cursor.execute('INSERT INTO admin_security_lock VALUES (1,0) ON CONFLICT(lock_id) DO NOTHING')
        cursor.execute('UPDATE admin_security_lock SET revision=revision+1 WHERE lock_id=1')
        row = cursor.execute('''SELECT s.csrf_hash FROM admin_accounts a JOIN admin_sessions s USING(account_id)
            WHERE a.account_id=? AND a.version=? AND a.enabled=1 AND a.role='admin'
            AND s.session_hash=? AND s.account_version=a.version AND s.expires_at>?
            FOR SHARE OF a,s''', (grant.account_id,grant.account_version,grant.session_hash,now)).fetchone()
        if row is None or not hmac.compare_digest(row['csrf_hash'],grant.csrf_hash):
            raise MigrationDenied()
        return ControlEvidence(grant.account_id,grant.account_version,grant.session_hash)

    def begin(self, cursor, grant, *, action, request, preview_digest='', now):
        evidence = self._current(cursor,grant,now)
        if (grant.action != action or grant.request_digest != digest(request)
                or grant.preview_digest != preview_digest):
            raise MigrationDenied('migration_request_changed')
        txid = cursor.execute('SELECT txid_current()').fetchone()[0]
        previous = self._issued[grant]
        if previous is not None and previous != txid:
            raise MigrationDenied('migration_control_replayed')
        self._issued[grant] = txid
        return evidence

    def control(self, cursor, grant, *, action, preview_digest, now):
        evidence = self._current(cursor,grant,now)
        if (self._issued[grant] != cursor.execute('SELECT txid_current()').fetchone()[0]
                or grant.action != action or grant.preview_digest != preview_digest):
            raise MigrationDenied('migration_control_not_bound')
        return evidence

    def audit(self, cursor, grant, *, resource_id='', now):
        evidence = self.control(cursor,grant,action=grant.action,preview_digest=grant.preview_digest,now=now)
        cursor.execute('''INSERT INTO migration_control_audit
            (admin_account_id,admin_version,action,resource_id,request_digest,preview_digest,created_at)
            VALUES (?,?,?,?,?,?,?)''', (evidence.account_id,evidence.account_version,grant.action,resource_id,
            grant.request_digest,grant.preview_digest,now))


class MigrationConsoleService:
    """Host composition. Transaction/calls and baseline getter are trusted ports."""
    def __init__(self, *, authority, mappings, transaction, calls, journal=None,
                 baseline_source=None, history=None, activation=None, project_grants=None,
                 verification=None, clock=time.time):
        self.authority,self.mappings,self.transaction,self.calls = authority,mappings,transaction,calls
        self.journal,self.baseline_source,self.clock = journal,baseline_source,clock
        self.history=history
        self.activation=activation
        self.project_grants=project_grants
        self.verification=verification

    async def request_verification(self, **request):
        if not callable(self.verification):
            raise MigrationDenied('migration_verification_unconfigured')
        return await self.verification(**request)

    def _activation(self,cursor,migration_id,operation,now):
        if self.activation is not None:
            return getattr(self.activation,operation)(cursor,migration_id=migration_id,now=now)
        if cursor.execute('''SELECT 1 FROM migration_items WHERE migration_id=?
            AND parent_item_id IS NULL AND classification='unsent' LIMIT 1''',(migration_id,)).fetchone():
            raise MigrationDenied('migration_activation_unconfigured')

    @staticmethod
    def _project_scopes(cursor,scopes):
        for scope in sorted(set(scopes)):
            cursor.execute('SELECT pg_advisory_xact_lock(hashtextextended(?,0))',('project-scope:'+scope,))

    async def mutate(self, *, cookie, csrf, action, request, preview_digest=''):
        if action in {'project_grant_stage','project_grant_revoke'}:
            if self.project_grants is None or preview_digest:
                raise MigrationDenied('project_grant_control_unavailable')
            return await self.project_grants.mutate(cookie=cookie,csrf=csrf,action=action,request=request)
        return await self.calls(self._mutate,cookie,csrf,action,request,preview_digest)

    def _mutate(self,cookie,csrf,action,request,preview_digest):
        grant=self.authority.issue(cookie=cookie,csrf=csrf,action=action,request=request,preview_digest=preview_digest)
        now=_time(self.clock())
        with self.transaction() as cursor:
            if action=='mapping_put':
                from .migration_preview import Selection
                selection=Selection(EndpointKey.parse(request['source_endpoint']),
                    ConversationScope.from_key(request['source_scope']),EndpointKey.parse(request['target_endpoint']))
                evidence=self.mappings.put(cursor,grant,selection=selection,
                    source_actor=ActorKey.parse(request['source_actor']),target_actor=ActorKey.parse(request['target_actor']),
                    evidence_inbox_id=request['evidence_inbox_id'],expected_revision=request['expected_revision'],
                    expires_at=request['expires_at'],now=now)
                return {'mapping_id':evidence.mapping_id,'revision':evidence.revision,'expires_at':evidence.expires_at}
            if action=='mapping_revoke':
                revision=self.mappings.revoke(cursor,grant,mapping_id=request['mapping_id'],
                    expected_revision=request['expected_revision'],now=now)
                return {'mapping_id':request['mapping_id'],'revision':revision,'state':'revoked'}
            if self.journal is None:
                raise MigrationDenied()
            if action=='plan':
                if self.baseline_source is None:
                    raise MigrationDenied()
                source=EndpointKey.parse(request['source_endpoint'])
                target=EndpointKey.parse(request['target_endpoint'])
                from .migration_routes import same_physical_scope
                scope=ConversationScope.from_key(request['source_scope'])
                if (source.kind!='group' or target.kind!='group' or target.account.platform!='qq-official'
                        or not same_physical_scope(scope.key,source)
                        or (scope.bot_native_user_id and scope.bot_native_user_id!=source.account.app_id)):
                    raise MigrationDenied('invalid_migration_selection')
                self.journal.routes.lock(cursor,endpoints=(source,target),
                    scopes=(scope.key,target.scope.key))
                self._project_scopes(cursor,(scope.key,target.scope.key))
                self.authority.begin(cursor,grant,action=action,request=request,preview_digest=preview_digest,now=now)
                result=self.journal.plan(cursor,source_endpoint=source,
                    source_scope=ConversationScope.from_key(request['source_scope']),
                    target_endpoint=EndpointKey.parse(request['target_endpoint']),
                    items=tuple((item['kind'],item['key']) for item in request['items']),authorization=grant,
                    baseline_source=self.baseline_source(cursor,source,now),deadline=request['deadline'],now=now)
                resource=result.migration_id
                if self.project_grants is not None:
                    self.project_grants.bind_plan(cursor,migration_id=resource,now=now)
            else:
                resource=request['migration_id']
                # Bind EVERY mutation to the exact preview that was reviewed;
                # phase actions never accept a client-chosen replacement digest.
                current,current_route=self.journal._load(cursor,resource)
                self._project_scopes(cursor,(current_route['source_scope_key'],current_route['target_scope_key']))
                self.authority.begin(cursor,grant,action=action,request=request,preview_digest=preview_digest,now=now)
                if current['preview_digest']!=preview_digest:
                    raise MigrationDenied('migration_review_stale')
                kwargs=dict(migration_id=resource,authorization=grant,now=now)
                if action=='refresh' and 'deadline' in request:
                    kwargs['deadline']=request['deadline']
                if action=='history_preview':
                    if self.history is None:
                        raise MigrationDenied()
                    result,_=self.history.prepare(cursor,chapter_ids=tuple(request['chapter_ids']),
                        public_note_ids=tuple(request['public_note_ids']),**kwargs)
                elif action=='freeze':
                    result=self.journal.freeze(cursor,reviewed_digest=preview_digest,**kwargs)
                elif action=='quarantine':
                    result=self.journal.quarantine_activity(cursor,activity_id=request['activity_id'],**kwargs)
                elif action=='rollback':
                    result=self.journal.rollback(cursor,finish=request['finish'],**kwargs)
                else:
                    method={'refresh':'refresh_plan','drain':'begin_draining','switch':'switch_target','verify':'begin_verification',
                            'complete':'complete'}.get(action)
                    if method is None:
                        raise MigrationDenied()
                    if action=='verify':
                        self._activation(cursor,resource,'prepare_migration',now)
                    if action=='complete' and current_route['phase']!='completed':
                        self._activation(cursor,resource,'require_prepared',now)
                    if ((action=='switch' and current_route['phase']=='switching')
                            or (action=='complete' and current_route['phase']=='completed')):
                        # A lost HTTP response never creates a second target or
                        # advances the original route epoch a second time.
                        result=None
                    else:
                        result=getattr(self.journal,method)(cursor,**kwargs)
                    if action=='refresh' and self.project_grants is not None:
                        self.project_grants.bind_plan(cursor,migration_id=resource,now=now)
                    if action=='switch':
                        self._activation(cursor,resource,'prepare_migration',now)
                    if action=='complete':
                        self._activation(cursor,resource,'activate_migration',now)
                        if self.history is not None:
                            self.history.activate(cursor,migration_id=resource)
            self.authority.audit(cursor,grant,resource_id=resource,now=now)
            return asdict(result) if hasattr(result,'__dataclass_fields__') else {'ok':True,'migration_id':resource}

    async def read(self, *, cookie, migration_id):
        # Read-only snapshots do not authorize effects. Check the latest session
        # both before the snapshot and after it, outside its transaction view.
        return await self.calls(self._read,cookie,migration_id)

    def _read(self,cookie,migration_id):
        def current():
            try:
                value=self.authority.security.session(cookie)
            except SecurityError:
                raise MigrationDenied() from None
            if value.get('role')!='admin':
                raise MigrationDenied()
            return value
        before=current()
        with self.transaction() as cursor:
            row=cursor.execute('''SELECT j.migration_id,j.preview_digest,j.preview_json,j.revision,j.deadline,
                r.phase,r.epoch FROM migration_journals j JOIN migration_routes r USING(route_id)
                WHERE migration_id=?''',(migration_id,)).fetchone()
            if row is None:
                raise MigrationDenied()
            result=dict(row)
        after=current()
        if any(before.get(k)!=after.get(k) for k in ('account_id','version','session_hash')):
            raise MigrationDenied()
        return result
