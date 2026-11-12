"""Durable admin recovery intents; never callable by a VM or a model tool."""
import json
import secrets

from ...background_tools import workspace_owner
from ...platform_types import EndpointKey
from .keys import account_job_kind


class ProgramAdmin:
    def __init__(self, lifecycle, jobs):
        self.lifecycle,self.jobs = lifecycle,jobs
        self.kind = account_job_kind('program_admin',lifecycle.lease.account_key)

    def enqueue(self, cursor, endpoint, sid, expected):
        route = self.lifecycle.store._route(cursor,endpoint)
        now = int(self.lifecycle.store.clock())
        job,_ = self.jobs.enqueue(kind=self.kind,idempotency_key='program-resume:'+secrets.token_hex(16),
            scope_key=endpoint.scope.key,max_attempts=1,resume_on_lease_loss=True,now=now,transaction_cursor=cursor,
            payload=dict(endpoint_key=endpoint.key,sandbox_id=sid,expected=expected,
                generation=route['generation'],route_epoch=route['route_epoch'],deadline=now+3600))
        return dict(job_id=job.job_id,state='pending')

    def check(self, job):
        service = self.lifecycle
        if service.closed:
            raise PermissionError('program_admin_runtime_closed')
        endpoint = EndpointKey.parse(job.payload['endpoint_key'])
        with service.transaction() as cursor:
            route = service.store._route(cursor,endpoint)
            row = cursor.execute('SELECT * FROM durable_jobs WHERE job_id=? FOR UPDATE',(job.job_id,)).fetchone()
            now = int(service.store.clock())
            if (row is None or row['status']!='running' or row['kind']!=self.kind
                    or row['scope_key']!=endpoint.scope.key or row['lease_owner']!=job.lease_owner
                    or row['lease_generation']!=job.lease_generation or (row['lease_until'] or 0)<=now
                    or json.loads(row['payload_json'])!=job.payload or job.payload['deadline']<=now):
                raise PermissionError('program_admin_job_expired_or_replaced')
            if (route['generation'],route['route_epoch'])!=(job.payload['generation'],job.payload['route_epoch']):
                raise PermissionError('program_admin_route_replaced')
        return endpoint

    async def execute(self, job):
        service = self.lifecycle
        endpoint = await service.calls(self.check,job)
        await service.manager.program_state.reset(workspace_owner(endpoint),job.payload['sandbox_id'],
            expected=job.payload['expected'],request_id=str(job.job_id),authorize=lambda:service.calls(self.check,job))
        service.keeper.wake()
        return dict(reset_applied=True,synchronization='pending')
