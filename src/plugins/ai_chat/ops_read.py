"""Host-only GET calls for an owner's private background task."""
import json
from pathlib import Path
from urllib.parse import quote

import httpx

from .tools.background import definition


TOOL = definition('ops_read', '只读查看服务器概况、服务状态、日志、磁盘或资源。只能在管理员私聊发起的后台任务使用；不会重启或修改服务。',
    {'action': {'type': 'string', 'enum': ['overview', 'service', 'logs', 'disk', 'resources']},
     'host': {'type': 'string'}, 'unit': {'type': 'string'}, 'lines': {'type': 'integer', 'minimum': 1, 'maximum': 200}}, ('action',))


def owner_allowed(app, inbound):
    if inbound.endpoint.kind != 'private' or not app.settings.ops_read_base_url or not app.settings.ops_read_token_file:
        return False
    runtime = app.official_runtime
    with runtime.store.transaction() as cursor:
        return runtime.store.authority.owner_for_actor(cursor, inbound.endpoint, inbound.actor) is not None


async def read(app, inbound, args):
    if not await app.db_call(owner_allowed, app, inbound):
        raise PermissionError('运维读取只供管理员私聊使用。')
    action, host, unit = args['action'], str(args.get('host', '')), str(args.get('unit', ''))
    if action != 'overview' and not host:
        raise ValueError('请指定 host。')
    if action in {'service', 'logs'} and not unit:
        raise ValueError('请指定 unit。')
    host_path = '/v1/hosts/' + quote(host, safe='')
    paths = {'overview': host_path if host else '/v1/fleet', 'service': host_path + '/units/' + quote(unit, safe=''),
        'logs': host_path + '/units/' + quote(unit, safe='') + '/logs',
        'disk': host_path + '/metrics', 'resources': host_path + '/metrics'}
    if action not in paths:
        raise ValueError('不支持此运维读取操作。')
    token = Path(app.settings.ops_read_token_file).read_text().strip()
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        response = await client.get(app.settings.ops_read_base_url.rstrip('/') + paths[action],
            headers={'Authorization': 'Bearer ' + token}, params={'lines': int(args.get('lines', 50))} if action == 'logs' else None)
        response.raise_for_status()
        return json.dumps(response.json(), ensure_ascii=False)
