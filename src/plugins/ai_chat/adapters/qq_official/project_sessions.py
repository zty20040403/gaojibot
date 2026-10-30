"""Official project steps use the existing Coordinator and pinned baseline.

Only the host installs this service. Workspace creation and capture use M6;
accepted project updates still use the shared publication/materialization path.
"""
import asyncio
from contextvars import ContextVar
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

from src.plugins.ai_chat.agent.control import assert_job_owned, LeaseLost
from src.plugins.ai_chat.agent.execution import active_agent_step
from src.plugins.ai_chat.application.owned_sandbox import OwnedSandboxExecutor
from src.plugins.ai_chat.subagents import AgentExecutionHooks
from src.plugins.ai_chat.platform_types import BackgroundToolSource
from src.plugins.ai_chat.project_background import BackgroundProjectProvider
from src.plugins.ai_chat.project_task_inputs import inputs_for_task, official_project_step_owner
from src.plugins.ai_chat.project_tasks import ProjectTaskStore
from src.plugins.ai_chat.project_publication import ProjectPublications
from src.plugins.ai_chat.project_materialization import ProjectMaterializations
from src.plugins.ai_chat.tools import neutral_registry_for
from src.plugins.ai_chat.tools.state import ToolContext, TurnState
from src.plugins.ai_chat.workers.durable_jobs import JobDeferred
from .file_artifacts import BLOB_LOCK, current_source
from .file_generation import GenerationBindingAdapter
from .file_runtime import FileGenerationSession
from .project_authority import Operation
from .subagent_runtime import TaskToolSession, TEXT_TOOLS, FILE_TOOLS


_cleanup = ContextVar('official_project_workspace_cleanup', default=False)


class _StepBinding(GenerationBindingAdapter):
    """One host-selected running step; model arguments cannot select an owner."""
    def __init__(self, session, step, conversation, validate_step, provider):
        original = session.binding
        super().__init__(session.source, original.store, original.calls,
                         account_limit=original.account_limit, actor_limit=original.actor_limit)
        self.step, self.conversation, self.validate_step = step, conversation, validate_step
        self.provider = provider
        self.base_owner,self.owner = official_project_step_owner(
            self.endpoint,self.actor,session.source.task_id,step)
        if self.source_key != f'project-task:{session.source.task_id}:1':
            raise LeaseLost('project_step_source_changed')

    async def assert_current(self):
        if _cleanup.get():
            return await super().assert_current()
        if active_agent_step.get() != self.step:
            raise LeaseLost('project_step_source_changed')
        def check():
            # One short transaction checks both the issued file source and the
            # real running step. The previous composition opened several new
            # transactions and recursively repeated the outer JobFence across
            # every await. All original source/job/grant/baseline checks remain;
            # no authority or cursor survives into a physical guest call.
            with self.store.transaction() as cursor:
                with self.provider._read_composition(cursor):
                    current_source(cursor,self.source,self.store._now())
                    if self.validate_step(cursor,self.step) != self.conversation:
                        raise LeaseLost('project_step_source_changed')
        await self.calls(check)
        if active_agent_step.get() != self.step:
            raise LeaseLost('project_step_source_changed')


class _StepExecutor(OwnedSandboxExecutor):
    """Registry-compatible dispatcher; it never executes on the shared owner."""
    def __init__(self, workspaces):
        self.__dict__.update(workspaces.base.executor.__dict__)
        self.workspaces = workspaces

    async def execute(self, name, arguments):
        session = await self.workspaces.for_step()
        return await session.executor.execute(name, arguments)


class _StepManager:
    def __init__(self, workspaces):
        self.workspaces = workspaces
        self.backend = workspaces.base.runtime.manager.backend

    async def _call(self, name, owner, sid=None, *args):
        session = await self.workspaces.for_step()
        executor = session.executor
        if owner != executor.owner:
            raise LeaseLost('project_step_owner_changed')
        if sid is not None:
            await executor._owned(sid)
        operation = getattr(session.runtime.manager, name)
        values = (owner,) if sid is None else (owner, sid, *args)
        return await executor._call(operation, *values)

    async def list(self, owner):
        return await self._call('list', owner)

    async def start_owned(self, owner, sid):
        return await self._call('start_owned', owner, sid)

    async def install_readonly_file(self, owner, sid, path, content):
        session = await self.workspaces.for_step()
        try:
            return await self._call('install_readonly_file', owner, sid, path, content)
        except BaseException:
            session.executor._uncertain = True
            session.executor.generation_enabled = False
            raise


