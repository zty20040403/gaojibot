"""Explicit host assembly for the existing migration journal and runtimes.

No environment switch is introduced here. A host installs this service using
OneBotRuntime.migration_service_factory and passes official_factory to each
OfficialRuntime. Accounts and deployment digests must be explicit host config.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import re
import time
from src.bot_storage.database import StoreRow

from src.plugins.ai_chat.platform_types import AccountKey, ActorKey
from src.plugins.ai_chat.adapters.onebot_migration_runtime import DedicatedBaseline, OneBotMigrationRuntime, SourceJournal
from src.plugins.ai_chat.adapters.onebot_source_inventory import OneBotSourceInventory
from .migration_console import MigrationConsoleService, MigrationControlAuthority
from .migration_history_service import MigrationHistoryService
from .migration_host_ports import MigrationHostPorts, MigrationPlanGuard
from .migration_journal import Journal
from .migration_mappings import MigrationMappingStore
from .migration_native_sources import NativeSources, SourceAdmission
from .migration_preview import Previewer, Selection, SourceRef, MappingEvidence as PreviewMapping
from .migration_routes import RouteService, live_session
from .migration_types import ExportEvidence, MigrationDenied, MigrationItemProof, encoded, require_issued


@dataclass(frozen=True, repr=False)
class OneBotAccount:
    account: AccountKey
    deployment_digest: str

    def __post_init__(self):
        if (self.account.platform != 'onebot-v11'
                or re.fullmatch('[0-9]+',self.account.app_id) is None
                or re.fullmatch('[a-f0-9]{64}',self.deployment_digest) is None):
            raise MigrationDenied('invalid_migration_host_account')


@dataclass(frozen=True, repr=False)
class ExportDeadline:
    """Absolute reviewed host policy for sources lacking an original deadline.

    A mapping key is (full source endpoint key, kind, exact source ref).
    Changing this value changes the journal preview, not an existing item.
    """
    execution: int
    result: int
    schedule_json: str | None = None

    def __post_init__(self):
        if (type(self.execution) is not int or type(self.result) is not int
                or not 0 < self.execution <= self.result < 2**63):
            raise MigrationDenied('invalid_migration_deadline_policy')


class _Export:
    def __init__(self, preview, deadlines):
        self.preview, self.deadlines = preview, dict(deadlines)

    def __call__(self,cursor,source,scope,target,kind,ref,now):
        value=self.preview.preview(cursor,Selection(source,scope,target),SourceRef(kind,ref),now=now)
        data=value.export or {}
        revision=data.get('source_revision',data.get('version',1))
        actor=ActorKey.parse(data['mapping']['source_actor']) if value.disposition=='unsent' else None
        policy=self.deadlines.get((source.key,kind,ref))
        deadline=value.original_deadline
        if policy is None and deadline is not None:
            policy=ExportDeadline(deadline,deadline)
        disposition=value.disposition
        if disposition=='unsent' and policy is None:
            # Missing deadlines cannot become now+TTL on preview/restart.
            disposition='unsupported'
            actor=None
        if policy is not None and deadline is not None and policy.execution>deadline:
            raise MigrationDenied('migration_deadline_extension_denied')
        return ExportEvidence(kind,ref,revision,encoded(data),disposition,actor,value.safe_digest,
            policy.execution if policy else None,policy.result if policy else None,
            source_canonical_id=data.get('original_trigger_canonical_id'),
            schedule_json=policy.schedule_json if policy else None)


class _Domain:
    def __init__(self, service, kind):
        self.service,self.kind=service,kind

    def _get(self,cursor,item):
        if type(item) is MigrationItemProof:
            require_issued(item,MigrationItemProof)
            migration_id=item.migration_id
            if item.source_kind!=self.kind:raise MigrationDenied('migration_domain_source_unavailable')
        elif type(item) in (dict,StoreRow):migration_id=item.get('migration_id')
        else:raise MigrationDenied('migration_domain_source_unavailable')
        row=cursor.execute('''SELECT r.target_account_key FROM migration_journals j
            JOIN migration_routes r USING(route_id) WHERE j.migration_id=?''',(migration_id,)).fetchone()
        session=self.service.official.get(row['target_account_key']) if row else None
        domain=session.domains.get(self.kind) if session else None
        if domain is None:
            raise MigrationDenied('migration_domain_host_unavailable')
        return domain

    def validate_bound_job(self,cursor,item,job,**kwargs):
        return self._get(cursor,item).validate_bound_job(cursor,item,job,**kwargs)

    def validate_bound_plan(self,cursor,item,plan,**kwargs):
        return self._get(cursor,item).validate_bound_plan(cursor,item,plan,**kwargs)

    def validate_bound_schedule(self,cursor,item,schedule,**kwargs):
        return self._get(cursor,item).validate_bound_schedule(cursor,item,schedule,**kwargs)

    def validate_occurrence(self,cursor,item,**kwargs):
        return self._get(cursor,item).validate_occurrence(cursor,item,**kwargs)


class _Histories:
    def __init__(self, service):self.service=service

    def _get(self,cursor,migration_id):
        row=cursor.execute('''SELECT r.target_account_key FROM migration_journals j
            JOIN migration_routes r USING(route_id) WHERE j.migration_id=?''',(migration_id,)).fetchone()
        session=self.service.official.get(row['target_account_key']) if row else None
        if session is None:raise MigrationDenied('history_account_host_unavailable')
        return session.history

    def prepare(self,cursor,**kwargs):
        return self._get(cursor,kwargs['migration_id']).prepare(cursor,**kwargs)

    def activate(self,cursor,*,migration_id):
        return self._get(cursor,migration_id).activate(cursor,migration_id=migration_id)



class _Artifacts:
    """Journal callbacks resolve the actual target's current account holder."""
    def __init__(self, service, retained):
        self.service,self.retained=service,retained

    def validate_bound_artifact(self,cursor,item,artifact_id,**kwargs):
        require_issued(item,MigrationItemProof)
        session=self.service.official.get(item.target_endpoint.account.key)
        authority=None
        if session is not None and item.source_kind=='project_task':
            projects=session.project_sessions
            if projects is not None:
                candidate=projects.authority
                engine=session.domains.get('project_task')
                if (engine is not None and candidate.token is session.token
                        and getattr(candidate,'migration_projects',None) is engine):
                    authority=candidate
        elif session is not None and item.source_kind in {'subagent','file'}:
            authority=session.artifact_authority
        if (authority is None or session.token.account_key!=item.target_endpoint.account.key
                or authority.token is not session.token):
            raise MigrationDenied('migration_artifact_host_unavailable')
        return authority.validate_bound_artifact(cursor,item,artifact_id,**kwargs)

    def existing_artifact(self,cursor,source,**kwargs):
        if self.retained is None:
            raise MigrationDenied('retained_artifact_host_unavailable')
        return self.retained.existing_artifact(cursor,source,**kwargs)


