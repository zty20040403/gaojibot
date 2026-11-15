"""One original-endpoint ephemeral code, settled only by actual sender evidence."""
import hmac
import re

from ...message_ir import MessageBody,TextNode
from ...platform_types import ActorKey,EndpointKey
from .renderer import render_plan
from .send_types import OutboxInvariantError


def question(row,code):
    return (f"单向连接申请：{row['challenge_id']}\n此会话 → 目标会话；不共享历史或身份权限。\n"
        f"请由目标会话的宿主 owner 或群主/群管确认：\n/link {code}\n"
        "验证码最多十分钟有效；未知投递不会重发，反向连接须另行申请。")


class LinkNotices:
    def __init__(self,ingress,outbox):self.ingress,self.outbox = ingress,outbox; self.reducer = ingress.reducer

    def check(self,cursor,token,row):
        if row is None or row['state'] not in {'sending','confirming'} or row['delivery_state']!='pending':raise PermissionError('link_notice_not_pending')
        return self.ingress.source(cursor,token,row)

    def validate_plan(self,cursor,*,token,challenge_id,rendered,requester,logical_key,source_scope_key,
            source_native_message_id,source_canonical_message_id,turn_id,expires_at,now):
        row = self.reducer.get(cursor,token,challenge_id)
        if row is None:raise OutboxInvariantError('link_notice_source_unavailable')
        source = self.check(cursor,token,row)
        if (rendered.endpoint.key!=row['source_endpoint_key'] or requester.key!=row['source_actor_key']
            or logical_key!='link:'+challenge_id or source_scope_key!=row['source_scope_key']
            or source_native_message_id!=source['native_message_id'] or source_canonical_message_id is not None or turn_id is not None
            or expires_at>min(row['expires_at'],now+30) or len(rendered.parts)!=1):raise OutboxInvariantError('link_notice_source_unavailable')
        code = re.search(r'\n/link ([0-9]{6})\n',rendered.parts[0].canonical_text)
        if code is None or not hmac.compare_digest(row['code_hmac'],self.reducer.code_hash(row,code[1])):raise OutboxInvariantError('link_notice_code_invalid')
        expected = render_plan(MessageBody((TextNode(0,question(row,code[1])),)),rendered.endpoint,markdown=False,max_parts=1)
        if rendered.plan_hash!=expected.plan_hash:raise OutboxInvariantError('link_notice_content_changed')

    def bind_plan(self,cursor,token,challenge_id,plan_id):
        row = self.reducer.get(cursor,token,challenge_id)
        plan = cursor.execute('SELECT * FROM delivery_plans WHERE plan_id=?',(plan_id,)).fetchone()
        source = self.check(cursor,token,row)
        delivery = cursor.execute('SELECT reply_to_native_message_id FROM deliveries WHERE plan_id=?',(plan_id,)).fetchone()
        if (plan is None or row['notice_plan_id'] not in (None,plan_id) or plan['link_challenge_id']!=challenge_id
            or plan['purpose']!='link_otp' or plan['payload_mode']!='ephemeral' or plan['account_key']!=token.account_key
            or plan['endpoint_key']!=row['source_endpoint_key'] or plan['requester_actor_key']!=row['source_actor_key']
            or plan['source_scope_key']!=row['source_scope_key'] or plan['expires_at']>row['expires_at']
            or delivery is None or delivery['reply_to_native_message_id']!=source['native_message_id']):raise OutboxInvariantError('link_notice_binding_invalid')
        cursor.execute('UPDATE qq_official_link_challenges SET notice_plan_id=? WHERE challenge_id=?',(plan_id,challenge_id))

    def delivery_current(self,cursor,token,delivery):
        row = cursor.execute('SELECT * FROM qq_official_link_challenges WHERE notice_plan_id=?',(delivery['plan_id'],)).fetchone()
        if row is None or delivery['purpose']!='link_otp' or delivery['payload_mode']!='ephemeral':return False
        try:
            source = self.check(cursor,token,row)
            return (delivery['account_key']==token.account_key and delivery['endpoint_key']==row['source_endpoint_key']
                and delivery['requester_actor_key']==row['source_actor_key'] and delivery['reply_to_native_message_id']==source['native_message_id'])
        except PermissionError:return False

    def prepare(self,cursor,token,decision):
        if decision.code is None:raise OutboxInvariantError('link_notice_already_issued')
        row = self.reducer.get(cursor,token,decision.challenge_id); source = self.check(cursor,token,row)
        endpoint = EndpointKey.parse(row['source_endpoint_key'])
        rendered = render_plan(MessageBody((TextNode(0,question(row,decision.code)),)),endpoint,markdown=False,max_parts=1)
        return self.outbox.enqueue(token,rendered=rendered,requester=ActorKey.parse(row['source_actor_key']),
            logical_key='link:'+row['challenge_id'],source_scope_key=row['source_scope_key'],purpose='link_otp',
            link_challenge_id=row['challenge_id'],expires_at=min(row['expires_at'],self.reducer.now()+30),
            source_native_message_id=source['native_message_id'],transaction_cursor=cursor)

    def settled(self,cursor,token,plan_id):
        row = cursor.execute('SELECT * FROM qq_official_link_challenges WHERE account_key=? AND notice_plan_id=?',(token.account_key,plan_id)).fetchone()
        if row is None:return
        delivery = cursor.execute('SELECT * FROM deliveries WHERE plan_id=?',(plan_id,)).fetchone()
        if delivery is None:return self.reducer.close(cursor,row)
        receipt = cursor.execute('''SELECT r.native_message_id,r.observed_at FROM delivery_receipts r
            JOIN delivery_attempts a USING(attempt_id) WHERE a.delivery_id=? AND r.account_key=? AND r.endpoint_key=?
            AND r.kind='response' AND r.native_message_id=? AND r.applied_at IS NOT NULL ORDER BY r.observed_at,r.receipt_id LIMIT 1''',
            (delivery['delivery_id'],token.account_key,row['source_endpoint_key'],delivery['native_message_id'])).fetchone()
        if delivery['status']=='committed' and receipt:
            return self.reducer.record_issue(cursor,token,row['challenge_id'],outcome='committed',native_id=receipt['native_message_id'],received_at=receipt['observed_at'])
        if delivery['status'] in {'failed','cancelled','ambiguous'}:
            return self.reducer.record_issue(cursor,token,row['challenge_id'],outcome='unknown' if delivery['status']=='ambiguous' else 'failed')