class _ProjectWorkspaces:
    def __init__(self, owner, job, task, base, inputs, provider):
        self.service, self.job, self.task, self.base, self.inputs = owner, job, task, base, inputs
        if (type(provider) is not BackgroundProjectProvider or provider.kind != 'task'
                or provider.binding_id != task.task_id or provider.job != job
                or provider.endpoint != task.endpoint or provider.actor != task.requester_actor
                or not owner.authority._matches_provider(base.source,provider)):
            raise LeaseLost('project_step_provider_changed')
        self.provider = provider
        self.sessions, self._locks = {}, {}
        self.manager = _StepManager(self)
        self._closed = False

    def _require_step(self, cursor, step):
        prefix = f'task#{self.task.task_id}/'
        if not isinstance(step,str) or not step.startswith(prefix) or not step[len(prefix):]:
            raise LeaseLost('project_step_source_unavailable')
        access=self.provider.issue_for_project(cursor,self.task.project_id,operation=Operation.EXECUTE)
        # This host-only cursor helper contains exactly the shared public
        # require_step body; _provider has checked the actual original job.
        return self.inputs.store._require_step(cursor,access,self.task.task_id,step[len(prefix):])

    async def require_step(self, step):
        prefix = f'task#{self.task.task_id}/'
        if not isinstance(step, str) or not step.startswith(prefix) or not step[len(prefix):]:
            raise LeaseLost('project_step_source_unavailable')
        assert_job_owned()
        access = await self.inputs.resolve_access()
        value = await self.service.runtime._calls(self.inputs.store.require_step, access,
                                                  self.task.task_id, step[len(prefix):])
        assert_job_owned()
        return value

    async def for_step(self):
        if self._closed:
            raise LeaseLost('project_workspace_closed')
        step = active_agent_step.get()
        if step in self.sessions:
            return self.sessions[step]  # Its executor fences every physical operation.
        conversation = await self.require_step(step)
        lock = self._locks.setdefault(step, asyncio.Lock())
        async with lock:
            if step not in self.sessions:
                session = FileGenerationSession(self.base.runtime, self.base.source)
                binding = _StepBinding(session, step, conversation, self._require_step, self.provider)
                # Configure the trusted fixed owner before any manager operation.
                # The capacity adapter and physical executor share this exact owner.
                session.binding = session.executor.binding = binding
                session.executor.base_owner = binding.base_owner
                session.executor.owner = binding.owner
                session.workspaces._captured = self.base.workspaces._captured
                await session.executor.readiness()
                self.sessions[step] = session
            return self.sessions[step]

    @property
    def executor(self):
        session = self.sessions.get(active_agent_step.get())
        if session is None:
            raise LeaseLost('project_step_workspace_unavailable')
        return session.executor

    async def capture(self, task_id, artifacts):
        session = await self.for_step()
        return await session.workspaces.capture(task_id, artifacts)

    async def import_artifact(self, task_id, upstream, arguments):
        session = await self.for_step()
        return await session.workspaces.import_artifact(task_id, upstream, arguments)

    async def validate(self, task_id, artifact):
        # Format validation is a separate owned guest. Independent acceptance
        # still runs as its own real Coordinator run and uses for_step().
        return await self.base.workspaces.validate(task_id, artifact)

    async def quiesce_for_validation(self, task_id, completed):
        self.base.workspaces._task(task_id)
        token = _cleanup.set(True)
        try:
            for step,session in self.sessions.items():
                key=step.split('/',1)[1]
                if key not in completed:
                    raise LeaseLost('project_producer_not_quiescent')
                # Every artifact has already been copied to host CAS before a
                # run becomes terminal. Unknown exports keep _uncertain set,
                # so close refuses to destroy or release their capacity.
                def terminal():
                    with self.service.files.transaction() as cursor:
                        self.service._provider(cursor,self.job,self.task)
                        row=cursor.execute("SELECT status FROM subagent_runs WHERE task_id=? AND step_key=?",
                                           (task_id,key)).fetchone()
                        return row is not None and row['status'] in {'succeeded','partial','failed','skipped'}
                if not await self.service.runtime._calls(terminal):
                    raise LeaseLost('project_producer_not_quiescent')
                await session.close()
        finally:
            _cleanup.reset(token)

    async def deliver_request(self, request):
        return await self.base.workspaces.deliver_request(request)

    async def finalize_task(self, task_id, runs, **kwargs):
        self.base.workspaces._task(task_id)
        # The dispatcher still owns the original job and must publish the
        # accepted bytes before final close. Do not make cleanup a send grant.
        return {'retained': sum(len(s.executor.retained_resources) for s in self.sessions.values())}

    async def close(self):
        self._closed = True
        token = _cleanup.set(True)
        try:
            # If the task completed, renew only the trusted read/finalize source
            # for destruction. No tools are exposed on these closed sessions.
            try:
                source = await self.service.cleanup_source(self.job, self.task)
            except (Exception, LeaseLost):
                source = None
            for session in (*self.sessions.values(), self.base):
                if source is not None:
                    session.source = session.binding.source = source
                await session.close()
        finally:
            _cleanup.reset(token)


