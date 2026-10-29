"""Synchronous official OCR over the existing VisionWorker model path.

No vision job or auto-delivery is created. The host's active model budget must
already carry the original execution/project/route guard on every provider
attempt. The adapter additionally revalidates the exact media before dispatch
and while waiting; cancelling cannot undo a request already sent to a provider.
"""
import asyncio
import copy
from contextlib import suppress

from src.plugins.ai_chat.model_budget import active_model_budget
from src.plugins.ai_chat.vision_worker import VisionJob, VisionWorker
from .media_refs import ExternalText, MediaDenied, bounded_text


async def guarded_wait(awaitable, current, *, timeout=45, interval=0.2):
    task = asyncio.create_task(awaitable)
    try:
        async with asyncio.timeout(timeout):
            while True:
                await current()
                done, _ = await asyncio.wait({task}, timeout=interval)
                if done:
                    result = task.result()
                    await current()
                    return result
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


class _GuardedGateway:
    def __init__(self, gateway, current):
        self.gateway, self.current = gateway, current

    async def create_completion(self, *args, **kwargs):
        await self.current()
        return await self.gateway.create_completion(*args, **kwargs)


class VisionAdapter:
    def __init__(self, worker, *, timeout=45):
        if not isinstance(worker, VisionWorker) or type(timeout) is not int or not 1 <= timeout <= 60:
            raise MediaDenied('vision_unavailable')
        self.worker, self.timeout = worker, timeout

    async def recognize(self, source, *, resolver, fetcher):
        if getattr(source.node, 'media_kind', None) not in {'image', 'sticker'}:
            raise MediaDenied('image_reference_required')
        budget = active_model_budget()
        if budget is None or not callable(budget.before_request):
            raise MediaDenied('guarded_model_budget_required')
        current = lambda: resolver.require(source)
        await current()
        downloaded = await fetcher.fetch(source, resolver=resolver)
        # Use byte magic, never a remote MIME or filename to select a decoder.
        mime = self.worker._sniff_mime(downloaded.content)
        if mime not in {'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/bmp'}:
            raise MediaDenied('image_format_unavailable')
        try:
            async with asyncio.timeout(self.timeout):
                content, mime = await guarded_wait(asyncio.to_thread(
                    self.worker._prepare_for_vision, downloaded.content, mime), current,
                    timeout=self.timeout)
                if not content or len(content) > self.worker.max_vision_bytes:
                    raise MediaDenied('image_preparation_limit')
                # Copy only the call facade: no shared gateway mutation or global
                # cross-account cache. All parsing/prompts/model routing remain
                # the existing VisionWorker implementation.
                worker = copy.copy(self.worker)
                worker.llm_gateway = _GuardedGateway(worker.llm_gateway, current)
                job = VisionJob(0, '', source.ref.segment_index, '', 'detail',
                                '逐字识别可辨认文字；没有清晰文字则 text 为空。', 0)
                result = await guarded_wait(worker._request_analysis(job, content, mime),
                                            current, timeout=self.timeout)
                text, truncated = bounded_text(result.extracted_text)
                return ExternalText('vision_ocr', text, truncated)
        except (MediaDenied, asyncio.CancelledError):
            raise
        except (RuntimeError, OSError, ValueError, TimeoutError):
            raise MediaDenied('vision_unavailable') from None
