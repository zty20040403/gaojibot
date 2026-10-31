"""Bind foreground and durable tasks to reusable conversation workspaces."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import re

from .image_formats import artifact_filename as canonical_artifact_filename
from ...execution_control import LeaseLost
from ...application.owned_sandbox import (
    ArtifactAvailability, ArtifactRegistration, ExportedArtifact, GenerationError,
    GenerationLimits, OwnedSandboxExecutor,
)
from ...platform_types import AccountKey
from . import turn_source as execution
from .file_artifacts import current_source, foreground_source
from .file_generation import GenerationBindingAdapter

GENERATION_TOOLS = frozenset({"sandbox_read_file", "send_file_from_sandbox"})


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
        self.closed = True
        self.executor.generation_enabled = False
