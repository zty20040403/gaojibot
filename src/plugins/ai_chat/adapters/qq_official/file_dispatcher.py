"""Read one owned artifact, upload it, and send through the conversation queue."""
import asyncio
import logging
import traceback

from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from .file_upload import FileUploadError

logger = logging.getLogger(__name__)


class OfficialFileDispatcher:
    def __init__(self, service):
        self.service = service
        self._locks = {}

    def _load(self, artifact_id):
        service = self.service
        with service.transaction() as cursor:
            service._account(cursor)
            artifact = cursor.execute('SELECT * FROM qq_official_file_artifacts WHERE artifact_id=? AND account_key=?',
                                      (artifact_id, service.token.account_key)).fetchone()
            if artifact is None:
                raise FileUploadError('文件不存在。')
            if not service.admission(cursor, EndpointKey.parse(artifact['endpoint_key']),
                                      ActorKey.parse(artifact['requester_actor_key'])):
                raise FileUploadError('当前会话没有文件发送权限。')
            delivery = cursor.execute('SELECT * FROM qq_official_file_deliveries WHERE artifact_id=?',
                                      (artifact_id,)).fetchone()
            return dict(artifact), dict(delivery)

    def _queue(self, artifact, uploaded):
        service = self.service
        with service.transaction() as cursor:
            service._account(cursor)
            ref = service.runtime.outbox.enqueue_file(service.token, artifact=artifact,
                uploaded=uploaded, transaction_cursor=cursor)
            cursor.execute("""UPDATE qq_official_file_deliveries SET state='handed_off',
                route='native',active_plan_id=?,reason_code='',updated_at=? WHERE artifact_id=?""",
                (ref.plan_id, int(service.clock()), artifact['artifact_id']))
            return ref.delivery_ids[0]

    def _delivery_id(self, plan_id):
        with self.service.transaction() as cursor:
            return cursor.execute('SELECT delivery_id FROM deliveries WHERE plan_id=? ORDER BY part_index LIMIT 1',
                                  (plan_id,)).fetchone()['delivery_id']

    async def send_artifact(self, artifact_id):
        service = self.service
        lock = self._locks.setdefault(artifact_id, asyncio.Lock())
        async with lock:
            artifact, previous = await service.calls(self._load, artifact_id)
            if previous['active_plan_id']:
                delivery_id = await service.calls(self._delivery_id, previous['active_plan_id'])
            else:
                content = await service.calls(service.artifacts.read_verified, artifact_id,
                    sha256=artifact['sha256'], byte_size=artifact['byte_size'])
                uploaded = await service.uploader.upload(EndpointKey.parse(artifact['endpoint_key']),
                                                         content, artifact['filename'])
                delivery_id = await service.calls(self._queue, artifact, uploaded)
            outcome = await service.runtime.sender.send_delivery(delivery_id)
            if outcome.status == 'failed':
                raise FileUploadError('文件消息发送失败：' + outcome.reason)
            if outcome.status == 'ambiguous':
                raise FileUploadError('文件已上传，但消息可能没发出去；请先查看群里的文件，避免重复发送。')
            return outcome

    async def __call__(self, job):
        try:
            outcome = await self.send_artifact(job.payload['artifact_id'])
            return {'status': outcome.status}
        except FileUploadError as error:
            await self._notify(job.payload['artifact_id'], str(error))
            return {'status': 'failed', 'reason': str(error)}
        except Exception as error:
            frames = traceback.extract_tb(error.__traceback__)
            logger.error('QQ file delivery failed error=%s at=%s', type(error).__name__,
                         ' > '.join(f'{frame.name}:{frame.lineno}' for frame in frames[-4:]))
            await self._notify(job.payload['artifact_id'], '文件处理失败，还没有确认送达，请重试。')
            return {'status': 'failed', 'reason': type(error).__name__}

    async def _notify(self, artifact_id, message):
        from src.plugins.ai_chat.message_ir import MessageBody, TextNode
        from src.plugins.ai_chat.platform_types import ActorKey
        from .renderer import render_plan
        service = self.service
        def queue():
            artifact, _ = self._load(artifact_id)
            with service.transaction() as cursor:
                cursor.execute("""UPDATE qq_official_file_deliveries SET state='failed',
                    reason_code='upload_failed',updated_at=? WHERE artifact_id=? AND active_plan_id IS NULL""",
                    (int(service.clock()), artifact_id))
            return service.runtime.outbox.enqueue(service.token,
                rendered=render_plan(MessageBody((TextNode(0, message),)), EndpointKey.parse(artifact['endpoint_key'])),
                requester=ActorKey.parse(artifact['requester_actor_key']), logical_key='file-error:' + artifact_id,
                source_scope_key=artifact['source_scope_key'],
                source_canonical_message_id=artifact['source_canonical_message_id'],
                source_native_message_id=artifact['source_native_message_id'] or '',
                expires_at=artifact['pending_deadline_at'], purpose='progress')
        ref = await service.calls(queue)
        for delivery_id in ref.delivery_ids:
            await service.runtime.sender.send_delivery(delivery_id)
