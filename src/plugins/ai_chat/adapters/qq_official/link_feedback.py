"""One ephemeral reply from a frozen authenticated control outcome."""
import json

from ...message_ir import MessageBody,TextNode
from ...platform_types import ActorKey,EndpointKey
from .renderer import render_plan
from .send_types import OutboxInvariantError

LABELS = dict(sending='等待发行结果',issued='验证码已发行',confirming='等待真实发行确认',active='当时已建立单向连接',
    expired='请求已过期',cancelled='请求已取消',denied='没有原认证 owner/群管权限',unsupported='命令不受支持',
    limited='请求过于频繁',invalid='没有可操作的连接/申请',already_linked='当时已有同向连接',revoked='已撤销此方向',
    listed='本会话涉及的连接（原请求处理时）',use_unlink='已建立的连接须用 /unlink 撤销')


def feedback_rendered(row):
    lines = ['连接控制：'+LABELS[row['status']]]
    if row['challenge_ref']:lines.append('申请：'+row['challenge_ref'])
    if row['link_ref']:lines.append('连接：'+row['link_ref'])
    entries = json.loads(row['entries_json']); shown = 0
    for entry in entries:
        direction = '→' if entry['direction']=='outgoing' else '←'; state = '●' if entry['state']=='active' else '×'
        line = f"{direction}{entry['peer']} {state} {entry['link_id']} v{entry['generation']}"
        if len('\n'.join([*lines,line]))>1850:break  # Reserve an explicit overflow/footer within the fixed 2000-char wire limit.
        lines.append(line); shown += 1
    if entries:lines.append('● 有效；× 已撤销；v 版本。')
    if shown<len(entries):lines.append(f'本条仅显示{shown}/{len(entries)}条，剩余结果过长未显示。')
    if row['status']=='listed' and not entries:lines.append('没有涉及本会话的连接。')
    if row['more']:lines.append('原结果只取前32条，仍有更多记录。')
    lines.append('连接不共享历史或身份权限；反向须另申请。')
    plan = render_plan(MessageBody((TextNode(0,'\n'.join(lines)),)),EndpointKey.parse(row['endpoint_key']),markdown=False,max_parts=1)
    if plan.status!='ready':raise OutboxInvariantError('link_feedback_render_unavailable')
    return plan


class LinkFeedback:
    def __init__(self,ingress,outbox):self.sources,self.outbox = ingress.replies,outbox

    def validate_plan(self,cursor,*,token,inbox_id,rendered,requester,logical_key,source_scope_key,
            source_native_message_id,source_canonical_message_id,turn_id,expires_at,now):
        row = self.sources.get(cursor,token,inbox_id); original = self.sources.source(cursor,token,row)
        if (rendered.plan_hash!=feedback_rendered(row).plan_hash or requester.key!=row['actor_key']
            or logical_key!='link-feedback:'+inbox_id or source_scope_key!=row['scope_key']
            or source_native_message_id!=original['native_message_id'] or source_canonical_message_id is not None or turn_id is not None
            or expires_at>min(row['expires_at'],now+30)):raise OutboxInvariantError('link_feedback_source_mismatch')

    def bind_plan(self,cursor,token,inbox_id,plan_id):
        row = self.sources.get(cursor,token,inbox_id); original = self.sources.source(cursor,token,row)
        plan = cursor.execute('SELECT * FROM delivery_plans WHERE plan_id=?',(plan_id,)).fetchone()
        deliveries = cursor.execute('SELECT * FROM deliveries WHERE plan_id=?',(plan_id,)).fetchall()
        if (plan is None or plan['link_reply_inbox_id']!=inbox_id or plan['purpose']!='link_feedback' or plan['payload_mode']!='ephemeral'
            or (plan['account_key'],plan['endpoint_key'],plan['requester_actor_key'],plan['source_scope_key'])
                !=(row['account_key'],row['endpoint_key'],row['actor_key'],row['scope_key'])
            or plan['expires_at']>row['expires_at'] or len(deliveries)!=1 or deliveries[0]['reply_to_native_message_id']!=original['native_message_id']):
            raise OutboxInvariantError('link_feedback_plan_mismatch')

    def prepare(self,cursor,token,inbox_id):
        row = self.sources.get(cursor,token,inbox_id); original = self.sources.source(cursor,token,row)
        if cursor.execute('SELECT 1 FROM delivery_plans WHERE link_reply_inbox_id=?',(inbox_id,)).fetchone():return None
        return self.outbox.enqueue(token,rendered=feedback_rendered(row),requester=ActorKey.parse(row['actor_key']),
            logical_key='link-feedback:'+inbox_id,source_scope_key=row['scope_key'],purpose='link_feedback',link_reply_inbox_id=inbox_id,
            expires_at=min(row['expires_at'],int(self.sources.authority.clock())+30),source_native_message_id=original['native_message_id'],
            transaction_cursor=cursor)

    def delivery_current(self,cursor,token,delivery):
        if delivery['purpose']!='link_feedback' or delivery['payload_mode']!='ephemeral':return False
        plan = cursor.execute('SELECT * FROM delivery_plans WHERE plan_id=?',(delivery['plan_id'],)).fetchone()
        if plan is None:return False
        try:
            row = self.sources.get(cursor,token,plan['link_reply_inbox_id']); original = self.sources.source(cursor,token,row)
            content = self.outbox._ephemeral.get(delivery['delivery_id'])
            return (delivery['account_key']==token.account_key and delivery['endpoint_key']==row['endpoint_key']
                and (delivery['endpoint_generation'],delivery['route_epoch'])==(row['endpoint_generation'],row['route_epoch'])
                and delivery['requester_actor_key']==row['actor_key'] and delivery['reply_to_native_message_id']==original['native_message_id']
                and content is not None and content.plan_hash==feedback_rendered(row).plan_hash)
        except PermissionError:return False
