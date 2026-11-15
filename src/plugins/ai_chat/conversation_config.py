"""Fixed physical-conversation settings. Only authenticated host callers may write."""
from dataclasses import dataclass
import json
import re

from .platform_types import EndpointKey
from .admin_control import AdminVersionConflict
from . import daily_budgets
from .model_billing_views import usd


@dataclass(frozen=True)
class Setting:
    default: bool | int | str
    resource: str
    about: str
    maximum: int = 0
    minimum: int = 0

    def validate(self, value):
        if type(self.default) is str:
            return self.default if value==self.default else usd(daily_budgets.amount(value))
        if type(value) is not type(self.default) or type(value) is int and not self.minimum<=value<=self.maximum:
            raise ValueError('invalid_conversation_setting_value')
        return value

    def parse(self, value):
        if value=='default':return self.default
        if type(self.default) is str:return self.validate(value)
        if type(self.default) is bool:
            options = {'on':True,'off':False,'true':True,'false':False}
            if value not in options:raise ValueError('setting_requires_on_off_or_default')
            return options[value]
        if not re.fullmatch(r'[0-9]{1,5}',value):raise ValueError('setting_requires_integer_or_default')
        return self.validate(int(value))


SETTINGS = {
    'agent_timeout_hours':Setting(6,'commands','1–6小时；只影响新任务',6,1),
    'proactive_enabled':Setting(True,'conversations','仅群聊主动接话；仍需宿主启用分类器'),
    'program_say_limit':Setting(60,'programs','每整点小时发言/文件共用额度；0禁用',10000),
    'program_wake_limit':Setting(6,'programs','每整点小时唤醒额度；0禁用，超额合并',10000),
    'image_captions':Setting(False,'captions','新入站图片说明；仍需宿主视觉服务'),
    'image_daily_limit':Setting(200,'captions','北京时间每天图片数；0禁用',10000),
    'daily_budget_usd':Setting('off','budgets','美元/天，最多1000000、6位小数；0禁自动动作，off关闭；北京时间零点恢复，@仍计费'),
}


def parse(argument):
    words = argument.split()
    if not words:return '',None
    if len(words)>2 or words[0] not in SETTINGS:raise ValueError('unknown_conversation_setting')
    return words[0],SETTINGS[words[0]].parse(words[1]) if len(words)==2 else None


def snapshot(cursor, endpoint):
    row = cursor.execute('SELECT * FROM conversation_settings WHERE scope_key=?',(endpoint.scope.key,)).fetchone()
    values = {name:row[name] if row else item.default for name,item in SETTINGS.items()}
    if row:
        values['daily_budget_usd'] = usd(row['daily_budget_usd']) if row['daily_budget_usd'] is not None else 'off'
    return values


def display(values, name=''):
    def value(item,key):
        if key=='daily_budget_usd':return 'off' if item=='off' else '$'+item
        return ('on' if item else 'off') if type(item) is bool else str(item)
    return '\n'.join(f'{key} = {value(values[key],key)}（默认 {value(item.default,key)}；{item.about}）'
        for key,item in SETTINGS.items() if not name or name==key)


def change(cursor, endpoint, name, value, *, actor, now, expected_version=None):
    """The caller must check the native command/admin authority in this transaction.

    Join the same account -> resource -> settings lock order as admin writers.
    Never take a history scope or an SQL identifier from untrusted input.
    """
    if not isinstance(endpoint,EndpointKey) or endpoint.account.platform!='qq-official' or name not in SETTINGS:
        raise ValueError('invalid_conversation_setting')
    item = SETTINGS[name]; value = item.validate(value)
    if expected_version is not None and (type(expected_version) is not int or expected_version<0):
        raise ValueError('invalid_conversation_setting_version')
    cursor.execute('SELECT account_key FROM qq_official_accounts WHERE account_key=? FOR UPDATE',(endpoint.account.key,))
    if not cursor.execute('SELECT 1 FROM qq_official_endpoints WHERE endpoint_key=?',(endpoint.key,)).fetchone():
        raise ValueError('conversation_endpoint_unavailable')
    before = snapshot(cursor,endpoint)[name]
    cursor.execute('''INSERT INTO admin_resource_versions(resource_key,version,updated_at) VALUES (?,0,?)
        ON CONFLICT(resource_key) DO NOTHING''',(item.resource,now))
    version = cursor.execute('SELECT version FROM admin_resource_versions WHERE resource_key=? FOR UPDATE',(item.resource,)).fetchone()[0]
    if expected_version is not None and expected_version!=version:
        raise AdminVersionConflict(item.resource,expected_version,version)
    version += 1
    cursor.execute('UPDATE admin_resource_versions SET version=?,updated_at=? WHERE resource_key=?',(version,now,item.resource))
    cursor.execute('INSERT INTO conversation_settings(scope_key) VALUES (?) ON CONFLICT DO NOTHING',(endpoint.scope.key,))
    extra = ',proactive_generation=proactive_generation+CASE WHEN proactive_enabled<>? THEN 1 ELSE 0 END' if name=='proactive_enabled' else ''
    args = (value,value,endpoint.scope.key) if extra else (value,endpoint.scope.key)
    if name=='daily_budget_usd':
        extra = ',budget_generation=budget_generation+?,proactive_generation=proactive_generation+?'
        changed = int(before!=value)
        args = (None if value=='off' else value,changed,changed,endpoint.scope.key)
    cursor.execute('UPDATE conversation_settings SET '+name+'=?'+extra+' WHERE scope_key=?',args)
    if name=='daily_budget_usd':daily_budgets.refresh(cursor,endpoint,now=now)
    cursor.execute('''INSERT INTO admin_audit_log(resource_key,resource_version,actor,action,target,status,before_json,after_json,error,created_at)
        VALUES (?,?,?,?,?,'succeeded',?,?,'',?)''',(item.resource,version,actor,'conversation.config',endpoint.key,
            json.dumps({name:before}),json.dumps({name:value}),now))
    return snapshot(cursor,endpoint)


def proactive_state(cursor, endpoint):
    row = cursor.execute('SELECT proactive_enabled,proactive_generation FROM conversation_settings WHERE scope_key=?',
        (endpoint.scope.key,)).fetchone()
    return (row['proactive_enabled'],row['proactive_generation']) if row else (True,0)
