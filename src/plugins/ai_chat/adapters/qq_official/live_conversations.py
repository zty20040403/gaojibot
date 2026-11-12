"""In-memory cancellation only; PostgreSQL generations remain authoritative."""
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class Work:
    endpoint: str
    generation: int
    kind: str
    job_id: int | None


class ConversationTasks:
    def __init__(self):
        self.active = {}

    @contextmanager
    def track(self, endpoint, generation, *, kind='turn', job_id=None):
        task = asyncio.current_task()
        previous = self.active.get(task)
        self.active[task] = Work(endpoint,generation,kind,job_id)
        try:
            yield
        finally:
            if previous is None:
                self.active.pop(task,None)
            else:
                self.active[task] = previous

    def interrupt(self, endpoint, *, before_generation=None, kind=None):
        for task,work in tuple(self.active.items()):
            if (work.endpoint==endpoint and (kind is None or work.kind==kind)
                    and (before_generation is None or work.generation<before_generation)
                    and not task.cancelling()):
                task.cancel()

    def cancel_jobs(self, jobs):
        for task,work in tuple(self.active.items()):
            if work.job_id in jobs and not task.cancelling():
                task.cancel()
