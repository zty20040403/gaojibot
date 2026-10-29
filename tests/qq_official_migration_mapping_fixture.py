"""Real processed-message evidence and authenticated mapping revision helpers."""
import secrets

from src.bot_security.store import SecurityStore,digest
from src.plugins.ai_chat.adapters.qq_official.migration_console import MigrationControlAuthority
from src.plugins.ai_chat.adapters.qq_official.migration_mappings import MigrationMappingStore,mapping_request
from src.plugins.ai_chat.adapters.qq_official.migration_preview import Selection,MappingEvidence as PreviewMapping
from src.plugins.ai_chat.message_ir import MessageBody,TextNode


def install_mapping(test):
    test.mapping_cookie='TEST_MAPPING_COOKIE_'+secrets.token_hex(16)
    test.mapping_csrf='TEST_MAPPING_CSRF_'+secrets.token_hex(16)
    test.mapping_security=SecurityStore(test.database,b'TEST_MAPPING_SECURITY_SECRET_12345',clock=lambda:test.now)
    test.addCleanup(test.mapping_security.close)
    test.mapping_authority=MigrationControlAuthority(test.mapping_security,clock=lambda:test.now)
    test.mapping_store=MigrationMappingStore(authority=test.mapping_authority,routes=test.routes)
    with test.tx() as cursor:
        cursor.execute('INSERT INTO admin_sessions VALUES (?,?,?,?,?,?)',
            (digest(test.mapping_cookie),'admin',1,digest(test.mapping_csrf),test.now+1000,test.now))
    test.mapping_inbox=record_evidence(test)
    test.mapping_evidence=rotate_mapping(test)
    if hasattr(test,'preview'):
        def snapshot_mapping(cursor,selection,ref,actor):
            value=test.mapping_store.lookup(cursor,selection.source,selection.source_scope,actor,selection.target,test.now)
            return PreviewMapping(value.mapping_id,value.revision,value.source,value.target_endpoint,value.target)
        test.preview.actor_mapping=snapshot_mapping


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


def rotate_mapping(test,*,selection=None,source_actor=None,target_actor=None,inbox=None,expires=None,expected=None):
    selection=selection or Selection(test.source,test.source_scope,test.endpoint)
    source_actor=source_actor or test.source_actor
    target_actor=target_actor or test.actor
    previous=getattr(test,'mapping_evidence',None)
    request=mapping_request(selection=selection,source_actor=source_actor,target_actor=target_actor,
        evidence_inbox_id=inbox or test.mapping_inbox,
        expected_revision=expected if expected is not None else (previous.revision if previous else 0),
        expires_at=expires if expires is not None else test.now+1000)
    grant=test.mapping_authority.issue(cookie=test.mapping_cookie,csrf=test.mapping_csrf,
        action='mapping_put',request=request)
    with test.tx() as cursor:
        value=test.mapping_store.put(cursor,grant,selection=selection,source_actor=source_actor,target_actor=target_actor,
            evidence_inbox_id=request['evidence_inbox_id'],expected_revision=request['expected_revision'],
            expires_at=request['expires_at'],now=test.now)
    test.mapping_evidence=value
    return value
