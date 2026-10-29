"""SDK-free, source-bound PDF/ZIP generation using the existing sandbox API.

No deployment configuration, model loop, HTTP client, shell-on-host API,
project grants or queue lives in this module.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import asyncio
import hashlib
import inspect
import io
import json
from pathlib import PurePosixPath
import re
import time
from typing import Protocol
import unicodedata
import zipfile

from src.plugins.ai_chat.agent.control import LeaseLost, assert_job_owned
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from src.plugins.ai_chat.tool_invocation import active_tool_call_id


class GenerationError(ValueError):
    """Only fixed diagnostics; never include guest errors or source identity."""


@dataclass(frozen=True)
class GenerationLimits:
    max_file_bytes: int = 20 * 1024 * 1024
    max_write_bytes: int = 1024 * 1024
    max_read_bytes: int = 64 * 1024
    max_seconds: int = 300
    max_sandboxes: int = 2
    max_artifacts: int = 4
    max_total_artifact_bytes: int = 64 * 1024 * 1024
    max_output_chars: int = 16000
    max_zip_entries: int = 1000
    max_zip_expanded_bytes: int = 64 * 1024 * 1024

    def __post_init__(self):
        bounds = {'max_file_bytes': 256 * 1024 * 1024, 'max_write_bytes': 16 * 1024 * 1024,
            'max_read_bytes': 64 * 1024, 'max_seconds': 300, 'max_sandboxes': 16,
            'max_artifacts': 16, 'max_total_artifact_bytes': 256 * 1024 * 1024, 'max_output_chars': 100000, 'max_zip_entries': 10000,
            'max_zip_expanded_bytes': 256 * 1024 * 1024}
        if any(type(getattr(self, k)) is not int or not 1 <= getattr(self, k) <= v for k, v in bounds.items()):
            raise GenerationError('invalid_generation_limits')


@dataclass(frozen=True)
class ArtifactAvailability:
    available: bool
    max_bytes: int
    route_kind: str  # native or link, never a URL


@dataclass(frozen=True)
class ArtifactRegistration:
    artifact_ref: str = field(repr=False)
    state: str = 'stored'
    message_accepted: bool = False

    def __post_init__(self):
        if (not isinstance(self.artifact_ref, str) or not re.fullmatch(r'[a-zA-Z0-9:_-]{1,160}', self.artifact_ref)
                or self.state not in {'stored', 'waiting'} or self.message_accepted is not False):
            raise GenerationError('invalid_artifact_registration')


@dataclass(frozen=True, eq=False)
class ExportedArtifact:
    endpoint: EndpointKey = field(repr=False)
    actor: ActorKey = field(repr=False)
    source_key: str = field(repr=False)
    owner: str = field(repr=False)
    sandbox_id: str = field(repr=False)
    path: str = field(repr=False)
    filename: str = field(repr=False)
    mime: str
    content: bytes = field(repr=False)
    sha256: str = field(repr=False)
    size: int
    checks: tuple[str, ...]


class GenerationBinding(Protocol):
    endpoint: EndpointKey
    actor: ActorKey
    source_key: str
    deadline_at: int
    source_native_message_id: str | None

    async def assert_current(self) -> None: ...
    # Host implementation atomically reserves aggregate actor/account capacity;
    # a model cannot choose the owner, reservation or quota principal.
    async def reserve_capacity(self, owner: str) -> object: ...
    async def release_capacity(self, reservation: object) -> None: ...


class ArtifactDeliveryPort(Protocol):
    async def readiness(self, binding: GenerationBinding) -> ArtifactAvailability: ...
    # Must revalidate binding in its manifest+job transaction and be idempotent
    # by source+logical_request_key. Bytes are durable before returning.
    async def register(self, binding: GenerationBinding, exported: ExportedArtifact, *,
                       logical_request_key: str, draft: bool, draft_reason: str) -> ArtifactRegistration: ...


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _text(value, *, maximum, blank=False):
    if (not isinstance(value, str) or (not blank and not value.strip())
            or any(unicodedata.category(c) in {'Cc', 'Cs'} and c not in '\n\r\t' for c in value)):
        raise GenerationError('invalid_tool_argument')
    try:
        if len(value.encode('utf-8')) > maximum:
            raise GenerationError('tool_argument_too_large')
    except UnicodeEncodeError:
        raise GenerationError('invalid_tool_argument') from None
    return value


def workspace_path(value):
    value = _text(value, maximum=1024)
    path = PurePosixPath(value)
    if (path.is_absolute() or any(p in {'', '.', '..'} for p in value.split('/'))
            or '\\' in value or any(unicodedata.category(c).startswith('C') for c in value)):
        raise GenerationError('invalid_workspace_path')
    return str(path)


def safe_filename(value):
    value = _text(value, maximum=180)
    if (value != value.strip() or value != PurePosixPath(value).name or value in {'.', '..'} or '\\' in value
            or any(unicodedata.category(c).startswith('C') for c in value)):
        raise GenerationError('invalid_filename')
    return value


def _identity(binding):
    return binding.endpoint, binding.actor, binding.source_key, binding.deadline_at, binding.source_native_message_id


class OwnedSandboxExecutor:
    def __init__(self, *, manager, binding: GenerationBinding, delivery: ArtifactDeliveryPort,
                 limits=GenerationLimits(), clock=time.time):
        identity = _identity(binding)
        endpoint, actor, source, deadline, native = identity
        background = getattr(binding,'allows_missing_native_source',None)
        valid_native = (isinstance(native,str) and bool(native)) or (native is None
            and callable(background) and background() is True)
        if (not isinstance(endpoint, EndpointKey) or endpoint.account.platform != 'qq-official'
                or not isinstance(actor, ActorKey) or not actor.matches_endpoint(endpoint)
                or actor.identity_kind == 'bot' or not isinstance(source, str) or not source
                or len(source.encode('utf-8')) > 2048 or type(deadline) is not int
                or not valid_native
                or not isinstance(limits, GenerationLimits)):
            raise GenerationError('invalid_generation_source')
        if any(not callable(getattr(binding, name, None)) for name in
               ('assert_current', 'reserve_capacity', 'release_capacity')):
            raise GenerationError('source_capacity_port_required')
        self.binding, self.delivery, self.sandbox_manager = binding, delivery, manager
        self.limits, self.clock, self._identity = limits, clock, identity
        actor_digest = hashlib.sha256(_json([endpoint.account.key, endpoint.key, actor.key]).encode()).hexdigest()
        source_digest = hashlib.sha256(source.encode()).hexdigest()[:32]
        self.base_owner = 'qqo:v1:' + actor_digest
        self.owner = self.base_owner + ':x:' + source_digest
        self.generation_enabled = False
        self._uncertain = False
        self._allocation_lock = asyncio.Lock()
        self._export_lock = asyncio.Lock()
        self._resources = {}  # (owner,sandbox_id) -> host capacity reservation
        self._exports = {}
        self._logical = {}

    def matches(self, inbound):
        from ..platform_types import BackgroundToolSource
        if isinstance(inbound,BackgroundToolSource):
            match = getattr(self.binding,'matches_tool_source',None)
            return match is not None and match(inbound) is True
        return (inbound is not None and inbound.endpoint == self._identity[0]
                and inbound.actor == self._identity[1]
                and inbound.native_message_id == self._identity[4])

    @property
    def retained_resources(self):
        """Host-only cleanup metadata, not tool output or permission to reuse."""
        return tuple(self._resources)

    async def _fence(self):
        assert_job_owned()
        if self._uncertain:
            raise LeaseLost('generation_operation_requires_attention')
        if _identity(self.binding) != self._identity or self.clock() >= self._identity[3]:
            raise LeaseLost('generation_source_changed_or_expired')
        await self.binding.assert_current()
        assert_job_owned()
        if _identity(self.binding) != self._identity or self.clock() >= self._identity[3]:
            raise LeaseLost('generation_source_changed_or_expired')

    async def _call(self, operation, *args, observed=None, **kwargs):
        await self._fence()
        from src.sandbox_runtime.source_fence import active_sandbox_source
        token = active_sandbox_source.set(self._fence)
        try:
            result = await operation(*args, **kwargs)
            if observed is not None:
                observed(result)  # Remember resources before a post-source check can fail.
            await self._fence()
            return result
        except BaseException:
            # A guest mutation may have happened even without a result. A new
            # tool call must not replay it under this live source automatically.
            if ((getattr(operation, '__self__', None) is self.sandbox_manager
                    and getattr(operation, '__name__', '') in {'create', 'start_owned', 'exec', 'write_file', 'destroy', 'export_artifact'})
                    or getattr(operation, '__self__', None) is self.delivery
                    and getattr(operation, '__name__', '') == 'register'):
                self._uncertain = True
                self.generation_enabled = False
            raise
        finally:
            active_sandbox_source.reset(token)

    async def readiness(self):
        await self._fence()
        cap = getattr(self.sandbox_manager, 'max_file_bytes', None)
        signature = inspect.signature(self.sandbox_manager.export_artifact)
        if (type(cap) is not int or not 1 <= cap <= 256 * 1024 * 1024
                or not {'file_only', 'max_bytes'} <= set(signature.parameters)):
            raise GenerationError('bounded_export_unavailable')
        if getattr(self.sandbox_manager, 'requires_export_handshake', False):
            health = await self._call(self.sandbox_manager.health)
            if (not isinstance(health, dict) or type(health.get('bounded_file_export_version')) is not int
                    or health['bounded_file_export_version'] != 1
                    or type(health.get('max_file_bytes')) is not int or health['max_file_bytes'] <= 0):
                raise GenerationError('bounded_export_runner_unavailable')
            cap = min(cap, health['max_file_bytes'])
        available = await self._call(self.delivery.readiness, self.binding)
        if (not isinstance(available, ArtifactAvailability) or available.available is not True
                or available.route_kind not in {'native', 'link'} or type(available.max_bytes) is not int
                or not 1 <= available.max_bytes <= 256 * 1024 * 1024):
            raise GenerationError('artifact_route_unavailable')
        self.generation_enabled = True
        return min(cap, self.limits.max_file_bytes, available.max_bytes)

    async def _create(self, runtime, *, owner=None):
        async with self._allocation_lock:
            return await self._create_locked(runtime, owner=owner)

    async def _create_locked(self, runtime, *, owner=None):
        owner = owner or self.owner
        await self.readiness()
        if runtime not in {'python', 'node', 'debian'}:
            raise GenerationError('unsupported_runtime')
        if sum(resource[0] == self.owner for resource in self._resources) >= self.limits.max_sandboxes and owner == self.owner:
            raise GenerationError('source_sandbox_limit')
        capacity = await self._call(self.binding.reserve_capacity, owner)
        if capacity is None:
            raise GenerationError('generation_capacity_unavailable')
        # Unknown create/cancel does not refund capacity: the host must reconcile.
        def remember(result):
            sid = result.get('sandbox_id') if isinstance(result, dict) else None
            if not isinstance(sid, str) or not re.fullmatch(r's[0-9a-f]{6}', sid):
                raise GenerationError('invalid_sandbox_receipt')
            self._resources[(owner, sid)] = capacity
        result = await self._call(self.sandbox_manager.create, owner, runtime, purpose='task', observed=remember)
        return result['sandbox_id']

    async def _owned(self, value):
        if not isinstance(value, str) or not re.fullmatch(r's[0-9a-f]{6}', value):
            raise GenerationError('invalid_sandbox_id')
        # Current objects originate only from this executor's creation. Restart
        # restoration needs a host capacity/source reconciliation, not model IDs.
        if (self.owner, value) not in self._resources:
            raise GenerationError('sandbox_source_mismatch')
        await self._call(self.sandbox_manager.start_owned, self.owner, value)
        return value

    async def _destroy(self, owner, sid):
        reservation = self._resources.get((owner, sid))
        if reservation is None:
            raise GenerationError('sandbox_source_mismatch')
        await self._call(self.sandbox_manager.destroy, owner, sid)
        await self._call(self.binding.release_capacity, reservation)
        self._resources.pop((owner, sid))

    async def retain_task_sandboxes(self):
        stopped = 0
        for owner, sid in tuple(self._resources):
            await self._call(self.sandbox_manager.stop_owned, owner, sid)
            stopped += 1
        return {'stopped': stopped, 'retained': len(self._resources)}

    async def execute(self, name, arguments):
        allowed = {
            'sandbox_create': {'runtime'}, 'sandbox_list': set(), 'sandbox_destroy': {'sandbox_id'},
            'sandbox_write_file': {'sandbox_id', 'path', 'content'}, 'sandbox_read_file': {'sandbox_id', 'path'},
            'sandbox_exec': {'sandbox_id', 'command', 'packages', 'timeout_seconds', 'background'},
            'send_file_from_sandbox': {'sandbox_id', 'path', 'filename'},
        }
        if name not in allowed:
            return None
        try:
            if not isinstance(arguments, dict) or not set(arguments) <= allowed[name]:
                raise GenerationError('invalid_tool_arguments')
            await self._fence()
            if name == 'sandbox_create':
                sid = await self._create(arguments.get('runtime', 'python'))
                return _json({'ok': True, 'sandbox_id': sid, 'purpose': 'task'})
            if name == 'sandbox_list':
                items = await self._call(self.sandbox_manager.list, self.owner)
                visible = [item['sandbox_id'] for item in items if isinstance(item, dict)
                    and (self.owner, item.get('sandbox_id')) in self._resources]
                return _json({'ok': True, 'sandbox_ids': visible})
            sid = await self._owned(arguments.get('sandbox_id'))
            if name == 'sandbox_destroy':
                await self._destroy(self.owner, sid)
                return _json({'ok': True, 'state': 'destroyed'})
            if name == 'sandbox_exec':
                if arguments.get('background', False) is not False:
                    raise GenerationError('detached_sandbox_job_disabled')
                command = _text(arguments.get('command'), maximum=32768)
                timeout = arguments.get('timeout_seconds', self.limits.max_seconds)
                packages = arguments.get('packages', [])
                if (type(timeout) is not int or not 1 <= timeout <= self.limits.max_seconds
                        or not isinstance(packages, list) or len(packages) > 16
                        or any(not isinstance(p, str) or not re.fullmatch(r'[a-zA-Z0-9_.+-]{1,128}', p) for p in packages)):
                    raise GenerationError('invalid_execution_limits')
                remaining = int(self._identity[3] - self.clock())
                if remaining <= 0:
                    raise LeaseLost('generation_deadline')
                result = await self._call(self.sandbox_manager.exec, self.owner, sid, command,
                    min(timeout, remaining), packages=packages)
                return _json({'ok': result.returncode == 0, 'returncode': result.returncode,
                    'stdout': result.stdout[:self.limits.max_output_chars],
                    'stderr': result.stderr[:self.limits.max_output_chars]})
            path = workspace_path(arguments.get('path'))
            if name == 'sandbox_write_file':
                content = _text(arguments.get('content'), maximum=self.limits.max_write_bytes, blank=True).encode()
                size = await self._call(self.sandbox_manager.write_file, self.owner, sid, path, content)
                if size != len(content):
                    raise GenerationError('invalid_write_receipt')
                return _json({'ok': True, 'bytes_written': size})
            if name == 'sandbox_read_file':
                content, directory = await self._call(self.sandbox_manager.export_artifact,
                    self.owner, sid, path, file_only=True, max_bytes=self.limits.max_read_bytes)
                if directory or not isinstance(content, bytes) or len(content) > self.limits.max_read_bytes:
                    raise GenerationError('invalid_export_receipt')
                return _json({'ok': True, 'content': content.decode('utf-8', errors='replace')})
            call_id = active_tool_call_id.get()
            if not isinstance(call_id, str) or not call_id:
                raise GenerationError('host_tool_call_required')
            logical = 'generated:' + hashlib.sha256(_json([self._identity[2], call_id]).encode()).hexdigest()
            exported = await self.export_file(sid, path, arguments.get('filename') or PurePosixPath(path).name)
            return await self.send_file_content(exported.content, exported.filename,
                provenance=exported, logical_request_key=logical)
        except GenerationError as error:
            return _json({'ok': False, 'code': str(error), 'message_accepted': False})
        except Exception:
            # Includes driver errors: do not leak stdout/host paths/credentials
            # through arbitrary exception text. LeaseLost/CancelledError escape.
            return _json({'ok': False, 'code': 'generation_operation_failed', 'message_accepted': False})

    async def export_file(self, sid, path, filename):
        async with self._export_lock:
            return await self._export_file_locked(sid, path, filename)

    async def _export_file_locked(self, sid, path, filename):
        cap = await self.readiness()
        sid = await self._owned(sid)
        path, filename = workspace_path(path), safe_filename(filename)
        if len(self._exports) >= self.limits.max_artifacts:
            raise GenerationError('source_artifact_limit')
        remaining = self.limits.max_total_artifact_bytes - sum(item.size for item in self._exports.values())
        if remaining <= 0:
            raise GenerationError('source_artifact_bytes_limit')
        cap = min(cap, remaining)
        if PurePosixPath(filename).suffix.lower() not in {'.pdf', '.zip'}:
            raise GenerationError('artifact_type_unavailable')
        def validate_receipt(result):
            if (not isinstance(result, tuple) or len(result) != 2 or result[1] is not False
                    or not isinstance(result[0], bytes) or not 0 < len(result[0]) <= cap):
                raise GenerationError('invalid_export_receipt')
        content, directory = await self._call(self.sandbox_manager.export_artifact,
            self.owner, sid, path, file_only=True, max_bytes=cap, observed=validate_receipt)
        if filename.lower().endswith('.pdf'):
            if not content.startswith(b'%PDF-'):
                raise GenerationError('artifact_type_mismatch')
            mime = 'application/pdf'
        else:
            self._validate_zip(content)
            mime = 'application/zip'
        checks = await self._validate_snapshot(content, filename)
        exported = ExportedArtifact(*self._identity[:3], self.owner, sid, path, filename,
            mime, content, hashlib.sha256(content).hexdigest(), len(content), checks)
        self._exports[id(exported)] = exported
        return exported

    def _validate_zip(self, content):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                infos = archive.infolist()
                if not infos or len(infos) > self.limits.max_zip_entries:
                    raise GenerationError('zip_entry_limit')
                total = 0
                for info in infos:
                    workspace_path(info.filename.rstrip('/'))
                    if info.flag_bits & 1 or (info.external_attr >> 16) & 0o170000 == 0o120000:
                        raise GenerationError('unsupported_zip_entry')
                    if total + info.file_size > self.limits.max_zip_expanded_bytes:
                        raise GenerationError('zip_expanded_limit')
                    with archive.open(info) as stream:
                        while data := stream.read(65536):
                            total += len(data)
                            if total > self.limits.max_zip_expanded_bytes:
                                raise GenerationError('zip_expanded_limit')
        except (OSError, ValueError, zipfile.BadZipFile, NotImplementedError):
            raise GenerationError('invalid_zip') from None

    async def _validate_snapshot(self, content, filename):
        owner = self.owner + ':verify'
        sid = await self._create('python', owner=owner)
        path = 'acceptance' + PurePosixPath(filename).suffix.lower()
        try:
            await self._call(self.sandbox_manager.write_file, owner, sid, path, content, allow_large=True)
            commands = ([('pdfinfo acceptance.pdf', 'pdf_structure'),
                         ('pdffonts acceptance.pdf', 'pdf_fonts'),
                         ('pdftotext acceptance.pdf -', 'pdf_text')]
                        if path.endswith('.pdf') else [('unzip -t acceptance.zip', 'zip_structure')])
            checks = []
            fonts = text = ''
            for command, label in commands:
                remaining = int(self._identity[3] - self.clock())
                if remaining <= 0:
                    raise LeaseLost('generation_deadline')
                result = await self._call(self.sandbox_manager.exec, owner, sid, command,
                    min(30, remaining), packages=['poppler-utils'] if getattr(self.sandbox_manager, 'backend', 'oci') == 'vm' and path.endswith('.pdf') else None)
                if result.returncode != 0:
                    raise GenerationError('artifact_validation_failed')
                if label == 'pdf_fonts':
                    fonts = result.stdout
                if label == 'pdf_text':
                    text = result.stdout
                checks.append(label)
            if path.endswith('.pdf') and (not text.strip() or not any(re.search(
                    r'\s+yes\s+(?:yes|no)\s+(?:yes|no)\s+\d+\s+\d+\s*$', line.lower()) for line in fonts.splitlines())):
                raise GenerationError('pdf_text_or_font_validation_failed')
            return tuple(checks)
        finally:
            # No stale source writes after loss; remembered exact resource is
            # left for host cleanup. Do not swallow genuine cancellation.
            import sys
            if sys.exc_info()[0] is None or issubclass(sys.exc_info()[0], Exception):
                await self._destroy(owner, sid)

    async def send_file_content(self, content, filename, *, provenance, logical_request_key,
                                draft=False, draft_reason=''):
        cap = await self.readiness()
        if (self._exports.get(id(provenance)) is not provenance or not isinstance(provenance, ExportedArtifact)
                or not isinstance(content, bytes) or content != provenance.content
                or provenance.owner != self.owner or provenance.source_key != self._identity[2]
                or filename != provenance.filename or len(content) != provenance.size or len(content) > cap
                or hashlib.sha256(content).hexdigest() != provenance.sha256
                or type(draft) is not bool or not isinstance(draft_reason, str)
                or len(draft_reason.encode('utf-8')) > 1000 or (not draft and draft_reason)
                or not isinstance(logical_request_key, str) or not re.fullmatch(r'generated:[0-9a-f]{64}', logical_request_key)):
            raise GenerationError('invalid_generated_provenance')
        previous = self._logical.get(logical_request_key)
        frozen = (provenance.sha256, filename, draft, draft_reason)
        if previous is not None and previous != frozen:
            raise GenerationError('artifact_logical_conflict')
        self._logical[logical_request_key] = frozen
        registered = await self._call(self.delivery.register, self.binding, provenance,
            logical_request_key=logical_request_key, draft=draft, draft_reason=draft_reason)
        if not isinstance(registered, ArtifactRegistration):
            raise GenerationError('invalid_artifact_registration')
        return _json({'ok': True, 'stored': True, 'artifact_ref': registered.artifact_ref,
            'delivery_state': registered.state, 'message_accepted': False,
            'content_accepted': False, 'draft': draft, 'size': provenance.size,
            'message': '产物已登记，交付状态仍需实际消息回执确认。'})
