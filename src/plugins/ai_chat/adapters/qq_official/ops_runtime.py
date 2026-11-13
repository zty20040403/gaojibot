"""Owner-private job orchestration; no operation capability is granted to a VM."""
import asyncio
import json

from ...ops_plans import OpsPlanError
from ...tools.background import definition
from html import escape
from .ops_actions import OpsActions,TERMINAL
from .ops_execution import OpsExecution,drain
from .ops_notices import OpsNotices
from .ops_replies import OpsReplies
from .send_types import OutboxInvariantError


def tool(catalog):
    return definition('ops_act','仅执行已声明操作单。宿主私聊询问原owner，等待最多十分钟的纯文本“批准 <短码>”，不要通过ask_main代批。'
        '调用会等待批准及执行观察；排队不是成功，unknown可能仍已执行，不得换request_id盲目重试。'
        '同一次意图沿用request_id；新操作才用新ID。运维输出仅用于当前owner私聊，不转发到群。操作单：'+json.dumps(catalog,ensure_ascii=False),
        {'request_id':{'type':'string','pattern':'^[A-Za-z0-9_-]{1,64}$'},'plan':{'type':'string','enum':[item['name'] for item in catalog]},
         'parameters':{'type':'object'}},('request_id','plan','parameters'))


class OfficialOps:
    def __init__(self,runtime,token,app):
        self.runtime,self.token,self.app,self.closed = runtime,token,app,False
        self.calls,self.transaction = runtime._calls,runtime.store.transaction
        self.actions = OpsActions(token,authority=runtime.store.authority,admission=runtime.store.admitted,
            pepper=runtime.loaded.binding_pepper,clock=runtime.store.clock)
        self.notices = OpsNotices(self.actions,runtime.outbox,configuration=self.configuration)
        self.replies = OpsReplies(self.notices)
        self.previous = runtime.outbox.ops_authority,runtime.store.ops_replies

    def configuration(self):
        host = getattr(self.app,'official_host',None)
        if self.closed or self.app.official_runtime is not self.runtime or host is None or host.config.account.key!=self.token.account_key:
            return None
        return host.config.operations

    def catalog(self,job):
        config = self.configuration()
        if config is None:return []
        try:
            with self.transaction() as cursor:self.actions._source(cursor,job.job_id,job.lease_owner,job.lease_generation)
        except PermissionError:return []
        return config.catalog()

    async def start(self):
        await self.calls(self.recover)
        self.runtime.outbox.ops_authority,self.runtime.store.ops_replies = self.notices,self.replies

    def recover(self,*,closing=False):
        with self.transaction() as cursor:
            rows = cursor.execute("SELECT * FROM ops_actions WHERE account_key=? AND state NOT IN ('succeeded','failed','denied','expired','cancelled','unknown')",
                (self.token.account_key,)).fetchall()
            for row in rows:
                state = 'unknown' if row['state']=='executing' else 'expired' if row['expires_at']<=self.actions.clock() else 'cancelled'
                self.actions.close(cursor,row['action_id'],state=state,error='ops_runtime_closed' if closing else 'ops_runtime_restarted')

    async def close(self):
        if self.closed:return
        self.closed = True
        if self.runtime.outbox.ops_authority is self.notices:self.runtime.outbox.ops_authority = self.previous[0]
        if self.runtime.store.ops_replies is self.replies:self.runtime.store.ops_replies = self.previous[1]
        await self.calls(self.recover,closing=True)

    def prepare(self,job,args,holder):
        if type(args) is not dict or args.keys()!={'request_id','plan','parameters'}:
            raise OpsPlanError('invalid_ops_request')
        config = self.configuration()
        if config is None:raise PermissionError('ops_configuration_unavailable')
        plan = config.plan(args['plan']); binding = config.binding(plan,config.secret())
        with self.transaction() as cursor:
            proposal = self.actions.request(cursor,job=job,plan=plan,parameters=args['parameters'],binding=binding,request_id=args['request_id'])
            ref = None
            if proposal.code is not None:
                try:ref = self.notices.prepare(cursor,proposal)
                except (OutboxInvariantError,PermissionError):
                    self.actions.close(cursor,proposal.action['action_id'],state='cancelled',error='ops_notice_unavailable')
        holder.update(action_id=proposal.action['action_id'],issued=proposal.code is not None)
        return ref

    def settle(self,action_id):
        with self.transaction() as cursor:return self.notices.settled(cursor,action_id)

    def inspect(self,job,action_id):
        if self.configuration() is None:raise PermissionError('ops_configuration_unavailable')
        with self.transaction() as cursor:
            self.actions._source(cursor,job.job_id,job.lease_owner,job.lease_generation)
            row = self.actions.get(cursor,action_id)
            if row['job_id']!=job.job_id:raise PermissionError('ops_original_job_unavailable')
            row = self.actions.retire(cursor,row)
            if row['state'] not in TERMINAL:
                try:self.notices.check(cursor,row)
                except PermissionError:
                    row = self.actions.close(cursor,action_id,state='unknown' if row['state']=='executing' else 'cancelled',error='ops_source_closed')
            ready = row['state'] not in {'sending','pending','approved'}
            if row['state']=='approved':
                notice = cursor.execute('SELECT status FROM deliveries WHERE plan_id=?',(row['notice_plan_id'],)).fetchone()
                ready = notice is not None and notice['status']=='committed'
            return row,ready

    def abandon(self,action_id):
        with self.transaction() as cursor:
            row = self.actions.get(cursor,action_id)
            if row['state'] in {'sending','pending','approved'}:
                self.actions.close(cursor,action_id,state='cancelled',error='ops_request_interrupted')

    async def act(self,job,args,*,current):
        holder = {}
        def prepare():
            try:return self.prepare(job,args,holder)
            except Exception as error:
                holder['error'] = error
                raise
        async def authorized():
            await current()
            if self.configuration() is None:raise PermissionError('ops_runtime_unavailable')
        try:
            await authorized()
            # emit_ephemeral drains the real sender and drops its in-memory code.
            # Duplicate request IDs return no plan and never issue another code.
            await self.runtime.sender.emit_ephemeral(prepare)
            if 'error' in holder:raise holder['error']
            if 'action_id' not in holder:raise OpsPlanError('ops_notice_unavailable')
            action_id = holder['action_id']
            if holder.get('issued'):await self.calls(self.settle,action_id)
            while True:
                await authorized()
                row,ready = await self.calls(self.inspect,job,action_id)
                if ready:break
                await asyncio.sleep(0.25)
            execution = OpsExecution(self.notices,transaction=self.transaction,calls=self.calls,current=authorized)
            if row['state']=='approved':row = await execution.execute(action_id)
            await authorized()
            row = await self.calls(execution.inspect,action_id)
            return {'action':row['action_id'],'request_id':row['request_id'],'plan':row['plan_name'],'state':row['state'],
                'error':row['error_code'],'output_summary':'<untrusted>\n'+escape(row['output_summary'])+'\n</untrusted>',
                'note':'结果不明，可能仍在执行；不要自动换ID重试。' if row['state']=='unknown' else '只有succeeded表示执行成功；不表示服务健康或效果已另行验收。'}
        except BaseException:
            if holder.get('issued'):
                await drain(asyncio.create_task(self.calls(self.abandon,holder['action_id'])))
            raise
