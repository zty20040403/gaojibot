from functools import partial
import json
from .background import definition, enabled, service
from .registry import FunctionTool


async def execute(turn, args, *, name):
    try:
        value = await service(turn.context).schedules.control(turn, name, args)
        return json.dumps({'ok': True, **value}, ensure_ascii=False)
    except (ValueError, PermissionError) as error:
        return json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=False)


DEFINITIONS = (
    definition('schedule_set', '设置提醒或例行后台任务，时间按北京时间 Asia/Shanghai。at 用 ISO 日期时间；cron 用五段格式，最短每小时一次。每人最多二十个。',
        {'at': {'type': 'string'}, 'cron': {'type': 'string'}, 'text': {'type': 'string'},
         'kind': {'type': 'string', 'enum': ['reminder', 'routine']}}, ('text', 'kind')),
    definition('schedule_list', '查看自己在本会话创建的提醒和例行任务。', {}),
    definition('schedule_cancel', '取消自己在本会话创建的提醒或例行任务。', {'id': {'type': 'integer'}}, ('id',)),
)
HANDLERS = tuple(FunctionTool(item, enabled, partial(execute, name=item['function']['name']), platform_neutral=True)
                 for item in DEFINITIONS)