class _Projects:
    """Stable shared-runtime router; all sources select their original account."""
    def __init__(self, services, projects):
        self.services,self.projects=services,projects

    def _session(self,account):
        session=self.services.official.get(account)
        if (session is None or session.project_sessions is None
                or not self.projects.ready or self.projects.closed
                or session.project_sessions.projects is not self.projects):
            from src.plugins.ai_chat.project_authorization import _deny
            _deny()
            raise MigrationDenied('project_account_host_unavailable')
        return session

    def authority(self,cursor,job):
        row=cursor.execute('SELECT account_key FROM qq_official_project_lifecycle_authority WHERE job_id=?',
            (job.job_id,)).fetchone()
        if row is None:raise MigrationDenied('project_source_unavailable')
        return self._session(row['account_key']).project_resolver.authority(cursor,job)

    async def __call__(self,project,principal_id,*,job):
        def account():
            with self.services.transaction() as cursor:
                row=cursor.execute('SELECT account_key FROM qq_official_project_lifecycle_authority WHERE job_id=?',
                    (job.job_id,)).fetchone()
                if row is None:raise MigrationDenied('project_source_unavailable')
                return row['account_key']
        key=await self.services.database_calls(account)
        return await self._session(key).project_resolver(project,principal_id,job=job)

    async def autosave(self,project,principal_id):
        """Select the original IO's account, never the resource's platform."""
        from src.plugins.ai_chat.project_autosave_authority import AutosaveProjectProvider
        def resolve():
            with self.services.transaction() as cursor:
                rows=cursor.execute('''SELECT DISTINCT account_key FROM qq_official_project_io_authority
                    WHERE project_id=? AND turn_id=?''',(project['project_id'],project['turn_id'])).fetchall()
                if not rows:return None  # unchanged legacy autosave path
                if len(rows)!=1:raise MigrationDenied('project_autosave_source_conflict')
                session=self._session(rows[0]['account_key']);resolver=session.project_resolver
                provider=AutosaveProjectProvider.load(cursor,token=session.token,project_id=project['project_id'],
                    turn_id=project['turn_id'],admission=resolver.admission,route_guard=resolver.route_guard,
                    project_scope_guard=resolver.project_scope_guard,clock=resolver.clock)
                access=provider.issue(cursor)
                if access.principal_id!=principal_id:raise MigrationDenied('project_autosave_actor_changed')
                return access
        try:return await self.services.database_calls(resolve)
        except MigrationDenied:
            from src.plugins.ai_chat.adapters.qq_official.project_authority import Denied
            raise Denied() from None

    def dispatch(self,cursor,*,access,**kwargs):
        session=self._session(access.endpoint.account.key)
        return session.project_dispatch(cursor,access=access,**kwargs)

    async def file(self,access,*args,**kwargs):
        return await self._session(access.endpoint.account.key).project_bridge(access,*args,**kwargs)


