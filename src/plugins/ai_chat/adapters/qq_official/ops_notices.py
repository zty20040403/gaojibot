"""One ephemeral owner-private consent question, reusing real QQ send receipts."""
from datetime import datetime,timezone
import hmac
import re

from ...message_ir import MessageBody,TextNode
from ...ops_plans import OpsPlanError
from ...platform_types import ActorKey,EndpointKey
from .renderer import render_plan
from .send_types import OutboxInvariantError


def question(row,code,about):
    expiry = datetime.fromtimestamp(row['expires_at'],timezone.utc).isoformat()
    return (f"运维操作单：{row['plan_name']}\n说明：{about}\n参数：{row['parameters_json']}\n有效期至：{expiry}\n"
        f"确认这一次操作请回复：\n批准 {code}\n其他回复视为拒绝；过期不执行。")


class OpsNotices:
    def __init__(self,actions,outbox,*,configuration):
        self.actions,self.outbox,self.configuration = actions,outbox,configuration

    def check(self,cursor,row):
        config = self.configuration()
        if config is None:
            raise PermissionError('ops_configuration_unavailable')
        try:
            plan = config.plan(row['plan_name'])
            self.actions.check(cursor,row,binding=config.binding(plan,config.secret()))
            return plan
        except OpsPlanError:
            raise PermissionError('ops_configuration_changed') from None

    def validate_plan(self,cursor,*,token,action_id,rendered,requester,logical_key,source_scope_key,
            source_native_message_id,source_canonical_message_id,turn_id,expires_at,now):
        row = self.actions.get(cursor,action_id)
        definition = self.check(cursor,row)
        source = cursor.execute('SELECT * FROM background_jobs WHERE job_id=?',(row['job_id'],)).fetchone()
        if (token!=self.actions.token or row['state']!='sending' or requester.key!=row['actor_key']
                or rendered.endpoint.key!=row['endpoint_key'] or logical_key!='ops:'+action_id
                or source_scope_key!=row['scope_key'] or source_native_message_id!=source['native_message_id']
                or source_canonical_message_id!=source['canonical_message_id'] or turn_id is not None
                or expires_at>row['expires_at'] or expires_at>now+30 or len(rendered.parts)!=1):
            raise OutboxInvariantError('ops_notice_source_unavailable')
        match = re.search(r'\n批准 ([0-9]{6})\n',rendered.parts[0].canonical_text)
        if match is None or not hmac.compare_digest(self.actions.code_hash(action_id,match[1]),row['code_hash']):
            raise OutboxInvariantError('ops_notice_code_invalid')
        expected = render_plan(MessageBody((TextNode(0,question(row,match[1],definition.about)),)),rendered.endpoint,markdown=False,max_parts=1)
        if rendered.plan_hash!=expected.plan_hash:
            raise OutboxInvariantError('ops_notice_content_changed')

    def bind_plan(self,cursor,action_id,plan_id):
        row = self.actions.get(cursor,action_id)
        plan = cursor.execute('SELECT * FROM delivery_plans WHERE plan_id=?',(plan_id,)).fetchone()
        if (row['notice_plan_id'] not in (None,plan_id) or plan is None or plan['ops_action_id']!=action_id
                or plan['purpose']!='ops_approval' or plan['payload_mode']!='ephemeral'
                or plan['account_key']!=row['account_key'] or plan['endpoint_key']!=row['endpoint_key']
                or plan['requester_actor_key']!=row['actor_key'] or plan['source_scope_key']!=row['scope_key']
                or plan['expires_at']>row['expires_at']):
            raise OutboxInvariantError('ops_notice_binding_invalid')
        self.check(cursor,row)
        cursor.execute('UPDATE ops_actions SET notice_plan_id=? WHERE action_id=?',(plan_id,action_id))

    def delivery_current(self,cursor,token,delivery):
        row = cursor.execute('SELECT * FROM ops_actions WHERE notice_plan_id=?',(delivery['plan_id'],)).fetchone()
        if (row is None or token!=self.actions.token or row['state'] not in {'sending','pending','approved'}
                or row['endpoint_key']!=delivery['endpoint_key'] or row['actor_key']!=delivery['requester_actor_key']
                or delivery['purpose']!='ops_approval' or delivery['payload_mode']!='ephemeral'):
            return False
        try:
            self.check(cursor,dict(row))
            return True
        except PermissionError:
            return False

    def prepare(self,cursor,proposal):
        row,code = proposal.action,proposal.code
        if code is None:
            raise OutboxInvariantError('ops_notice_already_issued')
        source = cursor.execute('SELECT * FROM background_jobs WHERE job_id=?',(row['job_id'],)).fetchone()
        definition = self.check(cursor,row)
        endpoint = EndpointKey.parse(row['endpoint_key'])
        rendered = render_plan(MessageBody((TextNode(0,question(row,code,definition.about)),)),endpoint,markdown=False,max_parts=1)
        if rendered.status!='ready':
            raise OutboxInvariantError('ops_notice_too_large')
        return self.outbox.enqueue(self.actions.token,rendered=rendered,requester=ActorKey.parse(row['actor_key']),
            logical_key='ops:'+row['action_id'],source_scope_key=row['scope_key'],purpose='ops_approval',
            expires_at=min(row['expires_at'],int(self.actions.clock())+30),ops_action_id=row['action_id'],
            source_native_message_id=source['native_message_id'],source_canonical_message_id=source['canonical_message_id'],
            transaction_cursor=cursor)

    def settled(self,cursor,action_id):
        row = self.actions.get(cursor,action_id)
        if row['state'] not in {'sending','approved'}:
            return row
        self.actions.retire(cursor,row)
        row = self.actions.get(cursor,action_id)
        if row['state'] not in {'sending','approved'}:
            return row
        try:
            self.check(cursor,row)
        except PermissionError:
            return self.actions.close(cursor,action_id,state='cancelled',error='ops_configuration_changed')
        delivered = cursor.execute("SELECT 1 FROM deliveries WHERE plan_id=? AND purpose='ops_approval' AND status='committed'",
            (row['notice_plan_id'],)).fetchone()
        if delivered is None:
            return self.actions.close(cursor,action_id,state='cancelled',error='ops_notice_unconfirmed')
        if row['state']=='sending':
            cursor.execute("UPDATE ops_actions SET state='pending',updated_at=? WHERE action_id=?",(int(self.actions.clock()),action_id))
        return self.actions.get(cursor,action_id)
