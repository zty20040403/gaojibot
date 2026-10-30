import secrets
from src.plugins.ai_chat.message_ir import MessageBody,TextNode

def record_evidence(test,*,endpoint=None,actor=None,received_at=None,status='processed',echo='not_echo',gateway=True):
    endpoint,actor=endpoint or test.endpoint,actor or test.actor
    received=test.now if received_at is None else received_at
    native='TEST_MAPPING_MESSAGE_'+secrets.token_hex(8)
    inbox=secrets.token_hex(16)
    with test.tx() as cursor:
        message=test.ledger.record_message(endpoint.scope,native_message_id=native,
            sender_native_user_id=actor.native_user_id,sender_display='Test user',
            body=MessageBody((TextNode(0,'TEST mapping evidence'),)),occurred_at=received,
            actor=actor,source_endpoint=endpoint,transaction_cursor=cursor)
        cursor.execute('''INSERT INTO qq_official_inbound_events
            (inbox_id,account_key,endpoint_key,endpoint_generation,route_epoch,event_type,
             native_message_id,status,echo_state,canonical_message_id,received_at,created_at,updated_at,processed_at)
            VALUES (?,?,?,1,1,'GROUP_AT_MESSAGE_CREATE',?,?,?,?,?,?,?,?)''',
            (inbox,endpoint.account.key,endpoint.key,native,status,echo,message.canonical_message_id,
             received,received,received,received))
        if gateway:
            cursor.execute('''INSERT INTO qq_official_gateway_receipts
                (account_key,session_id,seq,event_type,disposition,inbox_id,received_at)
                VALUES (?,?,0,'GROUP_AT_MESSAGE_CREATE','inbox',?,?)''',
                (endpoint.account.key,'TEST_SESSION_'+inbox,inbox,received))
    return inbox

