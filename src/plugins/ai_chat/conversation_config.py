"""Fixed physical-conversation settings. Only authenticated host callers may write."""
from dataclasses import dataclass
import json
import re

from .platform_types import EndpointKey


@dataclass(frozen=True)
class Setting:
    default: bool | int
    resource: str
    about: str
    maximum: int = 0
    minimum: int = 0

    def validate(self, value):
        if type(value) is not type(self.default) or type(value) is int and not self.minimum<=value<=self.maximum:
            raise ValueError('invalid_conversation_setting_value')
        return value

    def parse(self, value):
        if value=='default':return self.default
        if type(self.default) is bool:
            options = {'on':True,'off':False,'true':True,'false':False}
            if value not in options:raise ValueError('setting_requires_on_off_or_default')
            return options[value]
        if not re.fullmatch(r'[0-9]{1,5}',value):raise ValueError('setting_requires_integer_or_default')
        return self.validate(int(value))


SETTINGS = {
    'agent_timeout_hours':Setting(6,'commands','1–6小时；只影响新任务',6,1),
    'proactive_enabled':Setting(True,'conversations','主动接话；仍需宿主启用分类器'),
    'program_say_limit':Setting(60,'programs','每整点小时发言/文件共用额度；0禁用',10000),
    'program_wake_limit':Setting(6,'programs','每整点小时唤醒额度；0禁用，超额合并',10000),
    'image_captions':Setting(False,'captions','新入站图片说明；仍需宿主视觉服务'),
    'image_daily_limit':Setting(200,'captions','北京时间每天图片数；0禁用',10000),
}


def parse(argument):
    words = argument.split()
    if not words:return '',None
    if len(words)>2 or words[0] not in SETTINGS:raise ValueError('unknown_conversation_setting')
    return words[0],SETTINGS[words[0]].parse(words[1]) if len(words)==2 else None


def snapshot(cursor, endpoint):
    row = cursor.execute('SELECT * FROM conversation_settings WHERE scope_key=?',(endpoint.scope.key,)).fetchone()
    return {name:row[name] if row else item.default for name,item in SETTINGS.items()}


def display(values, name=''):
    def value(item):return ('on' if item else 'off') if type(item) is bool else str(item)
    return '\n'.join(f'{key} = {value(values[key])}（默认 {value(item.default)}；{item.about}）'
        for key,item in SETTINGS.items() if not name or name==key)


def change(cursor, endpoint, name, value, *, actor, now):
    """The caller must check the native command/admin authority in this transaction.

    Join the same account -> resource -> settings lock order as admin writers.
    Never take a history scope or an SQL identifier from untrusted input.
    """
    if not isinstance(endpoint,EndpointKey) or endpoint.account.platform!='qq-official' or name not in SETTINGS:
        raise ValueError('invalid_conversation_setting')
    item = SETTINGS[name]; item.validate(value)
    cursor.execute('SELECT account_key FROM qq_official_accounts WHERE account_key=? FOR UPDATE',(endpoint.account.key,))
    if not cursor.execute('SELECT 1 FROM qq_official_endpoints WHERE endpoint_key=?',(endpoint.key,)).fetchone():
        raise ValueError('conversation_endpoint_unavailable')
    before = snapshot(cursor,endpoint)[name]
    version = cursor.execute('''INSERT INTO admin_resource_versions(resource_key,version,updated_at) VALUES (?,1,?)
        ON CONFLICT(resource_key) DO UPDATE SET version=admin_resource_versions.version+1,updated_at=EXCLUDED.updated_at
        RETURNING version''',(item.resource,now)).fetchone()[0]
    cursor.execute('INSERT INTO conversation_settings(scope_key) VALUES (?) ON CONFLICT DO NOTHING',(endpoint.scope.key,))
    extra = ',proactive_generation=proactive_generation+CASE WHEN proactive_enabled<>? THEN 1 ELSE 0 END' if name=='proactive_enabled' else ''
    args = (value,value,endpoint.scope.key) if extra else (value,endpoint.scope.key)
    cursor.execute('UPDATE conversation_settings SET '+name+'=?'+extra+' WHERE scope_key=?',args)
    cursor.execute('''INSERT INTO admin_audit_log(resource_key,resource_version,actor,action,target,status,before_json,after_json,error,created_at)
        VALUES (?,?,?,?,?,'succeeded',?,?,'',?)''',(item.resource,version,actor,'conversation.config',endpoint.key,
            json.dumps({name:before}),json.dumps({name:value}),now))
    return snapshot(cursor,endpoint)


def proactive_state(cursor, endpoint):
    row = cursor.execute('SELECT proactive_enabled,proactive_generation FROM conversation_settings WHERE scope_key=?',
        (endpoint.scope.key,)).fetchone()
    return (row['proactive_enabled'],row['proactive_generation']) if row else (True,0)
