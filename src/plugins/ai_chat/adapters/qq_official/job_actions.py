"""Job-scoped file actions backed by the ordinary official file outbox."""
import json
import re

from ...application.owned_sandbox import workspace_path
from ...tool_invocation import active_tool_call_id
from .send_types import OutboxInvariantError, digest
from .vm_http import REQUEST


def _status(plan):
    states = {'committed': 'completed', 'pending': 'queued', 'sending': 'sending',
              'failed': 'failed', 'cancelled': 'cancelled', 'ambiguous': 'ambiguous'}
    state = states.get(plan['status'], 'unconfirmed')
    meta = json.loads(plan['manifest_json'])['job_request']
    return {'action_id': plan['plan_id'], 'request_id': meta['request_id'], 'kind': meta['kind'],
            'state': state}


def _plans(cursor, row, *, request_id=None, action_id=None):
    query = '''SELECT p.plan_id,p.manifest_json,p.manifest_digest,d.status FROM delivery_plans p
        JOIN deliveries d ON d.plan_id=p.plan_id AND d.part_index=0
        WHERE p.account_key=? AND p.endpoint_key=? AND p.requester_actor_key=? AND p.source_scope_key=?
        AND p.source_canonical_message_id=? AND p.purpose='requested_artifact'
        AND p.manifest_json::jsonb->'job_request'->>'job_id'=?'''
    args = [row['account_key'], row['endpoint_key'], row['actor_key'], row['scope_key'],
            row['canonical_message_id'], str(row['job_id'])]
    if request_id is not None:
        query += " AND p.manifest_json::jsonb->'job_request'->>'request_id'=?"
        args.append(request_id)
    if action_id is not None:
        query += ' AND p.plan_id=?'
        args.append(action_id)
    plans = cursor.execute(query + ' ORDER BY p.created_at DESC,p.plan_id DESC LIMIT 50', args).fetchall()
    if any(digest(plan['manifest_json']) != plan['manifest_digest'] for plan in plans):
        raise OutboxInvariantError('job_receipt_manifest_changed')
    return plans


def receipt_details(cursor, row):
    return [_status(plan) for plan in _plans(cursor, row)]


def receipt_summary(receipts):
    if not receipts:
        return '附件发送未确认：没有此任务的真实发送回执。'
    counts = {state: sum(r['state'] == state for r in receipts)
              for state in ('completed', 'queued', 'sending', 'failed', 'cancelled', 'ambiguous', 'unconfirmed')}
    return (('附件发送回执（最近 50 项）：' if len(receipts) == 50 else '附件发送回执：') + '，'.join(f'{label} {counts[state]}' for state, label in (
        ('completed', '已送达'), ('queued', '排队中'), ('sending', '发送中'), ('failed', '失败'),
        ('cancelled', '取消'), ('ambiguous', '结果不确定'), ('unconfirmed', '未确认')) if counts[state])
        + '；未确认的附件不能视为已送达。')


def delivery_current(cursor, delivery, meta):
    row = cursor.execute('SELECT * FROM background_jobs WHERE job_id=?', (meta['job_id'],)).fetchone()
    return (row is not None and row['state'] != 'cancelled'
            and row['account_key'] == delivery['account_key'] and row['endpoint_key'] == delivery['endpoint_key']
            and row['actor_key'] == delivery['requester_actor_key'] and row['scope_key'] == delivery['source_scope_key']
            and row['canonical_message_id'] == delivery['source_canonical_message_id'])


class JobFileActions:
    def __init__(self, service, row, inbound, current, check_transaction):
        self.service, self.row, self.inbound = service, row, inbound
        self.current, self.check_transaction = current, check_transaction
        self.inflight = set()

    def _find(self, *, request_id=None, action_id=None):
        with self.service.runtime.store.transaction() as cursor:
            self.check_transaction(cursor)
            plans = _plans(cursor, self.row, request_id=request_id, action_id=action_id)
            if plans:
                plan = plans[0]
                return json.loads(plan['manifest_json'])['job_request'], _status(plan)
        return None

    async def execute(self, name, arguments):
        await self.current()
        request_id = arguments.get('request_id')
        if name == 'action_status':
            action_id = arguments.get('action_id')
            if bool(request_id) == bool(action_id):
                raise ValueError('请提供 request_id 或 action_id 之一。')
            if request_id and REQUEST.fullmatch(request_id) is None:
                raise ValueError('invalid_request_id')
            if action_id and re.fullmatch(r'[0-9a-f]{32}', action_id) is None:
                raise ValueError('invalid_action_id')
            found = await self.service.calls(self._find, request_id=request_id, action_id=action_id)
            if found is None:
                raise ValueError('job_action_not_found')
            return json.dumps(found[1], ensure_ascii=False)
        if not request_id or REQUEST.fullmatch(request_id) is None:
            raise ValueError('发送文件必须提供 request_id；状态不明时用同一 ID 查询。')
        meta = {'job_id': self.row['job_id'], 'request_id': request_id, 'kind': name,
                'path': workspace_path(arguments['path']), 'filename': arguments.get('filename')}
        found = await self.service.calls(self._find, request_id=request_id)
        if found is not None:
            if found[0] != meta:
                raise ValueError('job_request_conflict')
            return json.dumps(found[1], ensure_ascii=False)
        if request_id in self.inflight:
            raise ValueError('job_request_in_progress')
        self.inflight.add(request_id)
        try:
            files = self.service.runtime.file_service
            if files is None:
                raise RuntimeError('job_files_unavailable')
            session = await files.generation.background(self.inbound, self.row, current=self.current,
                job_request=meta, check_transaction=self.check_transaction)
            if session is None:
                raise RuntimeError('job_files_unavailable')
            try:
                token = active_tool_call_id.set(request_id)
                try:
                    args = {'path': arguments['path']}
                    if arguments.get('filename') is not None:
                        args['filename'] = arguments['filename']
                    raw = await session.executor.execute('send_file_from_sandbox' if name == 'send_file' else name, args)
                finally:
                    active_tool_call_id.reset(token)
            finally:
                await session.close()
            found = await self.service.calls(self._find, request_id=request_id)
            if found:
                return json.dumps(found[1], ensure_ascii=False)
            if json.loads(raw).get('ok') is False:
                return raw
            raise OutboxInvariantError('job_action_not_recorded')
        finally:
            self.inflight.remove(request_id)
