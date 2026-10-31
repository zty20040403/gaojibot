"""Default-off media factory over the actual M4 execution and M3/M6 ports."""
from contextlib import nullcontext
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat

from src.plugins.ai_chat.message_ir import CardNode, ForwardNode, MediaNode, TextNode
from src.plugins.ai_chat.model_budget import ModelCallBudget, active_model_budget, model_budget_scope
from .content_reader import PinnedContentReader, read_text_document
from .file_artifacts import foreground_source, current_source
from .forward_render import forward_text, render_forward
from .image_formats import detect_image, MAX_IMAGE_BYTES
from .local_asr import LocalASR, LocalASRConfig
from .media_refs import LedgerMediaResolver, MediaRef, MediaDenied, transcribe, text_link
from .media_budget import OCRUsage
from .vision_adapter import VisionAdapter


@dataclass(frozen=True)
class MediaConfig:
    ocr: bool = False
    asr: bool = False
    local_asr: bool = False
    documents: bool = False
    forwards: bool = False
    stickers: bool = False
    emoji: bool = False  # Accepted for old host documents; QQ faces cannot be sent.
    max_download_bytes: int = 2 * 1024 * 1024
    timeout_seconds: int = 45
    model_max_tokens: int = 65536

    def __post_init__(self):
        if (any(type(getattr(self, name)) is not bool for name in (
                'ocr','asr','local_asr','documents','forwards','stickers','emoji'))
                or any(type(value) is not int or not low <= value <= high for value, low, high in (
                    (self.max_download_bytes,1,2*1024*1024), (self.timeout_seconds,1,60),
                    (self.model_max_tokens,1,1000000))) or self.local_asr and not self.asr):
            raise ValueError('invalid_official_media_configuration')


class OfficialMediaService:
    def __init__(self, runtime, token, app, config):
        if not isinstance(config, MediaConfig) or token is not runtime._token:
            raise MediaDenied('media_runtime_unavailable')
        self.runtime, self.token, self.app, self.config = runtime, token, app, config
        self.closed = False
        self.fetcher = PinnedContentReader(max_bytes=config.max_download_bytes,
                                          timeout=min(config.timeout_seconds, 30))
        worker = getattr(app, 'vision_worker', None)
        self.vision = VisionAdapter(worker, timeout=config.timeout_seconds) if config.ocr and worker is not None and getattr(app,'usage_store',None) is not None else None
        self.asr = None
        if config.local_asr:
            settings = app.settings
            model = getattr(settings, 'video_whisper_model_path', '')
            if model and Path(model).is_absolute():
                # Same configured model/threads and executable defaults as
                # DeepVideoAnalyzer; no invented NapCat or package fallback.
                cfg = LocalASRConfig(Path(model),
                    max_bytes=config.max_download_bytes, timeout=config.timeout_seconds,
                    threads=min(getattr(settings,'video_whisper_threads',2),8))
                if cfg.available():
                    self.asr = LocalASR(cfg)

    async def close(self):
        self.closed = True

    async def foreground(self, inbound, platform):
        if (platform.execution.token is not self.token or platform.inbound != inbound
                or platform.account.key != self.token.account_key):
            raise MediaDenied('media_runtime_source_mismatch')
        session = MediaSession(self, inbound, platform)
        row = await session.current()
        session.deadline = row['deadline_at']
        session.image_available = await runtime_call(session, session._route_ready, 'image/png')
        session.file_available = await runtime_call(session, session._route_ready, 'application/zip')
        return session


