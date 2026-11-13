"""Consent only from fresh, authenticated, top-level private owner messages."""
from dataclasses import dataclass,field
import hashlib
import hmac
import json
import re

from ...message_ir import TextNode
from .ops_actions import TERMINAL

_SENSITIVE = re.compile(r'批准\s*\d{6}')


def sensitive(value):
    # Includes quoted/forwarded text, cards and filenames. Redact before TxA,
    # even when this runtime has no operations tool/approval service installed.
    if isinstance(value,str):return _SENSITIVE.search(value) is not None
    if isinstance(value,dict):return any(sensitive(key) or sensitive(item) for key,item in value.items())
    if isinstance(value,list):return any(sensitive(item) for item in value)
    return False


@dataclass(frozen=True,repr=False)
class Reply:
    action_id: str
    endpoint: str
    actor: str
    native: str
    occurred: int | None
    code: str = field(repr=False)


class OpsReplies:
    def __init__(self,notices):
        self.notices,self.actions = notices,notices.actions

    def prepare(self,cursor,decoded,dispatch):
        if decoded.endpoint.kind!='private':return None
        row = cursor.execute("SELECT * FROM ops_actions WHERE endpoint_key=? AND actor_key=? AND state IN ('sending','pending')",
            (decoded.endpoint.key,decoded.actor.key)).fetchone()
        if row is None or decoded.platform_occurred_at is not None and decoded.platform_occurred_at<row['created_at']:return None
        message = dispatch.message
        match = re.fullmatch(r'批准[ \t]+([0-9]{6})',message.content.strip())
        plain = (not message.attachments and not message.elements and not message.ark_data and not message.mentions
            and not message.jump_url and not decoded.reference.referenced_index and not decoded.reference.conflicting
            and message.message_type in (None,0) and not decoded.evidence.truncated
            and len(decoded.body.nodes)==1 and isinstance(decoded.body.nodes[0],TextNode))
        return Reply(row['action_id'],decoded.endpoint.key,decoded.actor.key,decoded.native_message_id,
            decoded.platform_occurred_at,match[1] if match and plain else '')

    def commitment(self,inbox_id,reply):
        data = json.dumps([inbox_id,reply.action_id,reply.endpoint,reply.actor,reply.native,reply.occurred,reply.code],separators=(',',':'))
        return hmac.new(self.actions.pepper,b'ops-reply-v1:'+data.encode(),hashlib.sha256).hexdigest()

    def capture(self,cursor,inbox_id,reply):
        cursor.execute('''UPDATE qq_official_inbound_events SET ops_reply_action_id=?,ops_reply_actor_key=?,
            ops_reply_commitment=?,ops_reply_occurred_at=? WHERE inbox_id=?''',
            (reply.action_id,reply.actor,self.commitment(inbox_id,reply),reply.occurred,inbox_id))

    def observe(self,cursor,*,inbox_id,session_id,seq,reply):
        row = self.actions.get(cursor,reply.action_id)
        source = cursor.execute('''SELECT i.* FROM qq_official_inbound_events i JOIN qq_official_gateway_receipts g
            ON g.inbox_id=i.inbox_id AND g.account_key=i.account_key WHERE i.inbox_id=? AND g.session_id=? AND g.seq=?''',
            (inbox_id,session_id,seq)).fetchone()
        if (source is None or source['event_type']!='C2C_MESSAGE_CREATE' or source['echo_state']!='not_echo'
                or source['account_key']!=self.actions.token.account_key or source['ops_reply_action_id']!=row['action_id']
                or source['endpoint_key']!=row['endpoint_key'] or source['ops_reply_actor_key']!=row['actor_key']
                or source['native_message_id']!=reply.native or source['ops_reply_occurred_at']!=reply.occurred
                or not hmac.compare_digest(source['ops_reply_commitment'] or '',self.commitment(inbox_id,reply))):
            raise PermissionError('ops_reply_source_unavailable')
        prior = cursor.execute('SELECT decision FROM ops_replies WHERE inbox_id=?',(inbox_id,)).fetchone()
        if prior is not None:return prior['decision']
        row = self.actions.retire(cursor,row)
        if row['state'] not in TERMINAL:
            try:self.notices.check(cursor,row)
            except PermissionError:
                row = self.actions.close(cursor,row['action_id'],state='cancelled',error='ops_configuration_changed')
        now = int(self.actions.clock())
        # A fast real reply may arrive before capture_receipt/finalize. Approval
        # alone NEVER starts execution: the executor must also require committed
        # notice delivery. The notice settler cancels an unconfirmed outcome.
        sent = cursor.execute("SELECT 1 FROM deliveries WHERE plan_id=? AND purpose='ops_approval' AND status IN ('sending','accepted','committed')",
            (row['notice_plan_id'],)).fetchone()
        valid = (row['state'] in {'sending','pending'} and bool(reply.code) and sent is not None
            and source['payload_json'] is None and source['reason_code']=='ops_control' and source['status']=='processed'
            and source['endpoint_generation']==row['endpoint_generation'] and source['route_epoch']==row['route_epoch']
            and reply.occurred is not None and row['created_at']<=reply.occurred<=now and now<row['expires_at']
            and hmac.compare_digest(row['code_hash'],self.actions.code_hash(row['action_id'],reply.code)))
        decision = row['state'] if row['state'] in {'expired','cancelled'} else 'approved' if valid else 'denied'
        cursor.execute('''INSERT INTO ops_replies(inbox_id,action_id,account_key,endpoint_key,actor_key,source_session_id,source_seq,decision,created_at)
            VALUES (?,?,?,?,?,?,?,?,?)''',(inbox_id,row['action_id'],row['account_key'],row['endpoint_key'],row['actor_key'],session_id,seq,decision,now))
        if valid:
            cursor.execute("""UPDATE ops_actions SET state='approved',code_hash='',approved_inbox_id=?,approved_actor_key=?,approved_at=?,updated_at=?
                WHERE action_id=?""",(inbox_id,row['actor_key'],now,now,row['action_id']))
        elif row['state'] not in TERMINAL:
            self.actions.close(cursor,row['action_id'],state='denied',error='ops_not_approved')
        return decision
