"""One host accounting scope; each physical model attempt gets its own receipt."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import inspect

from .model_usage import TokenUsage,field

_active = ContextVar('model_billing',default=None)


def active_billing():return _active.get()


@contextmanager
def billing_scope(context):
    token = _active.set(context)
    try:yield context
    finally:_active.reset(token)


async def drain(task):
    """Keep historical accounting/socket cleanup alive through repeated cancel."""
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():raise
            cancelled = True
    if cancelled:raise asyncio.CancelledError
    return result


@dataclass(frozen=True)
class BillingContext:
    store: object
    source: object
    calls: object
    current: object = None

    def request(self,profile):return RequestReceipt(self,profile)


class RequestReceipt:
    def __init__(self,context,profile):
        self.context,self.profile = context,profile
        self.call_id = None
        self.usage = TokenUsage.read(None)
        self.finished = None

    async def start(self):
        if self.context.current is not None:await self.context.current()
        async def begin():
            self.call_id = await self.context.calls(self.context.store.begin,self.context.source,self.profile)
        # Even if admission is cancelled while its DB thread commits, retain
        # the exact call_id so the caller's finally can finish it as unknown.
        await drain(asyncio.create_task(begin()))
        if self.context.current is not None:await self.context.current()

    def observe(self,usage):
        if usage is not None and self.finished is None:self.usage = TokenUsage.read(usage)

    async def finish(self):
        if self.call_id is None:return
        if self.finished is None:
            self.finished = asyncio.create_task(self.context.calls(self.context.store.finish,self.call_id,self.usage))
        await drain(self.finished)


class BilledStream:
    def __init__(self,stream,receipt):
        self.stream,self.iterator,self.receipt = stream,stream.__aiter__(),receipt
        self.closing = None

    def __aiter__(self):return self

    async def __anext__(self):
        if self.closing is not None:raise StopAsyncIteration
        try:item = await self.iterator.__anext__()
        except BaseException:
            await self.close()
            raise
        self.receipt.observe(field(item,'usage'))
        return item

    async def close(self):
        async def finish():
            try:
                close = getattr(self.stream,'aclose',None) or getattr(self.stream,'close',None)
                if callable(close):
                    result = close()
                    if inspect.isawaitable(result):await result
            finally:await self.receipt.finish()
        if self.closing is None:self.closing = asyncio.create_task(finish())
        await drain(self.closing)
