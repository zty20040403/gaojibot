"""Authenticated TxA link reduction/capture; no sender or raw control retention."""
import hmac

from .linking import LinkReducer
from .lease import assert_epoch


class LinkIngress:
    def __init__(self,authority):self.authority = authority; self.reducer = LinkReducer(authority)
    def __repr__(self):return 'LinkIngress(<protected authority>)'
    def apply(self,cursor,token,message,command):return self.reducer.apply(cursor,token,message,command)

    def capture(self,cursor,token,message,inbox_id,decision):
        assert_epoch(cursor,token)
        inbox = cursor.execute('SELECT * FROM qq_official_inbound_events WHERE inbox_id=?',(inbox_id,)).fetchone()
        if (inbox is None or (inbox['account_key'],inbox['endpoint_key'],inbox['native_message_id'],inbox['event_id'],inbox['received_at'])
                !=(token.account_key,message.endpoint.key,message.native_message_id,message.event_id,message.received_at)
            or inbox['payload_json'] is not None or inbox['status']!='processed' or inbox['reason_code']!='link_control' or inbox['echo_state']!='not_echo'):
            raise ValueError('link_original_input_mismatch')
        if (message.timestamp_source!='platform' or type(message.platform_occurred_at) is not int
            or not 0<=message.platform_occurred_at<=message.received_at):return
        receipt = self.authority.receipt(message)
        cursor.execute('''UPDATE qq_official_inbound_events SET link_control_actor_key=?,link_control_receipt_hash=?,link_control_platform_at=? WHERE inbox_id=?''',
            (message.actor.key,receipt,message.platform_occurred_at,inbox_id))
        if decision.code is not None:
            challenge = self.reducer.get(cursor,token,decision.challenge_id)
            if challenge is None or challenge['source_actor_key']!=message.actor.key or not hmac.compare_digest(challenge['source_receipt_hash'],receipt):
                raise ValueError('link_original_challenge_mismatch')
            cursor.execute('UPDATE qq_official_link_challenges SET source_inbox_id=? WHERE challenge_id=?',(inbox_id,decision.challenge_id))

    def source(self,cursor,token,row):
        """One original gateway receipt, not a later convenient reply window."""
        assert_epoch(cursor,token)
        if not self.reducer.current(cursor,token,row):raise PermissionError('link_authority_changed')
        source = cursor.execute('SELECT * FROM qq_official_inbound_events WHERE inbox_id=?',(row['source_inbox_id'],)).fetchone()
        if (source is None or source['payload_json'] is not None or source['status']!='processed' or source['reason_code']!='link_control'
            or source['link_control_platform_at'] is None or source['received_at']!=row['created_at']
            or (source['endpoint_generation'],source['route_epoch'])!=(row['source_generation'],row['source_route_epoch'])
            or (source['account_key'],source['endpoint_key'],source['link_control_actor_key'])
                !=(row['account_key'],row['source_endpoint_key'],row['source_actor_key'])):raise PermissionError('link_original_input_unavailable')
        receipt = self.authority._hash('qq-link-receipt:v1',[row['source_endpoint_key'],row['source_actor_key'],source['native_message_id'],
            source['event_id'],source['link_control_platform_at'],source['received_at']])
        if not hmac.compare_digest(receipt,row['source_receipt_hash']):raise PermissionError('link_original_receipt_changed')
        gateway = cursor.execute('''SELECT 1 FROM qq_official_gateway_receipts WHERE account_key=? AND inbox_id=?
            AND event_type=? AND disposition='control' AND received_at=?''',
            (token.account_key,source['inbox_id'],source['event_type'],source['received_at'])).fetchone()
        if gateway is None:raise PermissionError('link_authenticated_receipt_unavailable')
        if not self.reducer.current(cursor,token,row):raise PermissionError('link_authority_changed')
        return source
