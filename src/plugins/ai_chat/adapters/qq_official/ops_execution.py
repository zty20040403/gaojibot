"""Once-only host execution; a consent flag alone is not a dispatch capability."""
import asyncio
from contextlib import suppress
import json
from urllib.parse import urlsplit

from ...ops_client import OpsClient


def summary(client,value):
    text = json.dumps({'remote_status':value['status'],'result':value.get('result',{}),
        'error_code':value.get('error_code','')},ensure_ascii=False,allow_nan=False)
    address = urlsplit(client.config.base_url)
    secrets = {client.secret,client.config.base_url,client.config.secret_file,address.netloc,address.hostname or ''}
    # ASCII credentials may contain quotes/backslashes; the serialized response
    # then contains their JSON escape form, not the original byte string.
    secrets |= {json.dumps(value,ensure_ascii=False)[1:-1] for value in secrets}
    for secret in sorted(secrets,key=len,reverse=True):
        if secret:text = text.replace(secret,'[redacted]')
    raw = text.encode()
    return text if len(raw)<=16000 else raw[:15000].decode('utf-8',errors='ignore')+'\n[输出摘要已截断]'


async def drain(task):
    while not task.done():
        with suppress(asyncio.CancelledError):await asyncio.shield(task)
    return task.result()


class OpsExecution:
    def __init__(self,notices,*,transaction,calls,current,client_factory=OpsClient,sleep=asyncio.sleep):
        self.notices,self.actions = notices,notices.actions
        self.transaction,self.calls,self.current = transaction,calls,current
        self.client_factory,self.sleep = client_factory,sleep

    def claim(self,action_id):
        with self.transaction() as cursor:
            row = self.actions.retire(cursor,self.actions.get(cursor,action_id))
            if row['state']!='approved':return row,None
            try:self.notices.check(cursor,row)
            except PermissionError:
                return self.actions.close(cursor,action_id,state='cancelled',error='ops_source_closed'),None
            consent = cursor.execute("SELECT 1 FROM ops_replies WHERE inbox_id=? AND action_id=? AND actor_key=? AND decision='approved'",
                (row['approved_inbox_id'],action_id,row['actor_key'])).fetchone()
            receipt = cursor.execute("SELECT 1 FROM deliveries WHERE plan_id=? AND purpose='ops_approval' AND status='committed'",
                (row['notice_plan_id'],)).fetchone()
            if consent is None or receipt is None:
                return self.actions.close(cursor,action_id,state='cancelled',error='ops_notice_unconfirmed'),None
            now = int(self.actions.clock())
            cursor.execute("UPDATE ops_actions SET state='executing',started_at=?,updated_at=? WHERE action_id=?",(now,now,action_id))
            deadline = cursor.execute('SELECT deadline_at FROM background_jobs WHERE job_id=?',(row['job_id'],)).fetchone()['deadline_at']
            return self.actions.get(cursor,action_id),deadline

    def inspect(self,action_id):
        with self.transaction() as cursor:
            row = self.actions.get(cursor,action_id)
            self.notices.check(cursor,row)
            return row

    def check(self,action_id,binding):
        with self.transaction() as cursor:
            row = self.actions.get(cursor,action_id)
            self.notices.check(cursor,row)
            if row['state']!='executing' or row['binding_hash']!=binding:
                raise PermissionError('ops_execution_source_closed')

    def save_reference(self,action_id,ref):
        # Preserve a validated remote receipt even if owner authorization just
        # changed; this is audit, NOT permission to approve that remote intent.
        with self.transaction() as cursor:
            row = self.actions.get(cursor,action_id)
            if row['remote_id'] and (row['remote_id'],row['remote_contract'],row['remote_version'])!=(ref.operation_id,ref.contract_hash,ref.resource_version):
                raise PermissionError('ops_remote_receipt_conflict')
            cursor.execute('UPDATE ops_actions SET remote_id=?,remote_contract=?,remote_version=?,updated_at=? WHERE action_id=?',
                (ref.operation_id,ref.contract_hash,ref.resource_version,int(self.actions.clock()),action_id))

    def close(self,action_id,*,state,error='',output=''):
        with self.transaction() as cursor:
            return self.actions.close(cursor,action_id,state=state,error=error,summary=output)

    async def execute(self,action_id):
        await self.current()
        claim = asyncio.create_task(self.calls(self.claim,action_id))
        try:
            row,deadline = await asyncio.shield(claim)
        except asyncio.CancelledError:
            row,deadline = await drain(claim)
            if deadline is not None:
                await drain(asyncio.create_task(self.calls(self.close,action_id,state='unknown',error='ops_execution_interrupted')))
            raise
        if deadline is None:return await self.calls(self.inspect,action_id)  # never replay; reauthorize even terminal output
        try:
            async def current():
                await self.current()
                await self.calls(self.check,action_id,client.binding)
            def construct():
                config = self.notices.configuration()
                if config is None:raise PermissionError('ops_configuration_unavailable')
                return self.client_factory(config,config.plan(row['plan_name']),json.loads(row['parameters_json']),
                    action_id=action_id,deadline_at=deadline,current=current,clock=self.actions.clock)
            client = await asyncio.to_thread(construct)
            await current()
            ref = await client.prepare()
            await self.calls(self.save_reference,action_id,ref)
            await current()
            value = await client.approve(ref)
            end = min(deadline,self.actions.clock()+1800)
            while value['status'] in {'queued','running','cancelling'}:
                if self.actions.clock()>=end:raise TimeoutError
                await self.sleep(1)
                await current()
                value = await client.status(ref)
            await current()
            state = {'succeeded':'succeeded','failed':'failed','cancelled':'failed','needs_attention':'unknown'}.get(value['status'],'unknown')
            await self.calls(self.close,action_id,state=state,
                error='' if state=='succeeded' else 'ops_remote_'+state,output=summary(client,value))
            await self.current()
            return await self.calls(self.inspect,action_id)
        except BaseException as error:
            # Once the local execution claim exists, never let a retry recreate
            # a remote approval. Cancellation is NOT proof of remote rollback.
            task = asyncio.create_task(self.calls(self.close,action_id,state='unknown',error='ops_execution_interrupted'))
            await drain(task)
            if isinstance(error,(asyncio.CancelledError,KeyboardInterrupt,SystemExit)):raise
            if isinstance(error,PermissionError):raise
            # Unexpected implementation failures stay visible to the caller too.
            from ...ops_client import OpsTransportError
            from ...ops_plans import OpsPlanError
            if not isinstance(error,(OpsTransportError,OpsPlanError,TimeoutError)):raise
            await self.current()
            return await self.calls(self.inspect,action_id)
