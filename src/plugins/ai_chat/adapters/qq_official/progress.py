"""Progress messages use the same sender as final answers."""
from dataclasses import dataclass
import logging
from ...message_ir import MessageBody, TextNode
from ...platform_types import ActorKey, EndpointKey
from .send_types import digest, stable_json
from .renderer import render_plan

@dataclass(frozen=True)
class ProgressContext:
    source_key: str
    endpoint: EndpointKey
    actor: ActorKey
    scope_key: str
    deadline_at: int
    native_message_id: str = ""
    canonical_message_id: int | None = None


class ProgressEmitter:
    def __init__(self, *, token, outbox, current, database_calls, wake=None, reserve_final_slots=4):
        self.token, self.outbox, self.current = token, outbox, current
        self.database_calls, self.wake = database_calls, wake

    def _queue(self, text):
        now = int(self.outbox.clock())
        with self.outbox.deliveries.transaction() as cursor:
            context = self.current(cursor, now)
            ref = self.outbox.enqueue(self.token,
                rendered=render_plan(MessageBody((TextNode(0, text),)), context.endpoint, max_chars=200, max_parts=1),
                requester=context.actor, logical_key='qq-progress:'+digest(stable_json([context.source_key,text])),
                source_scope_key=context.scope_key, source_native_message_id=context.native_message_id,
                source_canonical_message_id=context.canonical_message_id, purpose='progress',
                expires_at=context.deadline_at, transaction_cursor=cursor)
            return {'ok':True, 'status':'enqueued' if ref.created else 'already_reported'}

    async def say(self, text):
        if not isinstance(text, str) or not text.strip():
            return {'ok':False, 'reason':'progress_text_empty'}
        try:
            result = await self.database_calls(self._queue, text.strip()[:200])
        except Exception as error:
            logging.getLogger(__name__).warning('QQ progress enqueue failed: %s', type(error).__name__)
            return {'ok':False, 'reason':'progress_unavailable'}
        if self.wake is not None:
            self.wake()
        return result

    async def report(self, text):
        await self.say(text)