class ProjectTaskSessions:
    def __init__(self, runtime, token, app, *, authority):
        self.runtime,self.token,self.app,self.authority=runtime,token,app,authority
        self.projects=app.project_runtime
        self.files=runtime.file_service
        from .file_authority import ArtifactSourceAuthorities
        configured=self.files.config.source_authority if self.files is not None else None
        if isinstance(configured,ArtifactSourceAuthorities):
            configured=configured.resolve('project')
        if (self.projects is None or self.files is None or configured is not authority
                or authority.token is not token):
            raise ValueError('official_project_services_unavailable')
        self.publications=ProjectPublications(ProjectTaskStore(self.projects.store,app.subagent_store))
        self.materializations=ProjectMaterializations(self.publications,self.projects.lifecycle)

    def _provider(self,cursor,job,task,*,purpose='execute'):
        provider,access=BackgroundProjectProvider.load_for_project(cursor,project_id=task.project_id,
            token=self.token,job=job,kind='task',binding_id=task.task_id,
            admission=self.runtime.store.admitted,route_guard=self.authority.route_guard,
            project_scope_guard=self.authority.project_scope_guard,clock=self.runtime.store.clock,purpose=purpose,
            migration_authority=getattr(self.authority,'migration_projects',None))
        if task.endpoint!=provider.endpoint or task.requester_actor!=provider.actor:
            raise LeaseLost('project_task_identity_changed')
        return provider,access

    def assert_current(self,cursor,*,token,binding,task,job,now):
        if (token is not self.token or job is None or binding['job_id'] != job.job_id
                or binding['tool_profile'] != 'official-projects-v1' or task['project_id'] is None):
            raise LeaseLost('project_task_identity_changed')
        if task['status'] in {'failed','cancelled'}:
            # Reporting a terminal result needs read access, not a running task.
            allowed=cursor.execute("""SELECT 1 FROM qq_official_project_task_authority a
                JOIN qq_official_project_grants g ON g.grant_id=a.grant_id
                WHERE a.task_id=? AND a.project_id=? AND a.grant_revision=g.revision
                AND g.state='active' AND g.expires_at>? AND 'project.read'=ANY(g.capabilities)""",
                (task['task_id'],task['project_id'],now)).fetchone()
            if allowed is None:
                raise LeaseLost('project_result_access_revoked')
            return True
        record=self.app.subagent_store._task_row(task)
        self._provider(cursor,job,record,purpose='finalize' if task['status'] in {'completed','partial'} else 'execute')
        return True

    async def prepare(self,job,task,files_enabled):
        if not files_enabled or task.project_id is None:
            raise JobDeferred('official_project_context_unavailable',30)
        def issue():
            with self.files.transaction() as cursor:
                provider,access=self._provider(cursor,job,task)
                source=self.authority.issue(cursor,access=access,project_id=task.project_id)
                return provider,source
        provider,source=await self.runtime._calls(issue)
        session=await self.files.generation.bound_session(source)
        if session is None:
            raise JobDeferred('official_project_files_unavailable',30)
        try:
            from .subagent_composition import TaskPlatform
            platform=TaskPlatform(task.endpoint)
            platform.project_provider=provider
            context=ToolContext(SimpleNamespace(context=self.app),event=None,capabilities=frozenset({'self','context'}),
                background_source=BackgroundToolSource(task.endpoint,task.requester_actor,'project',f'task:{task.task_id}',1),
                platform=platform)
            inputs=await inputs_for_task(context,task.task_id)
            workspaces=_ProjectWorkspaces(self,job,task,session,inputs,provider)
            context=replace(context,capabilities=context.capabilities|{'sandbox'},
                            agent_executor=_StepExecutor(workspaces))
            hooks=AgentExecutionHooks(workspaces=workspaces,project_inputs=inputs,
                                      artifact_delivery=workspaces.deliver_request)
            turn=TurnState(context,user_text=task.objective)
            registry=await self.runtime._calls(neutral_registry_for,context)
            definitions=await self.runtime._calls(registry.definitions,context)
            allowed=TEXT_TOOLS|FILE_TOOLS
            definitions=tuple(t for t in definitions if t['function']['name'] in allowed)
            async def execute(name,args):
                assert_job_owned()
                if name not in allowed:
                    return '{"ok":false,"error":"tool_unavailable"}'
                return await registry.execute(turn,name,args)
            return TaskToolSession(definitions,execute,hooks=hooks,close=workspaces.close,
                files_enabled=True,project_enabled=True)
        except BaseException:
            await session.close()
            raise

    async def cleanup_source(self,job,task):
        def issue():
            with self.files.transaction() as cursor:
                provider,access=self._provider(cursor,job,task,purpose='finalize')
                return self.authority.issue(cursor,access=access,project_id=task.project_id)
        return await self.runtime._calls(issue)

    async def finalize(self,job,task):
        """Before the original task job ends, publish only independently accepted bytes."""
        acceptance=task.result.get('validation',{}).get('acceptance',{})
        if task.status not in {'completed','partial'} or acceptance.get('status')!='passed':
            return False
        def access():
            with self.files.transaction() as cursor:
                return self._provider(cursor,job,task,purpose='finalize')[1]
        current=await self.runtime._calls(access)
        output=await self.runtime._calls(self.publications.output,current,task.task_id)
        if 'publication' not in output:
            artifact=output['artifact']
            def read():
                with self.files.transaction() as cursor:
                    cursor.execute('SELECT pg_advisory_xact_lock(?)',(BLOB_LOCK,))
                    self._provider(cursor,job,task,purpose='finalize')
                    rows=cursor.execute('''SELECT a.metadata_json FROM subagent_artifacts a
                        JOIN subagent_runs r ON r.run_id=a.run_id AND r.task_id=a.task_id
                        WHERE a.task_id=? AND r.status='succeeded' ''',(task.task_id,)).fetchall()
                    exact=[json.loads(r['metadata_json']) for r in rows
                           if json.loads(r['metadata_json']).get('snapshot')==artifact['snapshot']]
                    if len(exact)!=1 or any(exact[0].get(k)!=artifact.get(k) for k in ('snapshot','size','name')):
                        raise LeaseLost('project_task_capture_changed')
                    size=artifact['size']
                    if type(size) is not int or not 0<size<=self.files.artifacts.max_bytes:
                        raise LeaseLost('project_task_capture_unavailable')
                    body=self.files.artifacts.blobs.read(artifact['snapshot'],max_bytes=size+1)
                    if len(body)!=size or hashlib.sha256(body).hexdigest()!=artifact['snapshot']:
                        raise LeaseLost('project_task_capture_changed')
                    self._provider(cursor,job,task,purpose='finalize')
                    return body
            content=await self.runtime._calls(read)
            current=await self.runtime._calls(access)
            await self.runtime._calls(self.publications.publish,current,task.task_id,artifact['snapshot'],content)
        current=await self.runtime._calls(access)
        project=await self.runtime._calls(self.projects.store.get,current,task.project_id)
        backend=self.projects.backends[project['backend']]
        await self.runtime._calls(self.materializations.request,current,task.task_id,preflight=backend.preflight)
        return True
