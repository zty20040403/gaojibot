"""Bounded result-only provenance after a legacy task's execution job ends.

This context does not install a model/tool RouteFence. It can bind exactly one
host-rendered final delivery to an already persisted task source and deadline.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import weakref

from src.plugins.ai_chat.delivery import body_fingerprint
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from .onebot_source_inventory import resolve_task
from .qq_official.migration_types import MigrationDenied, digest


@dataclass(frozen=True,slots=True,weakref_slot=True,eq=False,repr=False)
class _ResultSource:
    parent_id: str
    task_id: int
    revision: int
    result_digest: str
    fingerprint: str
    source_scope_key: str
    target_scope_key: str
    source_message: int | None
    reply_to: str


_issued=weakref.WeakKeyDictionary()
_active=ContextVar('onebot_terminal_delivery',default=None)


def _task(runtime,cursor,task_id,revision):
    parent=cursor.execute('''SELECT * FROM migration_work_sources
        WHERE source_kind='subagent' AND task_id=? AND source_revision=?''',(task_id,revision)).fetchone()
    if parent is None:
        raise MigrationDenied('terminal_result_original_source_missing')
    endpoint=EndpointKey.parse(parent['endpoint_key'])
    runtime.routes.lock(cursor,endpoints=(endpoint,),scopes=(parent['canonical_scope_key'],))
    runtime.routes.assert_active_route(cursor,canonical_scope=parent['canonical_scope_key'],endpoint=endpoint,
        route_epoch=parent['route_epoch'],operation='send')
    parent=cursor.execute('SELECT * FROM migration_work_sources WHERE source_id=? FOR SHARE',
                          (parent['source_id'],)).fetchone()
    value=resolve_task(cursor,task_id,revision)
    task=cursor.execute('SELECT * FROM subagent_tasks WHERE task_id=?',(task_id,)).fetchone()
    if (value is None or task is None or task['account_key'] is not None or task['project_id'] is not None
            or task['status'] not in {'completed','partial','failed','cancelled'}
            or parent['state'] not in {'ready','completed'} or parent['deadline_at']<=runtime.sources._now()
            or parent['source_digest']!=value.fingerprint or parent['canonical_scope_key']!=value.scope.key
            or endpoint.account.app_id!=value.bot_native_id or endpoint.kind!=value.endpoint_kind
            or endpoint.native_conversation_id!=value.endpoint_native_id
            or ActorKey.parse(parent['actor_key']).native_user_id!=value.actor_native_id):
        raise MigrationDenied('terminal_result_source_changed')
    result=digest([task[k] for k in ('task_id','status','result_json','last_error','finished_at','cancel_requested')])
    return dict(parent),dict(task),result


@contextmanager
def terminal_delivery_source(runtime,task_id,revision,*,body,source_scope_key,target_scope,source_message,reply_to):
    with runtime.sources.transaction() as cursor:
        parent,task,result=_task(runtime,cursor,task_id,revision)
        if (source_scope_key!=task['scope_key'] or target_scope.key!=task['scope_key']
                or source_message!=task['trigger_message_id']):
            raise MigrationDenied('terminal_result_target_changed')
    proof=_ResultSource(parent['source_id'],task_id,revision,result,body_fingerprint(body,reply_to),
        source_scope_key,target_scope.key,source_message,reply_to)
    _issued[proof]=runtime
    mark=_active.set(proof)
    try:
        yield
    finally:
        _active.reset(mark)
        _issued.pop(proof,None)


def capture_terminal_delivery(runtime,cursor,delivery,now):
    from .onebot_migration_runtime import delivery_digest
    proof=_active.get()
    if proof is None or _issued.get(proof) is not runtime:
        raise MigrationDenied('terminal_result_source_required')
    parent,task,result=_task(runtime,cursor,proof.task_id,proof.revision)
    if (parent['source_id']!=proof.parent_id or result!=proof.result_digest
            or delivery.idempotency_key!=f'subagent-final:{proof.task_id}:{proof.revision}'
            or delivery.source_scope_key!=proof.source_scope_key or delivery.target_scope.key!=proof.target_scope_key
            or delivery.source_canonical_message_id!=proof.source_message or delivery.reply_to_native_message_id!=proof.reply_to
            or body_fingerprint(delivery.body,delivery.reply_to_native_message_id)!=proof.fingerprint):
        raise MigrationDenied('terminal_result_payload_changed')
    endpoint=EndpointKey.parse(parent['endpoint_key'])
    row=cursor.execute('SELECT * FROM deliveries WHERE delivery_id=?',(delivery.delivery_id,)).fetchone()
    return runtime.sources.create(cursor,baseline=runtime.baselines[parent['account_key']],endpoint=endpoint,
        scope=ConversationScope.from_key(parent['canonical_scope_key']),
        actor=ActorKey.parse(parent['actor_key']),kind='delivery',ref=str(delivery.delivery_id),revision=1,
        fingerprint=delivery_digest(row),deadline=parent['deadline_at'],delivery_id=delivery.delivery_id,
        parent_source_id=parent['source_id'])


def require_terminal_delivery(runtime,cursor,source,parent,delivery):
    expected=f"subagent-final:{parent['task_id']}:{parent['source_revision']}"
    if delivery['idempotency_key']!=expected:
        raise MigrationDenied('terminal_result_identity_changed')
    current,task,_=_task(runtime,cursor,parent['task_id'],parent['source_revision'])
    if (current['source_id']!=source['parent_source_id']
            or delivery['source_canonical_message_id']!=task['trigger_message_id']):
        raise MigrationDenied('terminal_result_identity_changed')
