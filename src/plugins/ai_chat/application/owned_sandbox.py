"""Official conversation workspaces and native file delivery."""
from __future__ import annotations

from dataclasses import dataclass, field
import asyncio
import base64
import hashlib
import inspect
import json
from pathlib import PurePosixPath
import re
import shlex
import time
from typing import Protocol
import unicodedata
import mimetypes
import logging
import traceback

logger = logging.getLogger(__name__)

from src.plugins.ai_chat.execution_control import LeaseLost, assert_job_owned
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from src.plugins.ai_chat.tool_images import ToolImage
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
            'max_artifacts': 16, 'max_total_artifact_bytes': 512 * 1024 * 1024, 'max_output_chars': 100000, 'max_zip_entries': 10000,
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
                or self.state not in {'stored', 'waiting', 'committed', 'ambiguous', 'failed'}):
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


class ArtifactDeliveryPort(Protocol):
    async def readiness(self, binding: GenerationBinding) -> ArtifactAvailability: ...
    # Must revalidate binding in its manifest+job transaction and be idempotent
    # by source+logical_request_key. Bytes are durable before returning.
    async def register(self, binding: GenerationBinding, exported: ExportedArtifact, *,
                       logical_request_key: str, draft: bool, draft_reason: str) -> ArtifactRegistration: ...


