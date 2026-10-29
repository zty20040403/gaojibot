"""Current authority for stored artifacts after their producer turn has ended.

This adapter checks existing durable job leases; it introduces no queue or grant
from model input. M3 historical receipt settlement intentionally does not call it.
"""
from __future__ import annotations

from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.agent.control import LeaseLost
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from .file_artifacts import _ref, _time, file_job_kind
from .project_authority import RecipientEvidenceUnavailable


def _job_current(cursor, job, *, job_id, kind, payload, now):
    current = cursor.execute('SELECT * FROM durable_jobs WHERE job_id=? FOR SHARE',(job_id,)).fetchone()
    if (job is None or current is None or job.job_id!=job_id or job.kind!=kind or job.payload!=payload
            or current['status']!='running' or current['lease_owner']!=job.lease_owner
            or current['lease_generation']!=job.lease_generation or current['attempts']!=job.attempts
            or (current['lease_until'] or 0)<=now):
        raise LeaseLost('artifact_delivery_job_unavailable')


def assert_file_current(cursor, *, token, artifact_id, admission, now, job=None, plan_id=None, source_authority=None):
    """Account→endpoint→jobs→source domain→artifact, under caller transaction.

    A worker supplies its exact live job. A post-handoff M3 sender instead
    supplies the exact already-associated active plan; neither mode renews an
    expired producer or invents a source from canonical conversation history.
    """
    return _assert_current(cursor,token=token,artifact_id=artifact_id,admission=admission,
        now=now,job=job,plan_id=plan_id,read_only=False,source_authority=source_authority)


def assert_file_read_current(cursor, *, token, artifact_id, admission, now, source_authority=None):
    """Authenticated host-console read; never issues sending authority.

    Transport failures do not invalidate intact source-owned bytes. This path
    needs no file-worker lease or M3 plan, while keeping all source, revision,
    actor, endpoint, admission and retention checks below unchanged.
    """
    return _assert_current(cursor,token=token,artifact_id=artifact_id,admission=admission,
        now=now,job=None,plan_id=None,read_only=True,source_authority=source_authority)


