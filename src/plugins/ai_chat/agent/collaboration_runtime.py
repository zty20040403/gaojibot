"""Fresh membership and per-tool revocation checks for collaborative revisions."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
import time

from nonebot.adapters.onebot.v11 import GroupMessageEvent

from .contracts import ContextPacket
from ..onebot_codec import scope_from_event
from ..workers.durable_jobs import JobDeferred


class CollaborationRevoked(BaseException):
    """Escape ordinary tool/model retries when a shared execution loses permission."""


class CollaborationUnavailable(BaseException):
    """Unknown membership pauses the owned job instead of authorizing or retrying tools."""


active_collaboration_guard: ContextVar[object | None] = ContextVar("collaboration_guard", default=None)


async def guard_collaboration():
    guard = active_collaboration_guard.get()
    if guard is not None:
        await guard.check()


async def group_roster(bot, event):
    if not isinstance(event, GroupMessageEvent) or str(bot.self_id) != str(event.self_id):
        raise ValueError("Collaboration requires the original group and QQ account")
    try:
        async with asyncio.timeout(10):
            rows = await bot.get_group_member_list(group_id=event.group_id, no_cache=True)
        if not isinstance(rows, list) or any(not isinstance(row, dict) or
                not str(row.get("user_id", "")).isdigit() for row in rows):
            raise ValueError("Invalid group roster")
    except Exception:
        raise JobDeferred("暂时无法核实协作者群成员身份，未执行操作", 30) from None
    return {int(row["user_id"]): row for row in rows}


def execution_event(store, task, control):
    value = control["dispatch"].get("execution")
    if value is None:
        return None
    event = GroupMessageEvent.model_validate(value["event"])
    packet = ContextPacket.from_payload(value["context_packet"])
    actor = store.revision_actor(task.task_id)
    if (value.get("revision") != control["revision"] or event.user_id != actor
            or str(event.self_id) != str(control["dispatch"]["bot_id"])
            or scope_from_event(event).key != task.scope_key or packet.scope_key != task.scope_key
            or packet.requester_user_id != actor
            or packet.conversation_id != f"group:{event.group_id}:user:{actor}"):
        raise ValueError("Task execution envelope does not match this revision")
    return event


class CollaborationGuard:
    def __init__(self, app, bot, task, event, revision):
        self.app, self.bot, self.task, self.event = app, bot, task, event
        self.revision = revision
        self.checked_at = None

    async def check(self):
        store = self.app.subagent_store
        try:
            await self.app.db_call(store.require_execution_actor, self.task.task_id, self.event.user_id)
            control = await self.app.db_call(store.control, self.task.task_id)
            if control["revision"] != self.revision:
                raise ValueError("Task revision changed")
        except (ValueError, PermissionError) as exc:
            raise CollaborationRevoked(str(exc)) from exc
        if self.checked_at is not None and time.monotonic() - self.checked_at < 30:
            return
        try:
            members = await group_roster(self.bot, self.event)
        except JobDeferred as exc:
            raise CollaborationUnavailable(str(exc)) from exc
        if self.event.user_id not in members or self.task.requester_user_id not in members:
            raise CollaborationRevoked("任务发起人或本版协作者已不在原群，停止执行")
        self.checked_at = time.monotonic()
