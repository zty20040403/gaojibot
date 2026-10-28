from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from .platform_types import ActorKey, EndpointKey, InboundMessage


@dataclass(frozen=True)
class LegacyTaskIdentity:
    """Explicit OneBot metadata supplied by its adapter, never inferred from openid."""

    user_id: int
    group_id: int | None
    message_id: int

    def __post_init__(self) -> None:
        if (
            type(self.user_id) is not int
            or type(self.message_id) is not int
            or (self.group_id is not None and type(self.group_id) is not int)
        ):
            raise ValueError("Legacy task identity requires integer OneBot IDs")


@dataclass(frozen=True)
class RunningTaskInfo:
    task_id: str
    conversation_id: str
    user_id: int | None
    group_id: int | None
    message_id: int | None
    summary: str
    started_at: float
    endpoint: EndpointKey | None = field(default=None, repr=False)
    actor: ActorKey | None = field(default=None, repr=False)
    native_message_id: str | None = field(default=None, repr=False)
    legacy_identity: LegacyTaskIdentity | None = field(default=None, repr=False)

    @property
    def legacy_visible(self) -> bool:
        return self.endpoint is None or self.legacy_identity is not None

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

    def register_inbound(
        self,
        *,
        conversation_id: str,
        inbound: InboundMessage,
        summary: str,
        legacy_identity: LegacyTaskIdentity | None = None,
    ) -> RunningTaskInfo:
        """Register a real endpoint/actor independently of its history mapping.

        Numeric compatibility metadata is accepted only for OneBot and must
        exactly describe this inbound message. Other platforms leave the old
        fields empty; a database principal is never substituted for a QQ ID.
        """
        if not isinstance(inbound, InboundMessage):
            raise TypeError("Structured task registration requires InboundMessage")
        if not inbound.actor.matches_endpoint(inbound.endpoint):
            raise ValueError("Task actor does not belong to the endpoint")
        if legacy_identity is not None:
            if not isinstance(legacy_identity, LegacyTaskIdentity):
                raise TypeError("Invalid legacy task identity")
            if inbound.endpoint.account.platform != "onebot-v11":
                raise ValueError("Legacy task identity is only valid for OneBot")
            native_target = (
                legacy_identity.group_id
                if inbound.endpoint.kind == "group"
                else legacy_identity.user_id
            )
            if (
                (inbound.endpoint.kind == "group" and legacy_identity.group_id is None)
                or (inbound.endpoint.kind == "private" and legacy_identity.group_id is not None)
                or inbound.endpoint.native_conversation_id != str(native_target)
                or inbound.actor.native_user_id != str(legacy_identity.user_id)
                or inbound.native_message_id != str(legacy_identity.message_id)
            ):
                raise ValueError("Legacy identity does not match the inbound message")
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("No current asyncio task to register.")
        task_id = f"t{self._next_id}"
        self._next_id += 1
        info = RunningTaskInfo(
            task_id=task_id,
            conversation_id=conversation_id,
            user_id=legacy_identity.user_id if legacy_identity else None,
            group_id=legacy_identity.group_id if legacy_identity else None,
            message_id=legacy_identity.message_id if legacy_identity else None,
            summary=" ".join(summary.split())[:80],
            started_at=time.time(),
            endpoint=inbound.endpoint,
            actor=inbound.actor,
            native_message_id=inbound.native_message_id,
            legacy_identity=legacy_identity,
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
                if info.legacy_visible and info.conversation_id == conversation_id
            ),
            key=lambda info: info.started_at,
        )

    def list_for_group(self, group_id: int) -> list[RunningTaskInfo]:
        self._discard_finished()
        return sorted(
            (
                info
                for info, _task in self._tasks.values()
                if info.legacy_visible and info.group_id == int(group_id)
            ),
            key=lambda info: info.started_at,
        )

    def list_all(self) -> list[RunningTaskInfo]:
        """Trusted administration view; callers must authorize access separately."""
        self._discard_finished()
        return sorted(
            (info for info, _task in self._tasks.values()),
            key=lambda info: info.started_at,
        )

    def list_for_endpoint(
        self,
        endpoint: EndpointKey,
        actor: ActorKey | None = None,
    ) -> list[RunningTaskInfo]:
        """List this exact endpoint, optionally narrowed to its actual actor.

        Omitting actor is an endpoint-wide host view, not an authorization grant
        for a chat participant. User commands must pass the requesting actor.
        """
        if not isinstance(endpoint, EndpointKey):
            raise TypeError("Task lookup requires EndpointKey")
        if actor is not None:
            if not isinstance(actor, ActorKey):
                raise TypeError("Task lookup requires ActorKey")
            if not actor.matches_endpoint(endpoint):
                return []
        self._discard_finished()
        return sorted(
            (
                info for info, _task in self._tasks.values()
                if info.endpoint == endpoint and (actor is None or info.actor == actor)
            ),
            key=lambda info: info.started_at,
        )

    def cancel_for_endpoint(
        self,
        endpoint: EndpointKey,
        actor: ActorKey,
        task_id: str | None = None,
    ) -> RunningTaskInfo | None:
        """Cancel only this actor's task at this endpoint, never by history key."""
        if not isinstance(actor, ActorKey):
            raise TypeError("Task cancellation requires ActorKey")
        candidates = self.list_for_endpoint(endpoint, actor)
        selected = (
            next((info for info in candidates if info.task_id == task_id), None)
            if task_id is not None else candidates[-1] if candidates else None
        )
        if selected is None:
            return None
        stored = self._tasks.get(selected.task_id)
        if stored is None:
            return None
        stored[1].cancel()
        return selected

    def push_feedback_for_endpoint(
        self,
        note: str,
        *,
        endpoint: EndpointKey,
        actor: ActorKey,
        task_id: str | None = None,
        reply_message_id: str | None = None,
    ) -> RunningTaskInfo | None:
        """Feedback is actor-scoped; an unmatched explicit selector never falls back."""
        if not isinstance(actor, ActorKey):
            raise TypeError("Task feedback requires ActorKey")
        cleaned = " ".join(note.split()).strip()
        if not cleaned:
            return None
        if reply_message_id is not None and not isinstance(reply_message_id, str):
            raise TypeError("Structured reply message ID must remain a string")
        candidates = self.list_for_endpoint(endpoint, actor)
        if task_id is not None:
            candidates = [item for item in candidates if item.task_id == task_id]
        if reply_message_id is not None:
            candidates = [item for item in candidates if item.native_message_id == reply_message_id]
        selected = candidates[-1] if candidates else None
        if selected is None:
            return None
        inbox = self._feedback.get(selected.task_id)
        if inbox is None:
            return None
        inbox.append(cleaned[:1000])
        return selected

    def drain_feedback_for_endpoint(
        self,
        endpoint: EndpointKey,
        actor: ActorKey,
        task_id: str,
    ) -> list[str]:
        if not isinstance(actor, ActorKey):
            raise TypeError("Task feedback requires ActorKey")
        if not any(info.task_id == task_id for info in self.list_for_endpoint(endpoint, actor)):
            return []
        return self.drain_feedback(task_id)

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
