"""Current authority for stored artifacts after their producer turn has ended.

This adapter checks existing durable job leases; it introduces no queue or grant
from model input. M3 historical receipt settlement intentionally does not call it.
"""
from __future__ import annotations

from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.agent.control import LeaseLost
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from .file_artifacts import _ref, _time, file_job_kind


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
    if artifact.get('source_extension_kind') == 'project':
        if source_authority is None:
            raise LeaseLost('project_artifact_authority_unavailable')
        source_authority(cursor,token=token,artifact=dict(artifact),now=now,read_only=read_only)
    if not read_only and artifact['source_task_id'] is not None:
        task=cursor.execute('SELECT cancel_requested,status FROM subagent_tasks WHERE task_id=?',
            (artifact['source_task_id'],)).fetchone()
        if task is None or task['cancel_requested'] or task['status']=='cancelled':
            raise LeaseLost('artifact_task_cancelled')
    expiry = artifact['retain_until'] if read_only else min(artifact['pending_deadline_at'],artifact['retain_until'])
    if artifact['bytes_state']!='available' or now>=expiry:
        raise LeaseLost('artifact_retention_expired')
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
