from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class RunningTaskInfo:
    task_id: str
    conversation_id: str
    user_id: int
    group_id: int | None
    message_id: int
    summary: str
    started_at: float

    @property
    def elapsed_seconds(self) -> int:
        return max(int(time.time() - self.started_at), 0)


class RunningTaskRegistry:
    def __init__(self) -> None:
        self._next_id = 1
        self._tasks: dict[str, tuple[RunningTaskInfo, asyncio.Task[object]]] = {}
        self._feedback: dict[str, list[str]] = {}

    def register_current(
        self,
        *,
        conversation_id: str,
        user_id: int,
        group_id: int | None,
        message_id: int,
        summary: str,
    ) -> RunningTaskInfo:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("No current asyncio task to register.")
        task_id = f"t{self._next_id}"
        self._next_id += 1
        info = RunningTaskInfo(
            task_id=task_id,
            conversation_id=conversation_id,
            user_id=user_id,
            group_id=group_id,
            message_id=message_id,
            summary=" ".join(summary.split())[:80],
            started_at=time.time(),
        )
        self._tasks[task_id] = (info, task)
        self._feedback[task_id] = []
        return info

    def finish(self, task_id: str) -> None:
        self._tasks.pop(task_id, None)
        self._feedback.pop(task_id, None)

    def list_for(self, conversation_id: str) -> list[RunningTaskInfo]:
        self._discard_finished()
        return sorted(
            (
                info
                for info, _task in self._tasks.values()
                if info.conversation_id == conversation_id
            ),
            key=lambda info: info.started_at,
        )

    def list_for_group(self, group_id: int) -> list[RunningTaskInfo]:
        self._discard_finished()
        return sorted(
            (
                info
                for info, _task in self._tasks.values()
                if info.group_id == int(group_id)
            ),
            key=lambda info: info.started_at,
        )

    def list_all(self) -> list[RunningTaskInfo]:
        self._discard_finished()
        return sorted(
            (info for info, _task in self._tasks.values()),
            key=lambda info: info.started_at,
        )

    def push_feedback(
        self,
        note: str,
        *,
        conversation_id: str | None = None,
        group_id: int | None = None,
        task_id: str | None = None,
        reply_message_id: int | None = None,
    ) -> RunningTaskInfo | None:
        cleaned = " ".join(note.split()).strip()
        if not cleaned:
            return None
        candidates = (
            self.list_for_group(group_id)
            if group_id is not None
            else self.list_for(conversation_id or "")
        )
        selected: RunningTaskInfo | None = None
        if task_id:
            selected = next(
                (item for item in candidates if item.task_id == task_id),
                None,
            )
        elif reply_message_id is not None:
            selected = next(
                (
                    item
                    for item in reversed(candidates)
                    if item.message_id == int(reply_message_id)
                ),
                None,
            )
        if selected is None and candidates:
            selected = candidates[-1]
        if selected is None:
            return None
        inbox = self._feedback.get(selected.task_id)
        if inbox is None:
            return None
        inbox.append(cleaned[:1000])
        return selected

    def drain_feedback(self, task_id: str) -> list[str]:
        inbox = self._feedback.get(task_id)
        if not inbox:
            return []
        notes = list(inbox)
        inbox.clear()
        return notes

    def cancel(
        self,
        conversation_id: str,
        task_id: str | None = None,
    ) -> RunningTaskInfo | None:
        candidates = self.list_for(conversation_id)
        if task_id:
            selected = next(
                (info for info in candidates if info.task_id == task_id),
                None,
            )
        else:
            selected = candidates[-1] if candidates else None
        if selected is None:
            return None

        stored = self._tasks.get(selected.task_id)
        if stored is None:
            return None
        _info, task = stored
        task.cancel()
        return selected

    def cancel_for_group(
        self,
        group_id: int,
        task_id: str | None = None,
    ) -> RunningTaskInfo | None:
        candidates = self.list_for_group(group_id)
        if task_id:
            selected = next(
                (info for info in candidates if info.task_id == task_id),
                None,
            )
        else:
            selected = candidates[-1] if candidates else None
        if selected is None:
            return None
        stored = self._tasks.get(selected.task_id)
        if stored is None:
            return None
        _info, task = stored
        task.cancel()
        return selected

    def cancel_any(self, task_id: str) -> RunningTaskInfo | None:
        self._discard_finished()
        stored = self._tasks.get(task_id)
        if stored is None:
            return None
        info, task = stored
        task.cancel()
        return info

    def cancel_all(self) -> int:
        self._discard_finished()
        count = 0
        for _info, task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
                count += 1
        return count

    async def cancel_all_and_wait(self) -> int:
        current = asyncio.current_task()
        active = {
            task
            for _info, task in self._tasks.values()
            if task is not current and not task.done()
        }
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        self._discard_finished()
        return len(active)

    def _discard_finished(self) -> None:
        finished_ids = [
            task_id
            for task_id, (_info, task) in self._tasks.items()
            if task.done()
        ]
        for task_id in finished_ids:
            self._tasks.pop(task_id, None)
            self._feedback.pop(task_id, None)
