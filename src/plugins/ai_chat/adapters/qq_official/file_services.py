"""Direct QQ Official file upload and msg_type=7 delivery without intermediate DB queues."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import mimetypes
from pathlib import Path

from src.plugins.ai_chat.application.owned_sandbox import (
    ArtifactAvailability,
    ArtifactRegistration,
    ExportedArtifact,
    GenerationError,
    GenerationLimits,
    OwnedSandboxExecutor,
    safe_filename,
)
from src.plugins.ai_chat.execution_control import LeaseLost
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, BackgroundToolSource, EndpointKey

from .conversation_control import permitted, state
from .file_upload import FileUploadError, FileUploader, MAX_FILE_BYTES
from .image_formats import artifact_filename as canonical_artifact_filename
from .lease import assert_epoch
from .send_types import SendOutcome

GENERATION_TOOLS = frozenset({
    "sandbox_read_file",
    "send_file_from_sandbox",
    "send_file",
    "send_image",
    "generate_image",
    "shell",
    "video",
    "run_code",
})


@dataclass(frozen=True)
class FileServiceConfig:
    private_directory: Path | None = field(default=None, repr=False)
    manager: object = field(default=None, repr=False)
    generation_enabled: bool = False
    generation_limits: GenerationLimits = GenerationLimits(
        max_file_bytes=MAX_FILE_BYTES, max_total_artifact_bytes=512 * 1024 * 1024
    )
    max_file_bytes: int = MAX_FILE_BYTES
    quota_bytes: int = 2 * 1024 * 1024 * 1024
    max_artifacts: int = 1000
    min_free_bytes: int = 0
    account_capacity: int = 16
    actor_capacity: int = 4
    destroy_expired_resources: bool = True
    existing_artifact_authority: object = None


def file_services_factory(config=None):
    async def create(runtime, token, rest):
        return FileServices(runtime, token, rest, config) if config is not None else None
    return create


class _SourceBinding:
    def __init__(self, *, service: FileServices, endpoint: EndpointKey, actor: ActorKey,
                 source_key: str, source_scope_key: str, deadline_at: int,
                 source_native_message_id: str | None, source_canonical_message_id: int | None,
                 turn_id: int | None, generation: int | None, check, background: bool = False,
                 job_request=None, check_transaction=None):
        self.service = service
        self.endpoint = endpoint
        self.actor = actor
        self.source_key = source_key
        self.source_scope_key = source_scope_key
        self.deadline_at = deadline_at
        self.source_native_message_id = source_native_message_id
        self.source_canonical_message_id = source_canonical_message_id
        self.turn_id = turn_id
        self.generation = generation
        self._check = check
        self._background = background
        self.job_request = job_request
        self.check_transaction = check_transaction

    def allows_missing_native_source(self) -> bool:
        return self._background

    def matches_tool_source(self, source: BackgroundToolSource) -> bool:
        return self._background and source.endpoint == self.endpoint

    async def assert_current(self) -> None:
        await self._check()


class _DirectDelivery:
    def __init__(self, session: _DirectSandboxSession):
        self.session = session

    async def readiness(self, binding) -> ArtifactAvailability:
        if binding is not self.session.binding or self.session.closed:
            raise GenerationError("file_generation_closed")
        await binding.assert_current()
        cap = min(MAX_FILE_BYTES, self.session.service.config.max_file_bytes)
        return ArtifactAvailability(True, cap, "native")

    async def register(self, binding, exported: ExportedArtifact, *,
                       logical_request_key: str, draft: bool, draft_reason: str) -> ArtifactRegistration:
        if binding is not self.session.binding or self.session.closed:
            raise GenerationError("file_generation_closed")
        filename = canonical_artifact_filename(exported.filename, draft=draft)
        outcome = await self.session.service.send_bytes(
            endpoint=binding.endpoint,
            requester=binding.actor,
            content=exported.content,
            filename=filename,
            logical_key=(f"{binding.source_key}:action:{binding.job_request['request_id']}"
                         if binding.job_request else f"{binding.source_key}:{logical_request_key}"),
            source_scope_key=binding.source_scope_key,
            expires_at=binding.deadline_at,
            source_native_message_id=binding.source_native_message_id or "",
            source_canonical_message_id=binding.source_canonical_message_id,
            turn_id=binding.turn_id,
            source_generation=binding.generation,
            current=binding.assert_current,
            job_request=binding.job_request,
            check_transaction=binding.check_transaction,
        )
        return ArtifactRegistration(
            artifact_ref=f"qqfile:{exported.sha256[:32]}",
            state="committed" if outcome.status == "committed" else outcome.status,
            message_accepted=outcome.status == "committed",
        )


class _DirectSandboxSession:
    def __init__(self, service: FileServices, binding: _SourceBinding):
        self.service = service
        self.binding = binding
        self.closed = False
        self.executor = OwnedSandboxExecutor(
            manager=service.config.manager,
            binding=binding,
            delivery=_DirectDelivery(self),
            limits=service.config.generation_limits,
            clock=service.clock,
        )

    def context(self, context):
        if (self.closed or not self.executor.generation_enabled
                or not self.executor.matches(context.inbound or context.background_source)
                or context.platform is None or context.platform.account != self.service.account):
            raise GenerationError("file_runtime_source_mismatch")
        return replace(context, capabilities=context.capabilities | {"sandbox"}, agent_executor=self.executor)

    async def close(self):
        self.closed = True
        self.executor.generation_enabled = False


class OfficialFileRuntime:
    def __init__(self, service: FileServices):
        self.service = service

    async def foreground(self, inbound, platform):
        service = self.service
        if not service.config.generation_enabled or service.config.manager is None:
            return None
        life = getattr(platform, "execution", None)
        if (life is None or life.token is not service.token or platform.account != service.account
                or inbound.endpoint.account != service.account
                or (life.handle.endpoint, life.handle.actor, life.handle.source_native_message_id,
                    life.handle.scope_key) != (inbound.endpoint, inbound.actor,
                    inbound.native_message_id, inbound.scope.key)):
            raise LeaseLost("file_runtime_source_mismatch")

        def read_state():
            with service.transaction() as cursor:
                service._account(cursor)
                now = int(service.clock())
                row = life.current(cursor, now)
                if row["state"] != "executing" or row["execution_kind"] != "chat":
                    raise LeaseLost("artifact_source_unavailable")
                return row["deadline_at"], row["journal_turn_id"], state(cursor, inbound.endpoint)["stop_generation"]

        deadline_at, turn_id, generation = await service.calls(read_state)

        async def check():
            await service.calls(read_state)

        binding = _SourceBinding(
            service=service,
            endpoint=inbound.endpoint,
            actor=inbound.actor,
            source_key="turn:" + life.handle.execution_id,
            source_scope_key=inbound.scope.key,
            deadline_at=deadline_at,
            source_native_message_id=inbound.native_message_id,
            source_canonical_message_id=life.handle.canonical_message_id,
            turn_id=turn_id,
            generation=generation,
            check=check,
        )
        return await self._open_session(binding)

    async def background(self, inbound, row, *, current, job_request, check_transaction):
        service = self.service
        if not service.config.generation_enabled or service.config.manager is None:
            return None
        deadline = int(service.clock()) + min(service.runtime.outbox.max_wait_seconds, 86400)

        async def check():
            await service._fence()
            await current()

        binding = _SourceBinding(
            service=service,
            endpoint=inbound.endpoint,
            actor=inbound.actor,
            source_key=f"background:{row['job_id']}",
            source_scope_key=row["scope_key"],
            deadline_at=deadline,
            source_native_message_id=row["native_message_id"],
            source_canonical_message_id=row["canonical_message_id"],
            turn_id=None,
            generation=row["conversation_generation"],
            check=check,
            background=True,
            job_request=job_request,
            check_transaction=check_transaction,
        )
        return await self._open_session(binding)

    async def _open_session(self, binding: _SourceBinding):
        session = _DirectSandboxSession(self.service, binding)
        try:
            await session.executor.readiness()
        except GenerationError:
            return None
        return session


class FileServices:
    def __init__(self, runtime, token, rest, config: FileServiceConfig):
        self.runtime, self.token, self.config = runtime, token, config
        self.account = AccountKey.parse(token.account_key)
        self.calls, self.transaction = runtime._calls, runtime.deliveries.transaction
        self.admission, self.clock = runtime.store.admitted, runtime.store.clock
        self.closed = False
        self.uploader = FileUploader(rest, clock=self.clock)
        self.generation = OfficialFileRuntime(self)

    def _local(self) -> bool:
        return not self.closed and not self.runtime._stopping and self.runtime._token is self.token

    def _account(self, cursor) -> None:
        if not self._local():
            raise LeaseLost("file_services_unavailable")
        assert_epoch(cursor, self.token)

    async def _fence(self) -> None:
        def check():
            with self.transaction() as cursor:
                self._account(cursor)
        await self.calls(check)

    def _route(self, cursor, endpoint: EndpointKey, actor: ActorKey, size: int) -> bool:
        return (0 < size <= min(MAX_FILE_BYTES, self.config.max_file_bytes)
                and self.admission(cursor, endpoint, actor) is True)

    async def send_bytes(self, *, endpoint: EndpointKey, requester: ActorKey, content: bytes,
                         filename: str, logical_key: str, source_scope_key: str, expires_at: int,
                         source_native_message_id: str = "", source_canonical_message_id: int | None = None,
                         turn_id: int | None = None, source_generation: int | None = None,
                         current=None, defer: bool = False, purpose: str = 'requested_artifact', source_proof=None,
                         job_request=None, check_transaction=None):
        """Upload and send; with defer=True, upload and return a function that queues it."""
        filename = safe_filename(filename)
        size = len(content)
        cap = min(MAX_FILE_BYTES, self.config.max_file_bytes)
        if size <= 0:
            raise FileUploadError("文件为空，无法发送。")
        if size > cap:
            raise FileUploadError("文件超过上限，无法上传。")
        if self.runtime.sender is None:
            raise FileUploadError("官方发送通道当前未就绪，请稍后重试。")

        def check_route():
            with self.transaction() as cursor:
                self._account(cursor)
                if not self._route(cursor, endpoint, requester, size):
                    raise FileUploadError("当前会话没有文件发送权限。")
                if not permitted(cursor, endpoint, canonical=source_canonical_message_id, generation=source_generation):
                    raise FileUploadError("当前会话已停止或已重置。")

        await self.calls(check_route)
        uploaded = await self.uploader.upload(endpoint, content, filename, current=current or self._fence)
        mime = "audio/silk" if filename.lower().endswith(".silk") else (
            mimetypes.guess_type(filename)[0] or "application/octet-stream"
        )
        sha256 = hashlib.sha256(content).hexdigest()

        def enqueue(cursor):
            self._account(cursor)
            if check_transaction is not None:
                check_transaction(cursor)
            ref = self.runtime.outbox.enqueue_media(
                self.token,
                endpoint=endpoint,
                requester=requester,
                filename=filename,
                sha256=sha256,
                byte_size=size,
                mime=mime,
                uploaded=uploaded,
                logical_key=logical_key,
                source_scope_key=source_scope_key,
                expires_at=min(expires_at, uploaded.expires_at),
                source_native_message_id=source_native_message_id,
                source_canonical_message_id=source_canonical_message_id,
                turn_id=turn_id,
                source_generation=source_generation,
                purpose=purpose,
                source_proof=source_proof,
                job_request=job_request,
                transaction_cursor=cursor,
            )
            return ref

        if defer:
            # The caller queues it in its own transaction, e.g. right after the reply text.
            return lambda cursor: enqueue(cursor).delivery_ids[0]

        def enqueue_now():
            with self.transaction() as cursor:
                return enqueue(cursor)

        ref = await self.calls(enqueue_now)
        delivery_id = ref.delivery_ids[0]
        if not ref.created and job_request is not None:
            row = await self.calls(self.runtime.outbox.get, self.token, delivery_id)
            return SendOutcome(row['status'], row['wait_reason'] or '', delivery_id)
        outcome = await self.runtime.sender.send_delivery(delivery_id)
        if outcome.status == "failed":
            reason = "这个群没开主动发言，请群主打开" if outcome.reason == "proactive_disabled_no_reply_window" else outcome.reason
            raise FileUploadError(f"文件消息发送失败：{reason}")
        if outcome.status == "ambiguous":
            raise FileUploadError("文件已上传，但消息可能没发出去；请先查看群里的文件，避免重复发送。")
        return outcome

    async def maintain(self):
        cleanup = getattr(self.config.manager, "maintain_conversations", None)
        if cleanup is not None:
            await cleanup()

    async def close(self):
        self.closed = True
        await self.uploader.close()
