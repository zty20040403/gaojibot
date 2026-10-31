"""Wait for the rest of an unfinished message without blocking the gateway."""
import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
import time


def unfinished(text):
    text = text.rstrip()
    return not text or text.endswith(('，', '、', '；', '：', ',', ';', ':',
        '但是', '然后', '对了', '就是', '所以', '而且', '还有', '那个', '因为'))


@dataclass
class Batch:
    source: object
    started: float
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    messages: list = field(default_factory=list)
    last_at: float = 0


class MessageBatcher:
    def __init__(self, dispatch, *, idle=10, maximum=20, clock=time.monotonic):
        self.dispatch, self.idle, self.maximum, self.clock = dispatch, idle, maximum, clock
        self.pending, self.tasks = {}, set()
        self.recent = defaultdict(lambda: deque(maxlen=3))

    def add(self, key, source, text, *, requested, canonical_id):
        now = self.clock()
        recent = self.recent[key]
        recent.append(now)
        if text.lstrip().startswith(('/', '!')):
            return False
        batch = self.pending.get(key)
        if batch is None:
            burst = len(recent) == 3 and now - recent[0] <= 5
            if not requested or not (unfinished(text) or burst):
                return False
            batch = self.pending[key] = Batch(source, now)
            task = asyncio.create_task(self._wait(key, batch))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        batch.messages.append((canonical_id, text))
        batch.last_at = now
        batch.changed.set()
        return True

    async def _wait(self, key, batch):
        while True:
            delay = min(batch.last_at + self.idle, batch.started + self.maximum) - self.clock()
            if delay <= 0:
                break
            batch.changed.clear()
            try:
                await asyncio.wait_for(batch.changed.wait(), delay)
            except TimeoutError:
                break
        del self.pending[key]
        if len(batch.messages) > 1:
            batch.source.payload['batch_text'] = '[连续消息，请合起来回答一次]\n' + '\n'.join(
                f'[msg#{canonical}] {text}' for canonical, text in batch.messages)
        await self.dispatch(batch.source)

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.pending.clear()