# The model kept exporting login cookies (for yt-dlp) despite the browser skill;
# refuse code that reads browser cookies, storage or the profile directory.
_BROWSER_SECRETS = re.compile(
    r'\.cookies\s*\(|document\.cookie|storage_?state|cookies-from-browser|\.gaoji/chromium', re.I)


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
    for root in ('/workspace/', '/work/'):
        if value.startswith(root):
            value = value[len(root):]
            break
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
        self.binding, self.delivery, self.sandbox_manager = binding, delivery, manager
        self.limits, self.clock, self._identity = limits, clock, identity
        self.owner = 'qqo:conversation:' + hashlib.sha256(endpoint.key.encode()).hexdigest()
        self.generation_enabled = False
        locks = getattr(manager, '_conversation_locks', None)
        if locks is None:
            manager._conversation_locks = locks = {}
        self._allocation_lock = locks.setdefault(self.owner, asyncio.Lock())
        self._export_lock = asyncio.Lock()
        self._resources = {}  # Workspaces owned by this conversation.
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
        items = await self._call(self.sandbox_manager.list, owner)
        if items:
            sid = items[0]['sandbox_id']
            self._resources[(owner, sid)] = True
            await self._call(self.sandbox_manager.start_owned, owner, sid)
            return sid
        def remember(result):
            self._resources[(owner, result['sandbox_id'])] = True
        result = await self._call(self.sandbox_manager.create, owner, runtime, purpose='task', observed=remember)
        return result['sandbox_id']

    async def _owned(self, value):
        if not isinstance(value, str) or not re.fullmatch(r's[0-9a-f]{6}', value):
            raise GenerationError('invalid_sandbox_id')
        # The manager verifies the stable conversation owner, including after restart.
        await self._call(self.sandbox_manager.start_owned, self.owner, value)
        self._resources[(self.owner, value)] = True
        return value

    async def _destroy(self, owner, sid):
        await self._call(self.sandbox_manager.destroy, owner, sid)
        self._resources.pop((owner, sid), None)

    async def retain_task_sandboxes(self):
        return {'stopped': 0, 'retained': len(self._resources)}

    async def _ensure_workspace(self) -> str:
        async with self._allocation_lock:
            await self.readiness()
            if hasattr(self.sandbox_manager, 'ensure_default'):
                def remember_default(result):
                    if isinstance(result, dict) and result.get('sandbox_id'):
                        self._resources[(self.owner, result['sandbox_id'])] = True
                info = await self._call(self.sandbox_manager.ensure_default, self.owner, observed=remember_default)
                sid = info['sandbox_id']
                self._resources[(self.owner, sid)] = True
                await self._call(self.sandbox_manager.start_owned, self.owner, sid)
                return sid
            return await self._create_locked('python', owner=self.owner)

    async def _run_in_container(
        self, sid: str, command: str, *, workdir: str = '/work', timeout: int = 120, input_bytes: bytes | None = None,
    ):
        if hasattr(self.sandbox_manager, 'run_container_command'):
            return await self._call(
                self.sandbox_manager.run_container_command,
                self.owner,
                sid,
                command,
                workdir=workdir,
                timeout_seconds=timeout,
                input_bytes=input_bytes,
            )
        return await self._call(self.sandbox_manager.exec, self.owner, sid, command, timeout)

    async def _read_multimodal_bytes(self, sid: str, path: str, *, max_bytes: int, delete_after: bool = False) -> bytes:
        rel = workspace_path(path)
        if hasattr(self.sandbox_manager, 'export_temp_bytes'):
            return await self._call(
                self.sandbox_manager.export_temp_bytes,
                self.owner,
                sid,
                rel,
                max_bytes=max_bytes,
                delete_after=delete_after,
            )
        content, directory = await self._call(
            self.sandbox_manager.export_artifact,
            self.owner,
            sid,
            rel,
            file_only=True,
            max_bytes=max_bytes,
        )
        if directory or not isinstance(content, bytes) or not 0 < len(content) <= max_bytes:
            raise GenerationError('invalid_export_receipt')
        return content

    @staticmethod
    def _supports_video(model_profile) -> bool:
        return bool(
            model_profile is not None
            and getattr(model_profile, 'protocol', None) == 'openai-chat'
            and getattr(getattr(model_profile, 'capabilities', None), 'video', False)
        )

    async def _execute_shell(self, arguments: dict) -> str:
        command = _text(arguments.get('command'), maximum=32768)
        raw_wd = arguments.get('workdir')
        if raw_wd is None or str(raw_wd).strip() in {'', '/work', '/workspace'}:
            wd = '/work'
        else:
            wd = '/work/' + workspace_path(raw_wd)
        background = arguments.get('background', False)
        if type(background) is not bool:
            raise GenerationError('invalid_tool_arguments')
        sid = await self._ensure_workspace()
        if background:
            title = _text(arguments.get('title'), maximum=200) if arguments.get('title') else ''
            bg_cmd = f'gaoji units start {shlex.quote(command)} --workdir {shlex.quote(wd)}'
            if title:
                bg_cmd += f' --title {shlex.quote(title)}'
            result = await self._run_in_container(sid, bg_cmd, workdir=wd, timeout=30)
            out_text = result.stdout.strip()
            if result.returncode == 0 and out_text:
                try:
                    parsed = json.loads(out_text.splitlines()[-1])
                    if isinstance(parsed, dict):
                        return _json(parsed)
                except ValueError:
                    pass
            return _json({
                'ok': result.returncode == 0,
                'returncode': result.returncode,
                'stdout': result.stdout[:self.limits.max_output_chars],
                'stderr': result.stderr[:self.limits.max_output_chars],
            })
        timeout = arguments.get('timeout', 120)
        if type(timeout) is not int or not 1 <= timeout <= self.limits.max_seconds:
            raise GenerationError('invalid_execution_limits')
        remaining = int(self._identity[3] - self.clock())
        if remaining <= 0:
            raise LeaseLost('generation_deadline')
        result = await self._run_in_container(sid, command, workdir=wd, timeout=min(timeout, remaining))
        return _json({
            'ok': result.returncode == 0,
            'returncode': result.returncode,
            'stdout': result.stdout[:self.limits.max_output_chars],
            'stderr': result.stderr[:self.limits.max_output_chars],
        })

    @staticmethod
    def _sniff_image_ext(content: bytes) -> str | None:
        if content.startswith(b'\x89PNG\r\n\x1a\n'):
            return '.png'
        if content.startswith(b'\xff\xd8'):
            return '.jpg'
        if content[:6] in {b'GIF87a', b'GIF89a'}:
            return '.gif'
        return None

    async def _execute_send_image(self, arguments: dict) -> str:
        from src.plugins.ai_chat.adapters.qq_official.image_formats import (
            ImageFormatError,
            detect_image,
            mime_matches_filename,
        )

        sid = await self._ensure_workspace()
        path = workspace_path(arguments.get('path'))
        call_id = active_tool_call_id.get()
        if not isinstance(call_id, str) or not call_id:
            raise GenerationError('host_tool_call_required')
        logical = 'generated:' + hashlib.sha256(_json([self._identity[2], call_id]).encode()).hexdigest()
        requested_name = arguments.get('filename') or arguments.get('name') or PurePosixPath(path).name
        try:
            async with asyncio.timeout(300):
                raw_bytes = await self._read_multimodal_bytes(sid, path, max_bytes=16 * 1024 * 1024)
                # Automatically convert WebP (common on the web) to PNG inside container before sending.
                if (len(raw_bytes) >= 12 and raw_bytes[:4] == b'RIFF' and raw_bytes[8:12] == b'WEBP'
                        and hasattr(self.sandbox_manager, 'run_container_command')):
                    conv_rel = f'.gaoji/shown/conv-{hashlib.sha256(raw_bytes[:256]).hexdigest()[:12]}.png'
                    py = (
                        'from pathlib import Path; from PIL import Image; import sys; '
                        'dst = Path(sys.argv[2]); dst.parent.mkdir(parents=True, exist_ok=True); '
                        'Image.open(sys.argv[1]).save(dst, format="PNG")'
                    )
                    conv = await self._run_in_container(
                        sid,
                        f'gaoji-python -c {shlex.quote(py)} {shlex.quote("/work/" + path)} {shlex.quote("/work/" + conv_rel)}',
                        timeout=30,
                    )
                    if conv.returncode == 0:
                        path = conv_rel
                        raw_bytes = await self._read_multimodal_bytes(sid, path, max_bytes=16 * 1024 * 1024)
                sniffed_ext = self._sniff_image_ext(raw_bytes)
                if sniffed_ext is not None:
                    expected_mime = {'.png': 'image/png', '.jpg': 'image/jpeg', '.gif': 'image/gif'}[sniffed_ext]
                    if not mime_matches_filename(expected_mime, str(requested_name)):
                        stem = PurePosixPath(str(requested_name)).stem or 'image'
                        requested_name = f'{stem}{sniffed_ext}'
                exported = await self.export_file(sid, path, requested_name)
                mime = detect_image(exported.content, exported.filename)
                if exported.mime != mime:
                    exported = ExportedArtifact(
                        *self._identity[:3], self.owner, sid, exported.path, exported.filename,
                        mime, exported.content, exported.sha256, exported.size, exported.checks,
                    )
                    self._exports[id(exported)] = exported
        except ImageFormatError as error:
            return _json({
                'ok': False,
                'code': str(error),
                'error': f'图片格式或大小不符合要求（{error}），请先用 shell / ffmpeg / Pillow 转为合规 PNG/JPG/GIF 再发。',
                'retryable': True,
                'message_accepted': False,
            })
        except Exception as error:
            return _json({
                'ok': False,
                'code': str(error) if isinstance(error, GenerationError) else 'image_export_failed',
                'error': '图片尚未发送，读取或校验失败，请检查路径后重试。',
                'retryable': True,
                'message_accepted': False,
            })
        return await self.send_file_content(
            exported.content, exported.filename, provenance=exported, logical_request_key=logical,
        )

    async def _execute_video(self, arguments: dict, *, model_profile=None):
        if not self._supports_video(model_profile):
            return _json({
                'ok': False,
                'code': 'video_not_supported',
                'error': '当前会话模型不支持原生看视频，请切换 Gemini 模型或用 shell 抽帧/转写音频。',
            })
        sid = await self._ensure_workspace()
        rel_path = workspace_path(arguments.get('path'))
        req: dict[str, object] = {'work': '/work', 'path': '/work/' + rel_path}
        if arguments.get('start') is not None:
            if not isinstance(arguments['start'], (int, float, str)) or isinstance(arguments['start'], bool):
                raise GenerationError('invalid_tool_arguments')
            req['start'] = arguments['start']
        if arguments.get('end') is not None:
            if not isinstance(arguments['end'], (int, float, str)) or isinstance(arguments['end'], bool):
                raise GenerationError('invalid_tool_arguments')
            req['end'] = arguments['end']
        remaining = int(self._identity[3] - self.clock())
        if remaining <= 0:
            raise LeaseLost('generation_deadline')
        cmd = f'node /etc/gaoji/runner/runner.js --video {shlex.quote(_json(req))}'
        res = await self._run_in_container(sid, cmd, workdir='/work', timeout=min(240, remaining))
        line = res.stdout.strip().splitlines()[-1] if res.stdout.strip() else ''
        try:
            outcome = json.loads(line)
        except ValueError:
            return _json({
                'ok': False,
                'code': 'video_runner_failed',
                'error': (res.stderr or res.stdout or '容器内视频转换失败')[:500],
            })
        if not isinstance(outcome, dict) or not outcome.get('ok'):
            err = outcome.get('error') if isinstance(outcome, dict) else None
            msg = err.get('message') if isinstance(err, dict) else str(err or '视频读取失败')
            code = err.get('code') if isinstance(err, dict) else 'video_failed'
            return _json({'ok': False, 'code': code, 'error': msg})
        val = outcome.get('value') or {}
        out_path = str(val.get('path') or '')
        data = await self._read_multimodal_bytes(sid, out_path, max_bytes=32 * 1024 * 1024, delete_after=True)
        meta = _json({
            'ok': True,
            'path': '/work/' + rel_path,
            'seconds': val.get('seconds'),
            'bytes': len(data),
        })
        b64 = base64.b64encode(data).decode('ascii')
        return ToolImage(meta, f'data:video/mp4;base64,{b64}')

    async def _execute_run_code(self, arguments: dict, *, model_profile=None):
        from src.plugins.ai_chat.adapters.qq_official.image_formats import detect_image

        code = _text(arguments.get('code'), maximum=65536)
        extra_args = arguments.get('args')
        if extra_args is not None and not isinstance(extra_args, dict):
            raise GenerationError('invalid_tool_arguments')
        timeout = arguments.get('timeout', 120)
        if type(timeout) is not int or not 1 <= timeout <= self.limits.max_seconds:
            raise GenerationError('invalid_execution_limits')
        remaining = int(self._identity[3] - self.clock())
        if remaining <= 0:
            raise LeaseLost('generation_deadline')
        sid = await self._ensure_workspace()
        payload = json.dumps({'code': code, 'args': extra_args or {}, 'work': '/work'}, ensure_ascii=False).encode('utf-8')
        res = await self._run_in_container(
            sid,
            'node /etc/gaoji/runner/runner.js',
            workdir='/work',
            timeout=min(timeout, remaining),
            input_bytes=payload,
        )
        line = res.stdout.strip().splitlines()[-1] if res.stdout.strip() else ''
        try:
            envelope = json.loads(line)
        except ValueError:
            return _json({
                'ok': False,
                'returncode': res.returncode,
                'error': (res.stderr or res.stdout or 'run_code 执行失败')[:4000],
            })
        shown_items = envelope.get('shown') if isinstance(envelope.get('shown'), list) else []
        media_urls: list[str] = []
        warnings: list[str] = []
        for item in shown_items[:6]:
            if not isinstance(item, dict):
                continue
            kind = item.get('kind')
            spath = str(item.get('path') or '')
            if not spath:
                continue
            is_scratch = spath.startswith('/work/.gaoji/shown/')
            try:
                if kind == 'image':
                    raw_img = await self._read_multimodal_bytes(
                        sid, spath, max_bytes=10 * 1024 * 1024, delete_after=is_scratch,
                    )
                    ext = self._sniff_image_ext(raw_img) or '.png'
                    mime = detect_image(raw_img, f'shown{ext}')
                    media_urls.append(f"data:{mime};base64,{base64.b64encode(raw_img).decode('ascii')}")
                elif kind == 'video':
                    if not self._supports_video(model_profile):
                        warnings.append(f'{spath}: 当前模型不支持原生视频输入')
                        continue
                    raw_vid = await self._read_multimodal_bytes(
                        sid, spath, max_bytes=32 * 1024 * 1024, delete_after=True,
                    )
                    media_urls.append(f"data:video/mp4;base64,{base64.b64encode(raw_vid).decode('ascii')}")
            except Exception as exc:
                warnings.append(f'{spath}: {exc}')
        body: dict[str, object] = {'ok': bool(envelope.get('ok'))}
        if envelope.get('ok'):
            body['result'] = str(envelope.get('result') or '')[:self.limits.max_output_chars]
        else:
            body['error'] = str(envelope.get('error') or '')[:self.limits.max_output_chars]
        if res.stderr.strip():
            body['logs'] = res.stderr[:4000]
        if envelope.get('delivered') is True:
            body['delivered'] = True
        if warnings:
            body['warnings'] = warnings
        text = _json(body)
        if media_urls:
            return ToolImage(text, media_urls=tuple(media_urls))
        return text

    async def execute(self, name, arguments, *, model_profile=None):
        if name == 'send_file':
            items = await self._call(self.sandbox_manager.list, self.owner)
            sandboxes = [item['sandbox_id'] for item in items]
            if not sandboxes:
                return _json({'ok': False, 'error': '请先创建沙盒并生成文件。'})
            name, arguments = 'send_file_from_sandbox', dict(sandbox_id=sandboxes[-1],
                path=arguments.get('path'), filename=arguments.get('name'))
        allowed = {
            'sandbox_create': {'runtime'}, 'sandbox_list': set(), 'sandbox_destroy': {'sandbox_id'},
            'sandbox_write_file': {'sandbox_id', 'path', 'content'}, 'sandbox_read_file': {'sandbox_id', 'path'},
            'sandbox_exec': {'sandbox_id', 'command', 'packages', 'timeout_seconds', 'background'},
            'send_file_from_sandbox': {'sandbox_id', 'path', 'filename'},
            'shell': {'command', 'timeout', 'workdir', 'background', 'title'},
            'send_image': {'path', 'filename', 'name'},
            'video': {'path', 'start', 'end'},
            'run_code': {'code', 'args', 'timeout'},
        }
        if name not in allowed:
            return None
        try:
            if not isinstance(arguments, dict) or not set(arguments) <= allowed[name]:
                raise GenerationError('invalid_tool_arguments')
            if name in {'shell', 'run_code', 'sandbox_exec'} and _BROWSER_SECRETS.search(
                    str(arguments.get('code') or arguments.get('command') or '')):
                return _json({'ok': False, 'code': 'browser_state_blocked',
                              'error': '浏览器 cookie、存储和资料目录不能读取或导出。下载媒体请直接请求页面里的媒体地址或用 page.request。'})
            await self._fence()
            if name == 'shell':
                return await self._execute_shell(arguments)
            if name == 'send_image':
                return await self._execute_send_image(arguments)
            if name == 'video':
                return await self._execute_video(arguments, model_profile=model_profile)
            if name == 'run_code':
                return await self._execute_run_code(arguments, model_profile=model_profile)
            if name == 'sandbox_create':
                sid = await self._create(arguments.get('runtime', 'python'))
                return _json({'ok': True, 'sandbox_id': sid, 'purpose': 'task'})
            if name == 'sandbox_list':
                items = await self._call(self.sandbox_manager.list, self.owner)
                visible = [item['sandbox_id'] for item in items]
                self._resources.update({(self.owner, sid): True for sid in visible})
                return _json({'ok': True, 'sandbox_ids': visible})
            sandbox_id = arguments.get('sandbox_id')
            if sandbox_id is None and name in {'sandbox_read_file', 'send_file_from_sandbox'}:
                items = await self._call(self.sandbox_manager.list, self.owner)
                if not items:
                    raise GenerationError('conversation_workspace_missing')
                sandbox_id = items[0]['sandbox_id']
            sid = await self._owned(sandbox_id)
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
            try:
                async with asyncio.timeout(300):
                    exported = await self.export_file(sid, path, arguments.get('filename') or PurePosixPath(path).name)
            except Exception as error:
                return _json({'ok': False, 'code': str(error) if isinstance(error, GenerationError) else 'file_export_failed',
                    'error': '文件尚未发送，读取或校验失败，可以重试。', 'retryable': True, 'message_accepted': False})
            return await self.send_file_content(exported.content, exported.filename,
                provenance=exported, logical_request_key=logical)
        except GenerationError as error:
            return _json({'ok': False, 'code': str(error), 'message_accepted': False})
        except Exception as error:
            frames = traceback.extract_tb(error.__traceback__)[-4:]
            logger.warning('sandbox operation=%s error=%s frames=%s', name, type(error).__name__,
                           [(frame.name, frame.lineno) for frame in frames])
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
        def validate_receipt(result):
            if (not isinstance(result, tuple) or len(result) != 2 or result[1] is not False
                    or not isinstance(result[0], bytes) or not 0 < len(result[0]) <= cap):
                raise GenerationError('invalid_export_receipt')
        content, directory = await self._call(self.sandbox_manager.export_artifact,
            self.owner, sid, path, file_only=True, max_bytes=cap, observed=validate_receipt)
        mime = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
        checks = ()
        exported = ExportedArtifact(*self._identity[:3], self.owner, sid, path, filename,
            mime, content, hashlib.sha256(content).hexdigest(), len(content), checks)
        self._exports[id(exported)] = exported
        return exported

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
            'delivery_state': registered.state, 'message_accepted': registered.message_accepted,
            'content_accepted': False, 'draft': draft, 'size': provenance.size,
            'message': '文件已发送。' if registered.message_accepted else '产物已登记，交付状态仍需实际消息回执确认。'})
