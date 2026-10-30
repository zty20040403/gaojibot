"""Official host wiring: existing owner bindings and projects, no migration gate."""
from contextlib import nullcontext
import time

from .migration_types import MigrationDenied
from .project_resource_origins import ProjectResourceOrigins


class BoundRoutes:
    def assert_active_route(self, cursor, *, canonical_scope, endpoint, route_epoch=0, operation='read'):
        scope = canonical_scope.key if hasattr(canonical_scope, 'key') else canonical_scope
        row = cursor.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?', (endpoint.key,)).fetchone()
        if row is None or row['binding_state'] == 'revoked' or row['membership_state'] == 'absent':
            raise MigrationDenied('conversation_unavailable')
        if scope not in {endpoint.scope.key, row['canonical_scope_key']}:
            raise MigrationDenied('project_conversation_mismatch')
        return True


class SourceAdmission:
    def __init__(self, base): self.base = base

    def __call__(self, cursor, endpoint, actor):
        return self.base(cursor, endpoint, actor)

    def require_execution(self, cursor, execution_id, *, endpoint, actor, operation):
        row = cursor.execute('SELECT * FROM qq_official_turn_executions WHERE execution_id=? AND endpoint_key=? AND requester_actor_key=?',
            (execution_id, endpoint.key, actor.key)).fetchone()
        if row is None or not self(cursor, endpoint, actor):
            raise MigrationDenied('turn_source_unavailable')
        return {**row, 'logical_route_epoch': row['route_epoch']}

    def require_inbox(self, cursor, inbox_id, *, endpoint, actor, operation):
        row = cursor.execute('SELECT * FROM qq_official_inbound_events WHERE inbox_id=? AND endpoint_key=?', (inbox_id, endpoint.key)).fetchone()
        if row is None or not self(cursor, endpoint, actor):
            raise MigrationDenied('message_source_unavailable')
        return {**row, 'logical_route_epoch': row['route_epoch']}


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



class HostServices:
    native_only = True
    baselines = {}

    def __init__(self, *, database, transaction, database_calls, project_grant_ttl_seconds=86400, clock=time.time, **legacy):
        self.database, self.transaction, self.database_calls = database, transaction, database_calls
        self.project_grant_ttl_seconds, self.clock = project_grant_ttl_seconds, clock
        self.routes = BoundRoutes()
        self.project_resource_origins = ProjectResourceOrigins(routes=self.routes)
        self.project_grants = self.project_resource_origins
        self.project_router = None
        self.official = {}
        self.file_admission = None

    async def start_native(self):
        pass

    def admin_service(self):
        return None

    def official_factory(self, runtime, token):
        session = OfficialHostSession(self, runtime, token)
        self.official[token.account_key] = session
        return session

    def job_context(self, *args, **kwargs):
        return nullcontext()

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


    async def close(self):
        for session in tuple(self.official.values()):
            await session.close()


class OfficialHostSession:
    def __init__(self, services, runtime, token):
        self.services, self.runtime, self.token = services, runtime, token
        self.admission = SourceAdmission(runtime.store.admitted)
        self._attached = False
        self._files = self.project_sessions = self.project_resolver = self.project_dispatch = self.project_bridge = None
        self.domains = {}
        self.task_sources = None

    def attach(self):
        self.runtime.store.admitted = self.runtime.outbox.admission = self.admission
        self._attached = True

    def file_configuration(self, config):
        return config

    def attach_file_services(self, files):
        self._files = files

    def configure_project_runtime(self, projects):
        from .project_artifacts import ProjectFileBridge
        from .project_sessions import ProjectTaskSessions
        from ...project_background import LifecycleAccessResolver, ProjectTaskDispatcher
        if self._files is None or self.project_sessions is not None:
            return self.project_sessions is not None
        authority = self._files.config.source_authority
        self.project_sessions = ProjectTaskSessions(self.runtime, self.token, self.runtime.application, authority=authority)
        self.project_resolver = LifecycleAccessResolver(transaction=self.runtime.deliveries.transaction,
            token_for_account=lambda key: self.token if key == self.token.account_key and self._attached else None,
            admission=self.admission, route_guard=authority.route_guard,
            project_scope_guard=authority.project_scope_guard, clock=self.runtime.store.clock)
        self.project_dispatch = ProjectTaskDispatcher(self.runtime.jobs)
        self.project_bridge = ProjectFileBridge(files=self._files, projects=projects, authority=authority)
        self.runtime.project_service = self.project_sessions
        if self.runtime.subagent_service is not None:
            self.runtime.subagent_service.projects = self.project_sessions
        return True

    async def project_context(self, inbound, platform, *, expected_execution_kind='chat'):
        from ...project_authorization import ForegroundProjectProvider
        from .project_commands import CommandProjectProvider
        if self.project_sessions is None:
            return None
        binding = platform.execution
        authority = self.project_sessions.authority
        factory = CommandProjectProvider if expected_execution_kind == 'command' else ForegroundProjectProvider
        options = {'resource_origins': self.services.project_resource_origins} if expected_execution_kind == 'command' else {}
        return factory(token=self.token, handle=binding.handle, job=binding.job,
            admission=self.admission, route_epoch=binding.handle.route_epoch,
            route_guard=authority.route_guard, project_scope_guard=authority.project_scope_guard,
            clock=self.runtime.store.clock, max_grant_ttl_seconds=self.services.project_grant_ttl_seconds, **options)

    def public_history(self, inbound, platform):
        # Both old and new messages use the bound canonical conversation.
        return None

    async def maintain(self):
        pass

    async def close(self):
        if self._attached:
            self.runtime.store.admitted = self.runtime.outbox.admission = self.admission.base
            self._attached = False
        self.runtime.project_service = None
        self.services.official.pop(self.token.account_key, None)
