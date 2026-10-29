"""Explicit project grant commands; message handles only locate recipient evidence."""
from dataclasses import dataclass
import re

HELP = ('/项目权限 列表 prj#1\n'
        '/项目权限 授予 prj#1 msg#42 read,write 3600\n'
        '/项目权限 更新 prj#1 grant#<32位ID> v1 msg#42 read 3600\n'
        '/项目权限 撤销 prj#1 grant#<32位ID> v1\n'
        'msg#42 只定位当前入口的近期发言者；权限须由当前本地 owner 明确授予。期限单位为秒，最多 86400 秒。')
_CAPS = {'read', 'write', 'execute', 'manage', 'deliver'}

@dataclass(frozen=True)
class GrantCommand:
    action: str
    project_id: int
    message_id: int | None = None
    capabilities: frozenset[str] = frozenset()
    ttl_seconds: int | None = None
    grant_id: str | None = None
    expected_revision: int = 0


def _number(value, prefix):
    if not re.fullmatch(re.escape(prefix)+r'[1-9][0-9]{0,18}', value):
        raise ValueError(HELP)
    result = int(value[len(prefix):])
    if result >= 2**63: raise ValueError(HELP)
    return result


def parse_grant(text):
    if not isinstance(text, str) or len(text)>4096: raise ValueError(HELP)
    words = text.split()
    if len(words)<2: raise ValueError(HELP)
    action = {'list':'列表','grant':'授予','授权':'授予','revoke':'撤销','reissue':'更新'}.get(words[0].casefold(),words[0])
    project = _number(words[1],'prj#')
    if action=='列表' and len(words)==2: return GrantCommand('list',project)
    if action in {'撤销','更新'}:
        if len(words)!=(4 if action=='撤销' else 7): raise ValueError(HELP)
        if not re.fullmatch('grant#[a-f0-9]{32}',words[2]): raise ValueError(HELP)
        grant, revision = words[2][6:], _number(words[3],'v')
        if action=='撤销': return GrantCommand('revoke',project,grant_id=grant,expected_revision=revision)
        recipient, caps, ttl = words[4:]
    elif action=='授予' and len(words)==5:
        grant,revision=None,0
        recipient,caps,ttl=words[2:]
    else: raise ValueError(HELP)
    message_id = _number(recipient,'msg#')
    parts=caps.split(',')
    if not parts or len(set(parts))!=len(parts) or not set(parts)<=_CAPS: raise ValueError(HELP)
    if not re.fullmatch('[1-9][0-9]{0,4}',ttl) or not 1<=int(ttl)<=86400: raise ValueError(HELP)
    return GrantCommand('issue',project,message_id,frozenset('project.'+cap for cap in parts),int(ttl),grant,revision)


async def command_result(text, call):
    try: command=parse_grant(text)
    except ValueError: return HELP
    result=await call(command)
    if not result['ok']: return result['error']
    if command.action=='list':
        return '\n'.join(f"grant#{row['grant_id']} v{row['revision']} · {row['state']} · {','.join(row['capabilities'])} · 到期 {row['expires_at']}"
                         for row in result['grants']) or '当前项目没有可显示的授权。'
    reference=result['grant']
    return f"{'已撤销' if command.action=='revoke' else '已记录明确授权'} grant#{reference['grant_id']} v{reference['revision']}。"
