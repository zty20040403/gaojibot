"""Application publication and recovery over the private project browser RPC."""

from src.sandbox_runtime.client import SandboxClient

from .project_browser_store import ProjectBrowserStore
from .project_executor import ProjectBackendUnavailable, _store
from .projects import ProjectConflict
from . import project_browser_drain


class ProjectBrowserController:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = ProjectBrowserStore(runtime.io)

    async def _fresh(self, access, grant, drain_job=None):
        project = await _store(self.runtime.store.get, access, grant.project_id)
        current = await self.runtime._execution_access(project, access.principal_id)
        if current.scope != access.scope or current.principal_id != access.principal_id:
            raise PermissionError("Browser actor or conversation identity changed")
        if drain_job is not None:
            project, _ = await _store(self.runtime.lifecycle.context, drain_job, access=current)
            await _store(project_browser_drain.grant_for, self.runtime.lifecycle, drain_job, current)
        return current, project

    def _client(self, project):
        if project["backend"] == "vm":
            from .project_vm_executor import VmProjectBackend
            backend = self.runtime.backends.get("vm")
            if isinstance(backend, VmProjectBackend):
                return backend.runtime.browser
        client = self.runtime.app.sandbox_manager
        if project["backend"] != "oci" or not isinstance(client, SandboxClient):
            raise ProjectBackendUnavailable("This backend has no isolated project browser adapter")
        return client

    async def open(self, access, grant):
        access, project = await self._fresh(access, grant)
        client = self._client(project)
        session = await _store(self.store.session, access, grant)
        if session["operation_state"] == "reserved":
            await self.runtime.backends[project["backend"]].io_evidence(project)
            access, _ = await self._fresh(access, grant)
            await _store(self.runtime.io.dispatch, access, grant)
        elif session["operation_state"] not in {"dispatched", "needs_attention"}:
            raise ProjectConflict("Browser session has already finished")
        if session["session_id"] is None:
            access, _ = await self._fresh(access, grant)
            target = await _store(self.store.launch_request, access, grant)
            record = await client.project_browser("open", **target)
            await _store(self.store.opened, grant, record)
        access, _ = await self._fresh(access, grant)
        return await _store(self.store.session, access, grant)

    async def execute(self, access, grant, request_key, method, args):
        access, project = await self._fresh(access, grant)
        client = self._client(project)
        prior = await _store(self.store.find, access, request_key, method, args)
        if prior is None:
            session = await _store(self.store.session, access, grant)
            record = await client.project_browser("status", **session["target"])
            await _store(self.store.require_ready, access, grant, record)
        call = await _store(self.store.admit, access, grant, request_key, method, args)
        row = await _store(self.store.receipt, access, call)
        if row["state"] == "reserved":
            access, _ = await self._fresh(access, grant)
            target = await _store(self.store.dispatch, access, call, method, args)
            # The persisted intent precedes the effect. If this await is lost,
            # recovery only receives the old sequence; it does not click again.
            response = await client.project_browser("execute", **target)
            if response is not None:
                await _store(self.store.observe, call, response)
        return await self.recover(access, call)

    async def recover(self, access, call, *, drain_job=None):
        access, project = await self._fresh(access, call.grant, drain_job)
        client = self._client(project)
        options = {"drain_job": drain_job}
        row = await _store(self.store.receipt, access, call, **options)
        if row["state"] in {"reserved", "aborted"}:
            raise ProjectConflict("Browser action has no dispatched result")
        if row["response"] is None:
            session = await _store(self.store.session, access, call.grant, **options)
            response = await client.project_browser("receive", **session["target"], sequence=call.sequence)
            if response is None:
                raise ProjectConflict("Browser result remains unknown; action was not repeated")
            await _store(self.store.observe, call, response, **options)
            row = await _store(self.store.receipt, access, call, **options)
        if row["state"] == "needs_attention":
            raise ProjectConflict("Browser reported an unknown outcome; action was not repeated")
        if row["state"] == "received":
            access, _ = await self._fresh(access, call.grant, drain_job)
            target = await _store(self.store.ack_request, access, call, **options)
            record = await client.project_browser("ack", **target)
            await _store(self.store.acknowledged, call, record, **options)
        access, _ = await self._fresh(access, call.grant, drain_job)
        return (await _store(self.store.receipt, access, call, **options))["response"]["result"]

    async def drain(self, access, grant, *, drain_job=None):
        access, project = await self._fresh(access, grant, drain_job)
        client = self._client(project)
        options = {"drain_job": drain_job}
        io = await _store(self.runtime.io.receipt, access, grant, **options)
        if io["state"] in {"completed", "aborted"}:
            return io["result"]
        if io["state"] == "reserved":
            await _store(self.runtime.io.abort, grant, **options)
            return None
        session = await _store(self.store.session, access, grant, **options)
        if session["session_id"] is None:
            target = dict(session["target"])
            target.pop("session_id")
            record = await client.project_browser("status", **target, launch_id=session["launch_id"])
            if record is None:
                raise ProjectConflict("Browser launch outcome is unknown; no replacement was started")
            await _store(self.store.opened, grant, record, **options)
            session = await _store(self.store.session, access, grant, **options)
        pending = await _store(self.store.pending, access, grant, **options)
        if pending is not None:
            row = await _store(self.store.receipt, access, pending, **options)
            if row["state"] == "reserved":
                await _store(self.store.abandon_reserved, access, pending, **options)
            else:
                await self.recover(access, pending, **options)
        access, project = await self._fresh(access, grant, drain_job)
        target = await _store(self.store.start_drain, access, grant, **options)
        record = await client.project_browser("drain", **target, cancel_pending=session["worker_id"] is None)
        await _store(self.store.drained, grant, record, **options)
        result = {"ok": True, "browser_session_closed": True}
        await _store(self.runtime.io.observe_result, grant, result, **options)
        evidence = await self.runtime.backends[project["backend"]].io_evidence(project)
        await self._fresh(access, grant, drain_job)
        await _store(self.runtime.io.complete, grant, result, evidence, **options)
        return result

    async def drain_lifecycle(self, job, access):
        grant = await _store(project_browser_drain.grant_for, self.runtime.lifecycle, job, access)
        if grant is None:
            return
        await self.drain(access, grant, drain_job=job)
        access, _ = await self._fresh(access, grant, job)
        await _store(project_browser_drain.finish, self.runtime.lifecycle, job, access)
