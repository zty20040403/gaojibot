"""Actual-endpoint memory commands, fenced by the host's current execution."""
from __future__ import annotations

import inspect

from ...application.commands import CommandResult
from ...application.memory_commands import format_audit, format_entries, label, parse_memory, MemorySyntax
from ...long_term_memory import LongTermMemoryError
from ...memory_scopes import memory_scopes_for_inbound, group_memory_scope
from ...platform_types import InboundMessage
from ...tools.memory import _looks_like_secret
from .commands import CommandEffectDenied


class MemoryCommands:
    def __init__(self, *, store, inbound, effect_scope, database_call,
                 group_write_authority=None, provenance=None):
        if (not isinstance(inbound, InboundMessage) or inbound.endpoint.account.platform != 'qq-official'
                or inbound.actor.identity_kind == 'bot' or not inbound.actor.matches_endpoint(inbound.endpoint)):
            raise ValueError('memory_command_source_invalid')
        self.store, self.inbound, self.effect_scope = store, inbound, effect_scope
        self.calls, self.group_write_authority, self.provenance = database_call, group_write_authority, provenance

    async def __call__(self, inbound, parsed):
        if inbound is not self.inbound:
            raise CommandEffectDenied('memory_command_source_changed')
        try:
            request = parse_memory(parsed.argument)
        except MemorySyntax as exc:
            return CommandResult('syntax', str(exc))
        def run():
            with self.effect_scope(inbound) as cursor:
                return self._execute(cursor, request)
        try:
            result = self.calls(run)
            return await result if inspect.isawaitable(result) else result
        except LongTermMemoryError as exc:
            return CommandResult('syntax', str(exc))

    def _group_allowed(self, cursor, group):
        # A directed/canonical history view never grants edits to the source.
        if (self.inbound.endpoint.kind != 'group'
                or group != group_memory_scope(self.inbound.endpoint.scope)
                or self.group_write_authority is None):
            return False
        value = self.group_write_authority(cursor, self.inbound)
        if inspect.isawaitable(value):
            if inspect.iscoroutine(value): value.close()
            raise CommandEffectDenied('memory_authority_must_be_synchronous')
        return value is True

    def _audit(self, cursor):
        principal, message = (0, None) if self.provenance is None else self.provenance(cursor, self.inbound)
        if (type(principal) is not int or principal < 0 or
                (message is not None and (type(message) is not int or message <= 0))):
            raise CommandEffectDenied('memory_provenance_invalid')
        return dict(actor_user_id=None, actor_key=self.inbound.actor.key,
                    actor_principal_id=principal, source_message_id=message)

    def _execute(self, cursor, request):
        group, user = memory_scopes_for_inbound(self.inbound)
        scopes = [key for key in (group, user) if key is not None]
        if request.action == 'list':
            return CommandResult('ok', format_entries(self.store.list_entries(scopes)))
        if request.action == 'audit':
            return CommandResult('ok', format_audit(self.store.audit(scopes, limit=30)))
        if request.action == 'remove':
            entry = next((e for e in self.store.list_entries(scopes) if e.id == request.entry_id), None)
            if entry is None:
                return CommandResult('not_found', '没有找到这条可见记忆。发送 /记忆 查看 ID。')
            if entry.scope_key != user and not self._group_allowed(cursor, group):
                return CommandResult('denied', '当前本地权限不允许修改群记忆。')
            # Recheck exact address in the real store; no deletion by bare ID.
            removed = self.store.remove(entry.id, [entry.scope_key], **self._audit(cursor), reason='memory command remove')
            return CommandResult('ok' if removed else 'not_found', f'已删除 #{entry.id} [{label(entry)}] 记忆。' if removed else '没有找到这条可见记忆。')
        if request.action == 'clear':
            if request.scope == 'group' and not self._group_allowed(cursor, group):
                return CommandResult('denied', '当前本地权限不允许修改群记忆。')
            selected = [user] if request.scope != 'group' else [group]
            if request.scope == 'all' and self._group_allowed(cursor, group):
                selected.append(group)
            removed = self.store.clear(selected, **self._audit(cursor), reason='memory command clear '+request.scope)
            target = {'group':'当前群', 'user':'你的', 'all':'你有权修改的'}[request.scope]
            return CommandResult('ok', f'已清空{target}长期记忆，共 {removed} 条。')
        if request.scope == 'group':
            if group is None:
                return CommandResult('denied', '私聊中不能添加群记忆。')
            if not self._group_allowed(cursor, group):
                return CommandResult('denied', '当前本地权限不允许修改群记忆。')
        if _looks_like_secret(request.content):
            return CommandResult('denied', '检测到可能的密码、Token 或密钥，拒绝保存。')
        provenance = self._audit(cursor)
        entry, created = self.store.add(user if request.scope == 'user' else group, request.scope, request.content,
            creator_user_id=None, creator_actor_key=provenance['actor_key'],
            creator_principal_id=provenance['actor_principal_id'], source_message_id=provenance['source_message_id'],
            reason='memory command add')
        verb = '已记住' if created else '这条已经记过了'
        return CommandResult('ok', f'{verb}：#{entry.id} [{label(entry)}] {entry.content}')
