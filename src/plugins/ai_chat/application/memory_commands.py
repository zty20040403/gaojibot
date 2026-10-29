"""Memory command grammar and presentation. No storage or platform permissions."""
from dataclasses import dataclass, field
import re


class MemorySyntax(ValueError):
    pass


@dataclass(frozen=True)
class MemoryCommand:
    action: str
    scope: str = 'all'
    entry_id: int | None = None
    content: str = field(default='', repr=False)


def parse_memory(text: str) -> MemoryCommand:
    if not isinstance(text, str) or len(text) > 4096:
        raise MemorySyntax('命令参数无效。')
    requested = ' '.join(text.split())
    action, _, remainder = requested.partition(' ')
    normalized = action.casefold()
    if normalized in {'审计', 'audit', 'history', '历史'}:
        if remainder:
            raise MemorySyntax('用法：/memory 审计')
        return MemoryCommand('audit')
    if not requested or normalized in {'列表', 'list', 'ls', '查看'}:
        if remainder:
            raise MemorySyntax('用法：/memory 列表')
        return MemoryCommand('list')
    if normalized in {'删除', 'remove', 'rm', 'forget', '忘记'}:
        if not re.fullmatch(r'#?[1-9][0-9]{0,18}', remainder) or int(remainder.lstrip('#')) >= 2**63:
            raise MemorySyntax('请填写可见记忆 ID，例如 /memory 删除 12。')
        return MemoryCommand('remove', entry_id=int(remainder.lstrip('#')))
    if normalized in {'清空', 'clear'}:
        target = remainder.casefold() or 'user'
        scopes = {'user': 'user', '个人': 'user', '我': 'user',
                  'group': 'group', '群': 'group', 'all': 'all', '全部': 'all'}
        if target not in scopes:
            raise MemorySyntax('用法：/memory 清空 [个人|群|全部]')
        return MemoryCommand('clear', scopes[target])
    scope, content = 'user', requested
    if normalized in {'添加', 'add', '记住', 'remember'}:
        content = remainder
    if normalized in {'群', 'group'}:
        scope, content = 'group', remainder
    elif content.startswith(('群：', '群:', 'group:')):
        scope = 'group'
        content = content.partition('：' if content.startswith('群：') else ':')[2].strip()
    if not content:
        raise MemorySyntax('请填写要记住的内容。')
    return MemoryCommand('add', scope, content=content)


def label(entry) -> str:
    return '群' if entry.scope_type == 'group' else '个人'


def format_entries(entries) -> str:
    if not entries:
        return '当前群和你的个人范围都没有长期记忆。'
    return '\n'.join(['当前可见的长期记忆：', *[
        f'- #{entry.id} [{label(entry)}] {entry.content}' for entry in entries], '\n删除：/记忆 删除 ID'])


def format_audit(mutations) -> str:
    if not mutations:
        return '当前可见范围内还没有长期记忆变更记录。'
    lines = ['最近的长期记忆变更：']
    for mutation in mutations:
        actor = f'[mention#{mutation.actor_principal_id}]' if mutation.actor_principal_id > 0 else '本地操作者'
        evidence = f' · msg#{mutation.source_message_id}' if mutation.source_message_id is not None else ''
        lines.append(f'- m{mutation.memory_id} {mutation.action} v{mutation.from_version}→v{mutation.to_version} · {actor}{evidence}')
    return '\n'.join(lines)