def migration_service_factory(*,onebot_accounts,security,**options):
    """Explicit OneBotRuntime factory; does not enable a deployment by itself."""
    def create(context,runtime):
        return MigrationServices(database=context.database,transaction=context.delivery_store.transaction,
            database_calls=context.db_call,security=security,onebot_accounts=onebot_accounts,
            canonical_scope=lambda event:runtime.handlers.chat._conversation_scope(event),**options)
    return create

class MigrationServices(OneBotMigrationRuntime):
    def __init__(self, *, database, transaction, database_calls, security,
                 onebot_accounts, canonical_scope, clock=time.time, target_resolver=None,
                 project_preview=None, deadlines=(), verification=None, artifacts=None,
                 official_domains=None, scheduled_profile=None, subagent_profile=None, file_admission=None,
                 project_grant_ttl_seconds=86400):
        if type(project_grant_ttl_seconds) is not int or not 60<=project_grant_ttl_seconds<=86400:
            raise MigrationDenied('project_grant_lifetime_invalid')
        self.project_grant_ttl_seconds=project_grant_ttl_seconds
        configs=tuple(onebot_accounts)
        if (any(type(c) is not OneBotAccount for c in configs)
                or len({c.account.key for c in configs})!=len(configs)):
            raise MigrationDenied('explicit_onebot_accounts_required')
        self.database,self.transaction,self.clock=database,transaction,clock
        self.database_calls=database_calls
        self.file_admission=file_admission
        self.project_router=None
        self.official,self.official_domains={},official_domains
        self._closed=False
        self.scheduled_profile,self.subagent_profile=scheduled_profile,subagent_profile
        routes=RouteService()
        sources=SourceJournal(routes,transaction,clock=clock)
        baselines={c.account.key:DedicatedBaseline(database.dsn,c.account,
            deployment_digest=c.deployment_digest,clock=clock) for c in configs}
        self.control=MigrationControlAuthority(security,clock=clock)
        self.mappings=MigrationMappingStore(authority=self.control,routes=routes)
        self.preview=Previewer(route_guard=self._preview_route,actor_mapping=self._preview_mapping,
            project_guard=project_preview or (lambda *args:False))
        self.ports=MigrationHostPorts(control=self.control,mappings=self.mappings,sources=sources,
            baselines=baselines,export=_Export(self.preview,deadlines),verification=verification,artifacts=_Artifacts(self,artifacts))
        routes.ports=self.ports
        self.journal=Journal(routes)
        from .migration_project_grants import ProjectMigrationGrants
        self.project_grants=ProjectMigrationGrants(self)
        from .project_resource_origins import ProjectResourceOrigins
        self.project_resource_origins=ProjectResourceOrigins(routes=routes)
        if project_preview is None:self.preview.project_guard=self.project_grants.preview
        if verification is None:
            from .migration_verification_host import VerificationHost
            self.ports.verifier=VerificationHost(journal=self.journal,control=self.control)
        self.ports.domains=tuple((frozenset({kind}),_Domain(self,kind)) for kind in ('reminder','routine','subagent','project_task','file'))
        super().__init__(sources=sources,baselines=baselines,canonical_scope=canonical_scope,
            inventory=OneBotSourceInventory(),target_resolver=target_resolver,database_calls=database_calls)
        from .migration_activation import MigrationActivation
        self.activation=MigrationActivation(journal=self.journal,domain_for_item=self.domain_for_item)
        self.console=MigrationConsoleService(authority=self.control,mappings=self.mappings,
            transaction=transaction,calls=database_calls,journal=self.journal,
            baseline_source=self.baseline_source,history=_Histories(self),activation=self.activation,
            project_grants=self.project_grants,verification=self.request_verification,clock=clock)

    async def start_native(self):
        """Native host owns no legacy baseline and installs no OneBot hooks.

        Journal/source guards remain in every official account session. Existing
        legacy evidence is read as persisted; no current holder is fabricated.
        """
        if self._renewal is not None:
            raise MigrationDenied('legacy_baseline_already_started')

    async def request_verification(self, **request):
        def target():
            with self.transaction() as cursor:
                row=cursor.execute('''SELECT r.target_account_key FROM migration_journals j
                    JOIN migration_routes r USING(route_id) WHERE j.migration_id=?''',
                    (request['migration_id'],)).fetchone()
                return row['target_account_key'] if row is not None else None
        account=await self.database_calls(target)
        session=self.official.get(account)
        if session is None:
            raise MigrationDenied('migration_verification_host_unavailable')
        plan=await session.request_verification(**request)
        # No native IDs, artifact handles or verification nonce leave this API.
        return {'state':'queued' if plan is not None else 'awaiting_file',
                'plan_id':plan.plan_id if plan is not None else None}

    def domain_for_item(self,cursor,item):
        return _Domain(self,item['kind'])._get(cursor,item)

    def admin_service(self):
        """Pass this getter as AdminServices.official_migration_service."""
        return self.console

    def baseline_source(self,cursor,endpoint,now):
        holder=self.baselines.get(endpoint.account.key)
        if holder is None:raise MigrationDenied('migration_source_account_unconfigured')
        holder.evidence(cursor,holder,account=endpoint.account,now=now)
        return holder

    def _preview_route(self,cursor,selection,ref,facts=None):
        if selection.source.account.key not in self.baselines:return False
        if ref is not None and facts and not facts.get('account_attributed'):
            # Shared legacy scope alone does not attribute a reminder/file to a
            # particular bot. A real source-journal record is required.
            source=cursor.execute('''SELECT 1 FROM migration_work_sources WHERE account_key=?
                AND endpoint_key=? AND canonical_scope_key=? AND source_kind=? AND source_ref=? LIMIT 1''',
                (selection.source.account.key,selection.source.key,selection.source_scope.key,ref.kind,ref.key)).fetchone()
            if source is None:return False
        row=cursor.execute('SELECT * FROM migration_runtime_baselines WHERE account_key=?',
            (selection.source.account.key,)).fetchone()
        if (row is None or row['state']!='ready' or row['expires_at']<=int(self.clock())
                or not live_session(cursor,row['backend_pid'],row['advisory_key'])):return False
        physical=cursor.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=? AND account_key=?',
            (selection.target.key,selection.target.account.key)).fetchone()
        if physical is None or physical['binding_state']!='active' or physical['membership_state']!='present':return False
        route=self.routes._route(cursor,selection.source_scope.key)
        return route is None or (route['source_endpoint_key']==selection.source.key
            and route['target_endpoint_key']==selection.target.key and route['source_scope_key']==selection.source_scope.key)

    def _preview_mapping(self,cursor,selection,ref,actor):
        value=self.mappings.lookup(cursor,selection.source,selection.source_scope,actor,selection.target,int(self.clock()))
        return PreviewMapping(value.mapping_id,value.revision,value.source,value.target_endpoint,value.target)

    def official_factory(self,runtime,token):
        """Current account epoch only; resources are removed before reacquisition."""
        if token.account_key in self.official:raise MigrationDenied('migration_account_host_already_installed')
        session=OfficialMigrationSession(self,runtime,token)
        self.official[token.account_key]=session
        return session

    async def configure_project_runtime(self,projects):
        """Called only after the shared ProjectRuntime.start has completed.

        Install the stable router into both runtime attributes and objects that
        captured these ports during start. Missing account/file hosts still deny.
        """
        from src.plugins.ai_chat.project_runtime import ProjectRuntime
        if (not isinstance(projects,ProjectRuntime) or not projects.ready or projects.closed
                or projects.store is None or getattr(projects,'lifecycle',None) is None):
            raise MigrationDenied('project_runtime_not_ready')
        if self.project_router is not None and self.project_router.projects is not projects:
            raise MigrationDenied('project_runtime_binding_conflict')
        router=self.project_router or _Projects(self,projects)
        if projects.official_provider_factory not in (None,router):
            raise MigrationDenied('project_runtime_binding_conflict')
        projects.official_provider_factory=router
        projects.official_task_dispatch=router.dispatch
        projects.official_file_delivery=router.file
        projects.lifecycle.official_authority=router.authority
        if projects.task_admission is not None:
            projects.task_admission.bindings.official_dispatch=router.dispatch
        self.project_router=router
        for session in tuple(self.official.values()):session.configure_project_runtime(projects)

    def file_services_factory(self,config=None):
        """Compose the existing M6 factory with the same issued source owner."""
        from .file_services import file_services_factory
        async def create(runtime,token,rest):
            if config is None:return None
            session=self.official.get(token.account_key)
            if session is None or session.runtime is not runtime or session.token is not token:
                raise MigrationDenied('migration_account_host_unavailable')
            configured=session.file_configuration(config)
            files=await file_services_factory(configured)(runtime,token,rest)
            try:
                session.attach_file_services(files)
            except BaseException:
                await files.close()
                raise
            return files
        return create

    async def close(self):
        if self._closed:return
        for session in tuple(self.official.values()):await session.close()
        await super().close()
        self._closed=True


