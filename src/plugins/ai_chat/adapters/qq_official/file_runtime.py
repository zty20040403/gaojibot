"""Bind foreground and durable tasks to reusable conversation workspaces."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import logging
import re
import traceback

from ...agent.artifact_delivery import ArtifactDeliveryRequest, ArtifactDeliveryView
from .image_formats import artifact_filename as canonical_artifact_filename
from ...agent.control import LeaseLost
from ...agent.workspaces import ArtifactCaptureError
from ...application.owned_sandbox import (
    ArtifactAvailability, ArtifactRegistration, ExportedArtifact, GenerationError,
    GenerationLimits, OwnedSandboxExecutor, workspace_path,
)
from ...platform_types import AccountKey
from ...subagents import AgentExecutionHooks
from . import execution, subagent_runtime
from .file_artifacts import BLOB_LOCK, current_source, foreground_source, task_source
from .file_generation import GenerationBindingAdapter

GENERATION_TOOLS = frozenset({"sandbox_create", "sandbox_list", "sandbox_destroy", "sandbox_exec",
    "sandbox_write_file", "sandbox_read_file", "send_file_from_sandbox", "send_file"})


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


class OfficialFileRuntime:
    """Host factory; route availability must come from configured real routes.

    ``route_availability(binding)`` is async and receives a
    GenerationBindingAdapter, whose ``source`` is the issued SourceBinding.
    No callback means no generation, even with ``enabled=True``.
    """
    def __init__(self, *, account, token, artifact_store, database_calls, manager,
                 admission, route_availability=None, enabled=False,
                 limits=GenerationLimits(), account_limit=16, actor_limit=4,
                 pending_seconds=86400, source_factory=None):
        if (not isinstance(account, AccountKey) or account.platform != "qq-official"
                or token.account_key != account.key or type(enabled) is not bool
                or type(pending_seconds) is not int or not 1 <= pending_seconds <= 86400
                or not isinstance(limits, GenerationLimits)):
            raise GenerationError("invalid_file_runtime")
        self.account, self.token = account, token
        self.store, self.calls, self.manager = artifact_store, database_calls, manager
        self.admission, self.route_availability, self.enabled = admission, route_availability, enabled
        self.limits, self.account_limit, self.actor_limit = limits, account_limit, actor_limit
        self.pending_seconds = pending_seconds
        self.source_factory=source_factory

    async def foreground(self, inbound, platform):
        if not self.enabled or self.route_availability is None or self.manager is None:
            return None
        life = getattr(platform, "execution", None)
        if (life is None or life.token is not self.token or platform.account != self.account
                or inbound.endpoint.account != self.account
                or (life.handle.endpoint, life.handle.actor, life.handle.source_native_message_id,
                    life.handle.scope_key) != (inbound.endpoint, inbound.actor,
                    inbound.native_message_id, inbound.scope.key)):
            raise LeaseLost("file_runtime_source_mismatch")
        def issue():
            with self.store.transaction() as cursor:
                now = self.store._now()
                row = execution.assert_execution_current(cursor, token=self.token, handle=life.handle,
                    job=life.job, admission=self.admission, now=now)
                return foreground_source(cursor, token=self.token, handle=life.handle, job=life.job,
                    admission=self.admission, now=now,
                    pending_deadline_at=row["created_at"] + self.pending_seconds)
        return await self._session(await self.calls(issue))

    async def subagent(self, job, task):
        if not self.enabled or self.route_availability is None or self.manager is None:
            return None
        def issue():
            with self.store.transaction() as cursor:
                now = self.store._now()
                if task.migration_item_id is not None:
                    if self.source_factory is None:
                        raise LeaseLost('migration_artifact_source_unavailable')
                    source=self.source_factory(cursor,task=task,job=job,now=now)
                    current_source(cursor,source,now)
                    if source.token is not self.token or (task.endpoint,task.requester_actor)!=(source.endpoint,source.actor):
                        raise LeaseLost('file_runtime_source_mismatch')
                    return source
                row, _ = subagent_runtime.assert_current(cursor, token=self.token, task_id=task.task_id,
                    job=job, admission=self.admission, now=now)
                source = task_source(cursor, token=self.token, task_id=task.task_id, job=job,
                    admission=self.admission, now=now,
                    pending_deadline_at=row["created_at"] + self.pending_seconds)
                if (task.endpoint, task.requester_actor) != (source.endpoint, source.actor):
                    raise LeaseLost("file_runtime_source_mismatch")
                return source
        return await self._session(await self.calls(issue))

    async def _session(self, source):
        session = FileGenerationSession(self, source)
        try:
            await session.executor.readiness()
        except GenerationError:
            return None
        return session

    async def bound_session(self, source):
        """Host-issued source extension; never accept a source from tool JSON."""
        if not self.enabled or self.route_availability is None or self.manager is None:
            return None
        def check():
            with self.store.transaction() as cursor:
                current_source(cursor,source,self.store._now())
                if source.token is not self.token or source.endpoint.account != self.account:
                    raise LeaseLost('file_runtime_source_mismatch')
        await self.calls(check)
        return await self._session(source)


class _Delivery:
    def __init__(self, session):
        self.session = session

    async def readiness(self, binding):
        session = self.session
        if binding is not session.binding or session.closed:
            raise GenerationError("file_generation_closed")
        try:
            available = await session.runtime.route_availability(binding)
        except Exception:
            session.executor.generation_enabled = False
            raise GenerationError("artifact_route_unavailable") from None
        if (not isinstance(available, ArtifactAvailability) or available.available is not True
                or available.route_kind not in {"native", "link"}
                or type(available.max_bytes) is not int or available.max_bytes <= 0):
            session.executor.generation_enabled = False
            return ArtifactAvailability(False, 0, "")
        return ArtifactAvailability(True, min(available.max_bytes, session.runtime.store.max_bytes),
                                    available.route_kind)

    async def register(self, binding, exported, *, logical_request_key, draft, draft_reason):
        session = self.session
        # ExportedArtifact is metadata, not an issuance capability on its own.
        if (binding is not session.binding or not isinstance(exported, ExportedArtifact)
                or session.executor._exports.get(id(exported)) is not exported
                or not re.fullmatch(r"generated:[a-f0-9]{64}", logical_request_key)):
            raise GenerationError("invalid_generated_provenance")
        cap = await session.executor.readiness()
        if exported.size > cap:
            raise GenerationError("artifact_route_unavailable")
        record = await session.runtime.calls(session.runtime.store.register, binding.source,
            content=exported.content, filename=canonical_artifact_filename(exported.filename, draft=draft),
            logical_request_key=_digest(["foreground", logical_request_key]),
            draft=draft, draft_reason=draft_reason)
        sender = getattr(session.runtime, 'send_artifact', None)
        if sender is None:
            return ArtifactRegistration(record.artifact_id, "stored")
        outcome = await sender(record.artifact_id)
        return ArtifactRegistration(record.artifact_id,
            'committed' if outcome.status == 'committed' else 'waiting', outcome.status == 'committed')


class FileGenerationSession:
    def __init__(self, runtime, source):
        self.runtime, self.source, self.closed = runtime, source, False
        self.binding = GenerationBindingAdapter(source, runtime.store, runtime.calls,
            account_limit=runtime.account_limit, actor_limit=runtime.actor_limit)
        self.executor = OwnedSandboxExecutor(manager=runtime.manager, binding=self.binding,
            delivery=_Delivery(self), limits=runtime.limits, clock=runtime.store.clock)
        self.workspaces = _OwnedWorkspaces(self)
        self.hooks = AgentExecutionHooks(workspaces=self.workspaces,
                                        artifact_delivery=self.workspaces.deliver_request)

    def __repr__(self):
        return "FileGenerationSession(<protected>)"

    def context(self, context):
        if (self.closed or not self.executor.generation_enabled
                or not self.executor.matches(context.inbound or context.background_source)):
            raise GenerationError("file_runtime_source_mismatch")
        if context.platform is None or context.platform.account != self.runtime.account:
            raise GenerationError("file_runtime_source_mismatch")
        return replace(context, capabilities=context.capabilities | {"sandbox"}, agent_executor=self.executor)

    async def close(self):
        """End the turn; the conversation VM and files remain available."""
        self.closed = True
        self.executor.generation_enabled = False
        return {"released": 0, "retained": len(self.executor.retained_resources)}


class _OwnedWorkspaces:
    """Coordinator port backed by this source's executor and sealed byte copies.

    Captures use the existing subagent_artifacts journal as the persistent source
    association. A hash supplied by a worker cannot authorize a blob read. RAM
    captures are accepted only by exact metadata equality; after restart both a
    successful/partial run and its persisted capture are required.
    """
    def __init__(self, session):
        self.session, self.executor = session, session.executor
        self.manager = session.runtime.manager
        self._captured = {}

    def _task(self, task_id):
        if type(task_id) is not int or task_id != self.session.source.task_id:
            raise GenerationError("artifact_task_mismatch")

    @staticmethod
    def _identity(item):
        if (not isinstance(item, dict) or type(item.get("capture_version")) is not int or item["capture_version"] != 1
                or not isinstance(item.get("snapshot"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", item["snapshot"])
                or type(item.get("size")) is not int or item["size"] <= 0
                or not isinstance(item.get("handle"), str)
                or not re.fullmatch(r"s[0-9a-f]{6}:/workspace/.+", item["handle"])
                or canonical_artifact_filename(item.get("name"), draft=False) != item["name"]):
            raise GenerationError("artifact_capture_unavailable")
        return tuple(item[k] for k in ("snapshot", "size", "name", "handle", "capture_version"))

    async def capture(self, task_id, artifacts):
        self._task(task_id)
        if not isinstance(artifacts, list) or len(artifacts) > self.executor.limits.max_artifacts:
            raise ArtifactCaptureError([], ["artifact_capture_limit"])
        captured, errors = [], []
        for item in artifacts:
            try:
                if not isinstance(item, dict):
                    raise GenerationError("artifact_capture_unavailable")
                match = re.fullmatch(r"(s[0-9a-f]{6}):/workspace/(.+)", str(item.get("handle", "")))
                if match is None:
                    raise GenerationError("artifact_capture_unavailable")
                sid, path = match.groups()
                name = canonical_artifact_filename(item.get("name") or path.rsplit("/", 1)[-1], draft=False)
                exported = await self.executor.export_file(sid, workspace_path(path), name)
                # Do not copy model-provided snapshot/checks/draft/URLs to evidence.
                frozen = {"handle": "{}:/workspace/{}".format(sid, exported.path),
                    "name": name, "snapshot": exported.sha256, "size": exported.size,
                    "kind": "file", "source_kind": "file", "capture_version": 1}
                blob = await self.session.runtime.calls(self.session.runtime.store.capture_verified,
                    self.session.source, content=exported.content, filename=name)
                if (blob.sha256, blob.byte_size) != (exported.sha256, exported.size):
                    raise GenerationError("artifact_capture_integrity")
                await self.executor._fence()
                self._captured[self._identity(frozen)] = exported
                captured.append(frozen)
            except Exception:
                errors.append("artifact_capture_unavailable")
        if errors:
            raise ArtifactCaptureError(captured, errors) from None
        return captured

    def _recorded(self, cursor, item, *, run_id=None):
        identity = self._identity(item)
        rows = cursor.execute("""SELECT a.metadata_json FROM subagent_artifacts a
            JOIN subagent_runs r ON r.run_id=a.run_id AND r.task_id=a.task_id
            WHERE a.task_id=? AND a.handle=? AND r.status IN ('succeeded','partial')
            """ + (" AND a.run_id=?" if run_id is not None else ""),
            (self.session.source.task_id, "snapshot:" + identity[0] + ":" + identity[3])
            + ((run_id,) if run_id is not None else ())).fetchall()
        if len(rows) != 1 or self._identity(json.loads(rows[0]["metadata_json"])) != identity:
            raise GenerationError("artifact_capture_unavailable")

    async def _content(self, item, *, run_id=None):
        identity = self._identity(item)
        if identity[1] > min(self.executor.limits.max_file_bytes, self.session.runtime.store.max_bytes):
            raise GenerationError("artifact_capture_limit")
        cached = self._captured.get(identity)
        def read():
            store = self.session.runtime.store
            with store.transaction() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(?)", (BLOB_LOCK,))
                current_source(cursor, self.session.source, store._now())
                if cached is None or run_id is not None:
                    self._recorded(cursor, item, run_id=run_id)
                content = store.blobs.read(identity[0], max_bytes=identity[1] + 1)
                if len(content) != identity[1] or hashlib.sha256(content).hexdigest() != identity[0]:
                    raise GenerationError("artifact_capture_integrity")
                current_source(cursor, self.session.source, store._now())
                return content
        try:
            content = await self.session.runtime.calls(read)
        except Exception:
            raise GenerationError("artifact_capture_unavailable") from None
        await self.executor._fence()
        return content

    async def validate(self, task_id, artifact):
        self._task(task_id)
        await self._content(artifact)
        return {"ok": True, "readable": True, "checks": ["nonempty", "sha256"],
                "functional": "requires_review"}

    async def import_artifact(self, task_id, upstream, arguments):
        self._task(task_id)
        if (not isinstance(arguments, dict) or set(arguments) != {"step_id", "artifact_index", "sandbox_id"}
                or arguments.get("step_id") not in upstream or type(arguments.get("artifact_index")) is not int):
            raise GenerationError("artifact_upstream_unavailable")
        artifacts = upstream[arguments["step_id"]].get("artifacts", [])
        index = arguments["artifact_index"]
        if not 0 <= index < len(artifacts):
            raise GenerationError("artifact_upstream_unavailable")
        item = artifacts[index]
        content = await self._content(item)
        sid = await self.executor._owned(arguments["sandbox_id"])
        target = workspace_path("upstream/" + item["snapshot"] + "/" + item["name"])
        await self.executor._call(self.manager.install_readonly_file, self.executor.owner, sid, target, content)
        return json.dumps({"ok": True, "path": "/workspace/" + target,
                           "sha256": item["snapshot"], "read_only": True})

    async def quiesce_for_validation(self, task_id, completed):
        self._task(task_id)
        return await self.executor.retain_task_sandboxes()

    async def finalize_task(self, task_id, runs, **kwargs):
        self._task(task_id)
        return await self.session.close()

    async def deliver_request(self, request):
        if (not isinstance(request, ArtifactDeliveryRequest)
                or (request.task_id, request.endpoint, request.actor, request.revision) != (
                    self.session.source.task_id, self.session.source.endpoint, self.session.source.actor, 1)):
            raise GenerationError("artifact_delivery_source_mismatch")
        def capture_record():
            store = self.session.runtime.store
            with store.transaction() as cursor:
                current_source(cursor, self.session.source, store._now())
                rows = cursor.execute("""SELECT a.metadata_json FROM subagent_artifacts a
                    JOIN subagent_runs r ON r.run_id=a.run_id AND r.task_id=a.task_id
                    WHERE a.task_id=? AND a.run_id=? AND r.status IN ('succeeded','partial')""",
                    (request.task_id, request.run_id)).fetchall()
                matches = []
                for row in rows:
                    item = json.loads(row["metadata_json"])
                    if item.get("snapshot") != request.snapshot or item.get("size") != request.size:
                        continue
                    self._identity(item)
                    name = "kb-{}-r{}-{}-{}".format(request.task_id, request.revision,
                                                 request.snapshot[:10], item["name"])
                    if request.draft:
                        name = "未完成草稿-" + name
                    if canonical_artifact_filename(name, draft=request.draft) == request.filename:
                        matches.append(item)
                if len(matches) != 1:
                    raise GenerationError("artifact_capture_unavailable")
                return matches[0]
        try:
            item = await self.session.runtime.calls(capture_record)
            content = await self._content(item, run_id=request.run_id)
            cap = await self.executor.readiness()
            if len(content) > cap:
                raise GenerationError("artifact_route_unavailable")
            record = await self.session.runtime.calls(self.session.runtime.store.register, self.session.source,
                content=content, filename=request.filename,
                logical_request_key=_digest(["coordinator", request.task_id, request.run_id,
                    request.revision, request.snapshot, request.filename, request.draft, request.draft_reason]),
                draft=request.draft, draft_reason=request.draft_reason)
            await self.executor._fence()
        except Exception as error:
            logging.getLogger(__name__).warning('QQ artifact registration failed: %s at %s',
                type(error).__name__, ' > '.join(f'{f.name}:{f.lineno}' for f in traceback.extract_tb(error.__traceback__)[-4:]))
            raise GenerationError("artifact_registration_unavailable") from None
        return ArtifactDeliveryView(request.task_id, request.revision, request.endpoint, request.actor,
            record.artifact_id, record.sha256, record.filename, record.byte_size, record.draft,
            record.delivery_state, self.session.runtime.store._now(), request.draft_reason)