class MediaSession:
    def __init__(self, service, inbound, platform):
        self.service, self.inbound, self.platform = service, inbound, platform
        self.pending_text = []
        self.voice_sent = False
        self.deadline = None
        self.image_available = self.file_available = False
        self.resolver = LedgerMediaResolver(endpoint=inbound.endpoint, requester=inbound.actor,
            ledger=service.app.message_ledger, db_call=service.runtime._calls,
            current=self.current_actor)

    def _current(self):
        service, platform = self.service, self.platform
        if (service.closed or service.runtime._stopping or service.runtime._token is not service.token):
            raise MediaDenied('media_runtime_unavailable')
        with service.runtime.deliveries.transaction() as cursor:
            now = int(service.runtime.store.clock())
            row = platform.execution.current(cursor, now)
            self._check_row(row, int(service.runtime.store.clock()))
            return dict(row)

    def _check_row(self, row, now):
        if (row['state'] != 'executing' or row['deadline_at'] <= now
                or self.deadline is not None and row['deadline_at'] != self.deadline):
            raise MediaDenied('media_execution_unavailable')

    async def current(self):
        return await self.service.runtime._calls(self._current)

    async def current_actor(self, endpoint, actor):
        if (endpoint, actor) != (self.inbound.endpoint, self.inbound.actor):
            raise MediaDenied('media_runtime_source_mismatch')
        await self.current()
        return True

    def enabled(self, name):
        cfg = self.service.config
        return bool({
            'read_image_text': self.service.vision is not None,
            'transcribe_voice': cfg.asr,
            'read_attachment': cfg.documents,
            'view_forward': cfg.forwards,
            'send_forward_summary': cfg.forwards,
            'send_sticker': cfg.stickers and self.service.runtime.file_service is not None
                            and getattr(self.service.app,'media_library',None) is not None and self.image_available,
            'reply_with_voice': self.service.runtime.file_service is not None and self.file_available,
        }.get(name, False))

    def _route_ready(self, mime):
        files = self.service.runtime.file_service
        if files is None:
            return False
        with files.transaction() as cursor:
            row = self.platform.execution.current(cursor,int(files.clock()))
            probe = {'endpoint_key':self.inbound.endpoint.key,'requester_actor_key':self.inbound.actor.key,
                'mime':mime,'byte_size':1,'draft':False,'endpoint_generation':row['endpoint_generation'],
                'route_epoch':row['route_epoch']}
            return files._route(cursor,probe,'native') is True or (
                mime=='application/zip' and files._route(cursor,probe,'link') is True)

    async def source(self, arguments, kind):
        row = await self.current()
        handle = arguments.get('message_handle')
        if handle is None:
            canonical = row['canonical_message_id']
        elif isinstance(handle, str) and re.fullmatch(r'msg#[1-9][0-9]{0,18}', handle):
            canonical = int(handle[4:])
        else:
            raise MediaDenied('invalid_media_reference')
        index = arguments.get('segment_index')
        message = await self.service.runtime._calls(self.service.app.message_ledger.get_in_scope,
                                                   self.inbound.endpoint.scope, canonical)
        if message is None:
            raise MediaDenied('media_reference_unavailable')
        if index is None:
            candidates = [n.segment_index for n in message.body.nodes if (
                isinstance(n,MediaNode) and n.media_kind in kind or
                isinstance(n,ForwardNode) and 'forward' in kind or
                isinstance(n,CardNode) and 'card' in kind or
                isinstance(n,TextNode) and 'link' in kind and text_link(n) is not None)]
            if not candidates:
                raise MediaDenied('media_reference_unavailable')
            index = candidates[0]
        source = await self.resolver.resolve(MediaRef(canonical, index))
        node = source.node
        if not (isinstance(node,MediaNode) and node.media_kind in kind or
                isinstance(node,ForwardNode) and 'forward' in kind or
                isinstance(node,CardNode) and 'card' in kind or
                isinstance(node,TextNode) and 'link' in kind and text_link(node) is not None):
            raise MediaDenied('media_reference_unavailable')
        return source

    async def ocr(self, arguments):
        source = await self.source(arguments, {'image','sticker'})
        parent = active_model_budget()
        if parent is not None and (not callable(parent.before_request)
                                  or not callable(parent.reserve_request)):
            raise MediaDenied('guarded_model_budget_required')
        usage = OCRUsage(self, source)
        scope = (nullcontext() if parent is not None else model_budget_scope(ModelCallBudget(
            self.service.config.model_max_tokens, max_requests=1,
            before_request=usage.current, reserve_request=usage.reserve)))
        try:
            with scope:
                return await self.service.vision.recognize(source, resolver=self.resolver, fetcher=self.service.fetcher)
        finally:
            await usage.settle()

    async def voice(self, arguments):
        source = await self.source(arguments, {'audio'})
        return await transcribe(source, resolver=self.resolver, fetcher=self.service.fetcher, local_asr=self.service.asr)

    async def reply_with_voice(self, arguments):
        if self.voice_sent:
            return {'state': 'skipped', 'message': '这次回复已经生成一条语音。'}
        from ...voice_reply import synthesize
        await self.current()
        content = await synthesize(arguments['text'])
        record = await self.register_bytes(content, 'reply.silk', 'voice-reply')
        self.voice_sent = True
        outcome = await self.service.runtime.file_service.dispatcher.send_artifact(record.artifact_id)
        return {'state': outcome.status, 'message_accepted': outcome.status == 'committed',
                'artifact_id': record.artifact_id}

    async def document(self, arguments):
        source = await self.source(arguments, {'file','card','link'})
        data = await self.service.fetcher.fetch(source, resolver=self.resolver)
        result = read_text_document(data)
        await self.resolver.require(source)
        return result

    async def forward(self, arguments, *, send=False):
        source = await self.source(arguments, {'forward'})
        if not send:
            text, truncated = forward_text(source.node)
            return {'text':text,'truncated':truncated,'trust':'external_content'}
        await self.resolver.require(source)
        files = self.service.runtime.file_service
        result = render_forward(source.node, self.inbound.endpoint, file_available=files is not None and self.file_available)
        if result.content is not None:
            record = await self.register_bytes(result.content, result.filename,
                'forward:' + str(source.ref.canonical_id) + ':' + str(source.ref.segment_index))
            return {'state':record.delivery_state,'artifact_id':record.artifact_id,'message':'转发文件已登记，尚未确认平台接收。'}
        self.pending_text.append(result.plan.parts[0].canonical_text)
        return {'state':'prepared_content','message':'有限转发摘要将在本轮最终回复发送。'}

    def _file_source(self):
        self._current()
        runtime, platform = self.service.runtime, self.platform
        files = runtime.file_service
        if files is None:
            raise MediaDenied('file_runtime_unavailable')
        with runtime.deliveries.transaction() as cursor:
            now = int(runtime.store.clock())
            row = platform.execution.current(cursor, now)
            return foreground_source(cursor, token=self.service.token, handle=platform.execution.handle,
                job=platform.execution.job, admission=platform.execution.admission, now=now,
                pending_deadline_at=row['deadline_at'])

    async def register_bytes(self, content, filename, logical):
        source = await self.service.runtime._calls(self._file_source)
        key = hashlib.sha256(('official-media:' + source.source_key + ':' + logical).encode()).hexdigest()
        return await self.service.runtime._calls(self.service.runtime.file_service.artifacts.register,
            source, content=content, filename=filename, logical_request_key=key)

    async def sticker(self, arguments, observed_handles):
        handle = arguments.get('media_handle')
        if (not isinstance(handle,str) or re.fullmatch(r'media#[1-9][0-9]{0,18}',handle) is None
                or handle not in observed_handles):
            raise MediaDenied('sticker_reference_unavailable')
        await self.current()
        source = await self.service.runtime._calls(self._file_source)
        def read():
            library = self.service.app.media_library
            connection = library.database.store_connection()
            try:
                row = connection.execute('''SELECT b.sha256,b.byte_size,b.mime_type FROM media_blobs b
                    JOIN media_analysis a USING(media_id) JOIN sticker_library s USING(media_id)
                    WHERE media_id=? AND a.safety='safe' AND a.is_sticker=1 AND s.enabled=1 AND s.banned=0''',
                    (int(handle[6:]),)).fetchone()
            finally:
                connection.close()
            if (row is None or row['mime_type'] not in {'image/png','image/jpeg'}
                    or not 1<=row['byte_size']<=MAX_IMAGE_BYTES):
                raise MediaDenied('sticker_content_unavailable')
            files = self.service.runtime.file_service
            with files.transaction() as cursor:
                actual = current_source(cursor,source,int(files.clock()))
                probe = {**actual,'account_key':source.endpoint.account.key,'endpoint_key':source.endpoint.key,
                    'requester_actor_key':source.actor.key,'mime':row['mime_type'],
                    'byte_size':row['byte_size'],'draft':False}
                if files._route(cursor,probe,'native') is not True:
                    raise MediaDenied('native_image_route_unavailable')
            record = library.get_sticker(int(handle[6:]))
            if record is None or record.handle != handle or record.mime_type != row['mime_type']:
                raise MediaDenied('sticker_content_unavailable')
            content = _read_library_bytes(library.root,record.storage_path,row['byte_size'],row['sha256'])
            filename = 'sticker.png' if row['mime_type']=='image/png' else 'sticker.jpg'
            if detect_image(content,filename)!=row['mime_type']:
                raise MediaDenied('sticker_content_unavailable')
            return content,filename
        content,filename = await self.service.runtime._calls(read)
        await self.current()
        record = await self.register_bytes(content,filename,'sticker:'+handle)
        return {'state':record.delivery_state,'artifact_id':record.artifact_id,
                'message':'贴纸已进入受控图片投递，尚未确认平台接收。'}

    def finish(self, answer):
        # These are bounded original texts; final M3 rendering escapes all native
        # markup. No tool directly sends an extra message or spends a window.
        return '\n\n'.join([answer,*self.pending_text])


def _read_library_bytes(root,path,size,digest):
    descriptors=[]
    try:
        relative=Path(path).relative_to(Path(root))
        if not relative.parts or any(part in {'','..','.'} for part in relative.parts):
            raise ValueError
        fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);descriptors.append(fd)
        for part in relative.parts[:-1]:
            fd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);descriptors.append(fd)
        fd=os.open(relative.name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd);descriptors.append(fd)
        before=os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size!=size or size>MAX_IMAGE_BYTES:
            raise ValueError
        content=bytearray()
        while chunk:=os.read(fd,min(65536,size+1-len(content))):
            content.extend(chunk)
            if len(content)>size:raise ValueError
        after=os.fstat(fd)
        if (len(content)!=size or (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=
                (after.st_size,after.st_mtime_ns,after.st_ctime_ns) or hashlib.sha256(content).hexdigest()!=digest):
            raise ValueError
        return bytes(content)
    except (OSError,ValueError):
        raise MediaDenied('sticker_bytes_unavailable') from None
    finally:
        for fd in reversed(descriptors):os.close(fd)


async def runtime_call(session, function, *args):
    return await session.service.runtime._calls(function,*args)