class OfficialMigrationSession:
    def __init__(self,services,runtime,token):
        self.services,self.runtime,self.token=services,runtime,token
        self.native=NativeSources(services.routes,verifier=services.ports.verifier)
        self.admission=SourceAdmission(runtime.store.admitted,self.native)
        self.domains={}
        from .migration_task_sources import MigrationTaskSources
        self.task_sources=MigrationTaskSources(token=token,domains=self.domains,journal=services.journal)
        self.artifact_authority=None
        self.project_sessions=None
        self.project_resolver=self.project_dispatch=self.project_bridge=None
        self._files=None
        self.history=MigrationHistoryService(services.journal,token=token,
            reader_guard=self._reader,clock=runtime.store.clock)
        self.guard=MigrationPlanGuard(native=self.native,journal=services.journal)
        self._attached=False
        self._next_maintenance=0

    def attach(self):
        runtime,services=self.runtime,self.services
        if runtime.store.source_authority is not None or runtime.outbox.source_guard is not None:
            raise MigrationDenied('migration_host_binding_conflict')
        services.routes.tokens[self.token.account_key]=self.token
        runtime.store.source_authority=self.native
        runtime.store.admitted=self.admission
        runtime.outbox.admission=self.admission
        runtime.outbox.source_guard=self.guard
        self._attached=True
        app=runtime.application
        if services.scheduled_profile is not None:
            from .migration_recurring import ScheduledContinuations
            from .migration_continuation import SharedScheduledModel
            if app is None or app.usage_store is None:
                raise MigrationDenied('migration_schedule_host_unavailable')
            profile=app.model_catalog.resolve(services.scheduled_profile)
            scheduled=ScheduledContinuations(runtime.database,token=self.token,jobs=runtime.jobs,
                usage=app.usage_store,outbox=runtime.outbox,journal=services.journal,
                preview=services.preview,clock=runtime.store.clock,
                run_model=SharedScheduledModel(lambda work:profile),database_calls=runtime._calls)
            self.domains.update(reminder=scheduled,routine=scheduled)
        if services.subagent_profile is not None:
            from .migration_subagents import SubAgentContinuations
            if app is None or app.subagent_coordinator is None:
                raise MigrationDenied('migration_subagent_host_unavailable')
            app.model_catalog.resolve(services.subagent_profile)
            self.domains['subagent']=SubAgentContinuations(runtime.database,token=self.token,
                jobs=runtime.jobs,coordinator=app.subagent_coordinator,journal=services.journal,
                preview=services.preview,profile=services.subagent_profile,admission=self.admission,
                clock=runtime.store.clock,file_admission=services.file_admission)
        # Domain factory returns actual existing adapters using this runtime's
        # token/jobs/outbox/coordinator. It is never an approval callback.
        if services.official_domains is not None:
            additional=dict(services.official_domains(self))
            if set(additional)&set(self.domains):raise MigrationDenied('migration_domain_conflict')
            self.domains.update(additional)
        self._refresh_plan_sources()
        from .migration_subagents import SubAgentContinuations
        from .migration_artifacts import MigrationArtifactAuthority
        domain=self.domains.get('subagent')
        if domain is not None:
            if not isinstance(domain,SubAgentContinuations) or domain.token is not self.token:
                raise MigrationDenied('migration_subagent_host_changed')
            self.artifact_authority=MigrationArtifactAuthority(continuations=domain)

    def _refresh_plan_sources(self):
        # Both task domains share a persistence table. Select by its real item
        # kind rather than asking the first engine to authorize every task plan.
        others=(domain for kind,domain in self.domains.items() if kind not in {'subagent','project_task'})
        self.guard.continuations=tuple(dict.fromkeys((*others,self.task_sources)))

    def file_configuration(self,config):
        from .file_authority import ArtifactSourceAuthorities
        from .migration_files import MigrationFiles
        from .migration_artifacts import MigrationArtifactAuthority
        from pathlib import Path
        # Imports use the same M6 artifact store and account holder. No file
        # domain is installed when the host has not configured file services.
        if 'file' not in self.domains:
            app=self.runtime.application
            state_dir=getattr(app,'state_dir',None)
            imports=MigrationFiles(self.runtime.database,token=self.token,jobs=self.runtime.jobs,
                journal=self.services.journal,preview=self.services.preview,admission=self.admission,
                clock=self.runtime.store.clock,database_calls=self.runtime._calls,
                legacy_snapshot_root=Path(state_dir)/'subagent_artifacts' if state_dir is not None else None,
                project_grants=self.services.project_grants)
            self.domains['file']=imports
            if self.artifact_authority is None:
                self.artifact_authority=MigrationArtifactAuthority(continuations=None,imports=imports)
            else:self.artifact_authority.imports=imports
            self._refresh_plan_sources()
        configured=config.source_authority
        authorities=[]
        if isinstance(configured,ArtifactSourceAuthorities):
            authorities=[configured.resolve(kind) for kind in ('project','migration') if configured.resolve(kind) is not None]
        elif configured is not None:authorities=[configured]
        if any(getattr(value,'kind',None)=='migration' for value in authorities):
            raise MigrationDenied('migration_file_authority_conflict')
        if self.artifact_authority is not None:authorities.append(self.artifact_authority)
        return replace(config,source_authority=ArtifactSourceAuthorities(*authorities) if authorities else None)

    def attach_file_services(self,files):
        if files.token is not self.token or self._files is not None:
            raise MigrationDenied('migration_file_host_changed')
        if self.artifact_authority is not None:
            if files.config.source_authority.resolve('migration') is not self.artifact_authority:
                raise MigrationDenied('migration_file_authority_changed')
            if files.generation.source_factory is not None:
                raise MigrationDenied('migration_file_source_conflict')
            if self.artifact_authority.engine is not None:
                files.generation.source_factory=self.artifact_authority.issue
        imports=self.domains.get('file')
        if imports is not None:
            if imports.token is not self.token or imports.artifacts not in (None,files.artifacts):
                raise MigrationDenied('migration_file_host_changed')
            imports.artifacts=files.artifacts
        self._files=files
        if self.services.project_router is not None:
            self.configure_project_runtime(self.services.project_router.projects)

    def _project_engine(self,projects,authority):
        """Construct metadata composition only; every use still checks its DB source.

        No profile/configuration string grants migration, project or file access.
        The actual journal/item/task router selects this same engine at use time.
        """
        from .migration_project_tasks import ProjectTaskContinuations
        from .migration_project_artifacts import ProjectMigrationArtifacts
        existing=self.domains.get('project_task')
        configured=getattr(authority,'migration_projects',None)
        adapter=getattr(authority,'migration_artifacts',None)
        profile=self.services.subagent_profile
        if profile is None or not callable(self.services.file_admission):
            if existing is not None or configured is not None or adapter is not None:
                raise MigrationDenied('project_migration_host_changed')
            return None,None
        app=self.runtime.application
        if (app is None or app.subagent_coordinator is None
                or projects.store.database is not self.runtime.database
                or self.services.project_grants is None):
            raise MigrationDenied('project_migration_host_unavailable')
        app.model_catalog.resolve(profile)
        expected=dict(database=self.runtime.database,token=self.token,jobs=self.runtime.jobs,
            coordinator=app.subagent_coordinator,journal=self.services.journal,
            preview=self.services.preview,profile=profile,admission=self.admission,
            clock=self.runtime.store.clock,grants=self.services.project_grants,
            projects=projects.store,file_admission=self.services.file_admission)
        if existing is None:
            if configured is not None or adapter is not None:
                raise MigrationDenied('project_migration_host_changed')
            engine=ProjectTaskContinuations(**expected)
            return engine,ProjectMigrationArtifacts(authority,engine)
        if (type(existing) is not ProjectTaskContinuations or configured is not existing
                or type(adapter) is not ProjectMigrationArtifacts
                or adapter.authority is not authority or adapter.engine is not existing
                or adapter.token is not self.token or adapter.journal is not self.services.journal
                or existing.profile!=profile
                or any(getattr(existing,key,None) is not value for key,value in expected.items() if key!='profile')):
            raise MigrationDenied('project_migration_host_changed')
        return existing,adapter

    def configure_project_runtime(self,projects):
        """Late account binding; lack of files/project authority stays explicit deny."""
        from .file_authority import ArtifactSourceAuthorities
        from .project_artifacts import ProjectArtifactAuthority,ProjectFileBridge
        from .project_sessions import ProjectTaskSessions
        from src.plugins.ai_chat.project_background import LifecycleAccessResolver,ProjectTaskDispatcher
        if not projects.ready or projects.closed:raise MigrationDenied('project_runtime_not_ready')
        files=self._files
        if files is None:return False
        imports=self.domains.get('file')
        if imports is not None:
            if imports.projects not in (None,projects.store):
                raise MigrationDenied('migration_file_project_host_changed')
            imports.projects=projects.store
        authority=files.config.source_authority
        if isinstance(authority,ArtifactSourceAuthorities):authority=authority.resolve('project')
        if authority is None:return False
        if type(authority) is not ProjectArtifactAuthority or authority.token is not self.token:
            raise MigrationDenied('project_file_authority_changed')
        app=self.runtime.application
        if app is None or app.project_runtime is not projects:
            raise MigrationDenied('project_runtime_binding_conflict')
        engine,migration_artifacts=self._project_engine(projects,authority)
        if self.project_sessions is not None:
            if (self.project_sessions.projects is not projects or self.project_sessions.authority is not authority
                    or self.project_resolver.migration_authority is not engine):
                raise MigrationDenied('project_runtime_binding_conflict')
            return True
        # Runtime publishes files immediately after this factory returns. The
        # constructor must see the same exact ready object, never a substitute.
        if getattr(self.runtime,'file_service',None) is not files:return False
        session=ProjectTaskSessions(self.runtime,self.token,app,authority=authority)
        resolver=LifecycleAccessResolver(transaction=self.runtime.deliveries.transaction,
            token_for_account=lambda key:self.token if key==self.token.account_key and self._attached else None,
            admission=self.admission,route_guard=authority.route_guard,
            project_scope_guard=authority.project_scope_guard,clock=self.runtime.store.clock,
            migration_authority=engine)
        dispatch=ProjectTaskDispatcher(self.runtime.jobs)
        bridge=ProjectFileBridge(files=files,projects=projects,authority=authority)
        # Publish the late binding together only after all constructors succeed.
        # Existing native providers remain separate, with real native sources.
        if engine is not None:
            authority.migration_projects=engine
            authority.migration_artifacts=migration_artifacts
            self.domains['project_task']=engine
            self._refresh_plan_sources()
        self.project_resolver,self.project_dispatch,self.project_bridge=resolver,dispatch,bridge
        self.project_sessions=session
        self.runtime.project_service=session
        current=getattr(self.runtime,'subagent_service',None)
        if current is not None:current.projects=session
        return True

    async def project_context(self,inbound,platform,*,expected_execution_kind="chat"):
        """Issue the current foreground provider from its ORIGINAL admitted epoch."""
        from . import turn_source as execution
        from .turn_platform import ExecutionBinding
        from src.plugins.ai_chat.project_authorization import ForegroundProjectProvider
        if expected_execution_kind not in {"chat", "command"}:
            raise MigrationDenied("project_source_kind_invalid")
        projects=self.project_sessions
        if (projects is None or not projects.projects.ready or projects.projects.closed
                or self._files is None or self._files.closed):return None
        binding=platform.execution
        if (type(binding) is not ExecutionBinding or binding.token is not self.token
                or binding.api is not execution or binding.admission is not self.admission
                or binding.handle.endpoint != inbound.endpoint or binding.handle.actor != inbound.actor):
            raise MigrationDenied('project_foreground_source_changed')
        def issue():
            with self.runtime.deliveries.transaction() as cursor:
                current=binding.current(cursor,int(self.runtime.store.clock()))
                if current['state']!='executing' or current['execution_kind']!=expected_execution_kind:
                    raise MigrationDenied('project_foreground_not_executing')
                source=cursor.execute('SELECT logical_route_epoch FROM qq_migration_native_sources WHERE inbox_id=?',
                    (binding.handle.inbox_id,)).fetchone()
                if source is None:raise MigrationDenied('project_foreground_source_unavailable')
                authority=projects.authority
                from .project_commands import CommandProjectProvider
                factory=CommandProjectProvider if expected_execution_kind=="command" else ForegroundProjectProvider
                options={"resource_origins":self.services.project_resource_origins} if expected_execution_kind=="command" else {}
                return factory(token=self.token,handle=binding.handle,job=binding.job,
                    admission=self.admission,route_epoch=source['logical_route_epoch'],
                    route_guard=authority.route_guard,project_scope_guard=authority.project_scope_guard,
                    clock=self.runtime.store.clock,
                    max_grant_ttl_seconds=self.services.project_grant_ttl_seconds,**options)
        return await self.runtime._calls(issue)

    async def maintain(self):
        """Bounded materialization into the existing routine/job store only."""
        now=int(self.runtime.store.clock())
        if now<self._next_maintenance:return
        self._next_maintenance=now+5
        await self._settle_verification_receipts(now)
        domain=self.domains.get('routine')
        if domain is None:return
        def materialize():
            with self.runtime.deliveries.transaction() as cursor:
                rows=cursor.execute('''SELECT i.item_id FROM migration_items i
                    JOIN qq_official_schedule_bindings b ON b.item_id=i.item_id
                    JOIN agent_routines a ON a.routine_id=b.target_routine_id
                    WHERE b.account_key=? AND i.state='active' AND i.schedule_json IS NOT NULL
                      AND a.enabled=TRUE AND a.next_due_at<=?
                    ORDER BY a.next_due_at,i.item_id LIMIT 50''',(self.token.account_key,now)).fetchall()
                for row in rows:
                    proof=self.services.journal.issue_item(cursor,item_id=row['item_id'],operation='activate',now=now)
                    domain.materialize_due(cursor,proof,now=now)
        await self.runtime._calls(materialize)

    async def _settle_verification_receipts(self, now):
        def candidates():
            with self.runtime.deliveries.transaction() as cursor:
                return cursor.execute('''SELECT DISTINCT ON (v.migration_id,v.kind)
                    v.migration_id,r.receipt_id FROM migration_verification_intents v
                    JOIN migration_journals j USING(migration_id)
                    JOIN migration_routes m USING(route_id)
                    JOIN deliveries d ON d.plan_id=v.plan_id
                    JOIN delivery_attempts a USING(delivery_id)
                    JOIN delivery_receipts r USING(attempt_id)
                    WHERE m.target_account_key=? AND m.phase='verifying'
                      AND j.verification_until>? AND v.state='bound'
                      AND d.status='committed' AND a.state='committed' AND r.applied_at IS NOT NULL
                    ORDER BY v.migration_id,v.kind,r.observed_at,r.receipt_id LIMIT 50''',
                    (self.token.account_key,now)).fetchall()
        rows=await self.runtime._calls(candidates)
        for row in rows:
            def settle():
                with self.runtime.deliveries.transaction() as cursor:
                    self.services.journal.record_verification(cursor,migration_id=row['migration_id'],
                        source=row['receipt_id'],now=int(self.runtime.store.clock()))
            try:
                await self.runtime._calls(settle)
            except MigrationDenied:
                # A concurrent rollback/expiry cannot turn a late fact into a
                # new effect or block unrelated routine maintenance.
                continue

    def _reader(self,cursor,*,endpoint,source,now):
        from . import turn_source as execution
        from .turn_platform import ExecutionBinding
        if (not isinstance(source,ExecutionBinding) or source.token is not self.token
                or source.api is not execution or source.admission is not self.admission
                or source.handle.endpoint != endpoint):return False
        row=source.current(cursor,now)
        return row['state']=='executing' and row['deadline_at']>now

    def public_history(self,inbound,platform):
        if platform.execution.token is not self.token or inbound.endpoint != platform.execution.handle.endpoint:
            raise MigrationDenied('history_reader_source_mismatch')
        return self.history.session(endpoint=inbound.endpoint,source=platform.execution,
            transaction=self.runtime.deliveries.transaction)

    async def request_verification(self,*,cookie,csrf,migration_id,kind,inbox_id,artifact_id=None,
                                   preview_digest=None):
        request=dict(migration_id=migration_id,kind=kind,inbox_id=inbox_id,artifact_id=artifact_id)
        def prepare():
            # The real control authority rechecks the cookie+CSRF and current
            # account version again while binding the exact transaction.
            with self.runtime.deliveries.transaction() as cursor:
                state,_=self.services.journal._load(cursor,migration_id)
                preview=state['preview_digest']
                if preview_digest is not None and preview_digest!=preview:
                    raise MigrationDenied('migration_review_stale')
            grant=self.services.control.issue(cookie=cookie,csrf=csrf,action='verification_send',request=request,preview_digest=preview)
            with self.runtime.deliveries.transaction() as cursor:
                return self.services.ports.verifier.prepare(cursor,grant=grant,request=request,
                    outbox=self.runtime.outbox,token=self.token,now=int(self.runtime.store.clock()))
        result=await self.runtime._calls(prepare)
        sender=getattr(self.runtime,'sender',None)
        if sender is not None:sender.wake()
        return result

    async def close(self):
        if getattr(self.runtime,'project_service',None) is self.project_sessions:
            self.runtime.project_service=None
        subagent=getattr(self.runtime,'subagent_service',None)
        if subagent is not None and subagent.projects is self.project_sessions:subagent.projects=None
        self.project_sessions=None
        if self._attached:
            if self.runtime.store.admitted is self.admission:
                self.runtime.store.admitted=self.admission.base
                self.runtime.outbox.admission=self.admission.base
            if self.runtime.store.source_authority is self.native:self.runtime.store.source_authority=None
            if self.runtime.outbox.source_guard is self.guard:self.runtime.outbox.source_guard=None
            self._attached=False
        if self.services.routes.tokens.get(self.token.account_key) is self.token:
            del self.services.routes.tokens[self.token.account_key]
        if self.services.official.get(self.token.account_key) is self:
            del self.services.official[self.token.account_key]