def _assert_current(cursor, *, token, artifact_id, admission, now, job, plan_id, read_only, source_authority):
    _ref(artifact_id)
    _time(now)
    try:
        assert_epoch(cursor,token)
    except Exception:
        raise LeaseLost('artifact_account_unavailable') from None
    artifact=cursor.execute('SELECT * FROM qq_official_file_artifacts WHERE artifact_id=?',(artifact_id,)).fetchone()
    if artifact is None or artifact['account_key']!=token.account_key:
        raise LeaseLost('artifact_source_unavailable')
    endpoint,actor=EndpointKey.parse(artifact['endpoint_key']),ActorKey.parse(artifact['requester_actor_key'])
    route=cursor.execute('SELECT * FROM qq_official_endpoints WHERE endpoint_key=? AND account_key=? FOR UPDATE',
        (endpoint.key,token.account_key)).fetchone()
    if (endpoint.account.key!=token.account_key or not actor.matches_endpoint(endpoint) or actor.identity_kind=='bot'
            or route is None or route['binding_state']=='revoked'
            or (endpoint.kind=='group' and route['binding_state']!='active') or route['membership_state']!='present'
            or route['generation']!=artifact['endpoint_generation'] or route['route_epoch']!=artifact['route_epoch']
            or admission(cursor,endpoint,actor) is not True):
        raise LeaseLost('artifact_route_unavailable')
    delivery=cursor.execute('SELECT * FROM qq_official_file_deliveries WHERE artifact_id=?',(artifact_id,)).fetchone()
    denied={'cancelled','expired'} if read_only else {'failed','cancelled','expired','delivery_unknown'}
    if delivery is None or delivery['state'] in denied:
        raise LeaseLost('artifact_delivery_unavailable')
    if read_only:
        # This branch only returns immutable manifest data to the protected
        # reader. The public sending checker cannot select it.
        pass
    elif job is not None:
        if plan_id is not None:
            raise LeaseLost('artifact_authority_mode_conflict')
        _job_current(cursor,job,job_id=delivery['job_id'],kind=file_job_kind(endpoint.account),
            payload={'artifact_id':artifact_id},now=now)
    elif (not isinstance(plan_id,str) or delivery['active_plan_id']!=plan_id
          or delivery['state'] not in {'handed_off','message_accepted'}):
        raise LeaseLost('artifact_handoff_unavailable')
    if not read_only and artifact['source_execution_id'] is not None:
        file_guard = getattr(admission, 'require_file', None)
        source_guard = getattr(admission, 'require_execution', None)
        if file_guard is not None:
            file_guard(cursor, artifact=dict(artifact), job=job, plan_id=plan_id, now=now)
        elif source_guard is not None:
            source_guard(cursor, artifact['source_execution_id'], endpoint=endpoint, actor=actor,
                operation='execute' if job is not None else 'send')
    recipient_wait = None
    extension = artifact.get('source_extension_kind')
    if extension is not None:
        if extension not in {'project','migration'} or source_authority is None:
            raise LeaseLost('artifact_extension_unavailable')
        try:
            if source_authority(cursor,token=token,artifact=dict(artifact),now=now,read_only=read_only) is not None:
                raise LeaseLost('artifact_extension_unavailable')
        except RecipientEvidenceUnavailable as error:
            # Still check producer cancellation, exact provenance and retention.
            # Missing presence must not disguise an independently revoked source.
            recipient_wait = error
    # Parent job locking precedes domain records, as in the producer/cancel
    # paths. Completed producer jobs need no live lease to deliver frozen bytes.
    producer=cursor.execute('SELECT * FROM durable_jobs WHERE job_id=? FOR SHARE',
        (artifact['source_job_id'],)).fetchone()
    source=cursor.execute('SELECT * FROM qq_official_turn_executions WHERE execution_id=? FOR SHARE',
        (artifact['source_execution_id'],)).fetchone()
    if artifact.get('source_migration_item_id') is None and (source is None or any(source[k]!=artifact[v] for k,v in (
        ('account_key','account_key'),('endpoint_key','endpoint_key'),('requester_actor_key','requester_actor_key'),
        ('canonical_message_id','source_canonical_message_id'),('source_native_message_id','source_native_message_id'),
        ('scope_key','source_scope_key'),('endpoint_generation','endpoint_generation'),('route_epoch','route_epoch')))):
        raise LeaseLost('artifact_source_conflict')
    if extension=='migration' and artifact['source_task_id'] is None:
        # The exact migration-file import/manifest authority above replaces
        # the native producer; there is deliberately no fabricated turn/task.
        pass
    elif artifact['source_task_id'] is None:
        if source['state'] in {'prepared','cancelled','needs_attention'}:
            raise LeaseLost('artifact_turn_not_available')
        if source['state']=='executing':
            if (producer is None or producer['status']!='running' or (producer['lease_until'] or 0)<=now
                    or producer['lease_owner']!=source['worker_owner']
                    or producer['lease_generation']!=source['job_lease_generation'] or now>=source['deadline_at']):
                raise LeaseLost('artifact_producer_interrupted')
        elif (source['state'] not in {'result_ready','handed_off'} or source['result_status'] in {'cancelled','aborted'}
                or (source['result_status']!='succeeded' and not artifact['draft'])):
            raise LeaseLost('artifact_turn_result_unavailable')
    else:
        task_binding=cursor.execute('SELECT * FROM qq_official_subagent_dispatches WHERE task_id=? FOR SHARE',
            (artifact['source_task_id'],)).fetchone()
        control=cursor.execute('SELECT * FROM subagent_controls WHERE task_id=? FOR SHARE',
            (artifact['source_task_id'],)).fetchone()
        task=cursor.execute('SELECT * FROM subagent_tasks WHERE task_id=? FOR SHARE',
            (artifact['source_task_id'],)).fetchone()
        if (task_binding is None or control is None or task is None
                or task_binding['source_execution_id']!=artifact['source_execution_id']
                or task_binding['job_id']!=artifact['source_job_id']
                or task_binding['task_revision']!=artifact['source_task_revision']
                or control['revision']!=artifact['source_task_revision'] or task['cancel_requested']
                or task['status']=='cancelled' or (task['project_id'] is not None and extension != 'project')
                or (task['status']=='failed' and not artifact['draft'])
                or task['endpoint_key']!=endpoint.key or task['requester_actor_key']!=actor.key
                or task['account_key']!=token.account_key or task['requester_user_id'] is not None):
            raise LeaseLost('artifact_task_unavailable')
        if task['status'] not in {'completed','partial','failed'} and (
                producer is None or producer['status']!='running' or (producer['lease_until'] or 0)<=now
                or now>=task_binding['execution_deadline_at']):
            raise LeaseLost('artifact_producer_interrupted')
    # Re-read under row lock after source locks; publisher then locks its row.
    artifact=cursor.execute('SELECT * FROM qq_official_file_artifacts WHERE artifact_id=? FOR SHARE',
        (artifact_id,)).fetchone()
    if (artifact['bytes_state']!='available' or now>=min(artifact['pending_deadline_at'],artifact['retain_until'])):
        raise LeaseLost('artifact_retention_expired')
    if recipient_wait is not None:
        raise recipient_wait
    return dict(artifact),dict(delivery)


class ArtifactSourceAuthorities:
    """Explicit host composition; kind names alone never authenticate a source."""
    def __init__(self, *authorities):
        self._authorities={}
        for authority in authorities:
            kind=getattr(authority,'kind',None)
            if kind not in {'project','migration'} or kind in self._authorities or not callable(authority):
                raise ValueError('invalid_artifact_authorities')
            self._authorities[kind]=authority

    def resolve(self, kind):
        return self._authorities.get(kind)

    def __call__(self,cursor,*,token,artifact,now,read_only):
        authority=self.resolve(artifact.get('source_extension_kind'))
        if authority is None:
            raise LeaseLost('artifact_extension_unavailable')
        return authority(cursor,token=token,artifact=artifact,now=now,read_only=read_only)

    def enqueue_proof(self,cursor,*,token,artifact,job,now):
        authority=self.resolve(artifact.get('source_extension_kind'))
        if authority is None or artifact.get('source_migration_item_id') is None:
            raise LeaseLost('migration_artifact_authority_unavailable')
        return authority.enqueue_proof(cursor,token=token,artifact=artifact,job=job,now=now)

    def bind_plan(self,cursor,proof,*,plan_id,now):
        from .migration_artifact_journal import MigrationArtifactProof
        from .migration_types import require_issued
        require_issued(proof,MigrationArtifactProof)
        row=cursor.execute('SELECT source_extension_kind,source_migration_item_id FROM qq_official_file_artifacts WHERE artifact_id=?',(proof.artifact_id,)).fetchone()
        authority=self.resolve(row['source_extension_kind']) if row else None
        if authority is None or row['source_migration_item_id']!=proof.item.item_id:
            raise LeaseLost('migration_artifact_authority_unavailable')
        return authority.bind_plan(cursor,proof,plan_id=plan_id,now=now)
