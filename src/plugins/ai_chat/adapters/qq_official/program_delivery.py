"""A narrow, persisted program source for direct bot speech, never a human grant."""
import json
from contextlib import nullcontext

from ...message_ir import MessageBody,TextNode
from .identity_store import snapshot
from .program_actions import ProgramActions,ProgramDenied
from .renderer import render_plan
from .send_types import OutboxInvariantError,digest,stable_json
from .vm_sessions import VMSessions,source


def rendered_say(endpoint, text):
    if type(text) is not str or not text.strip() or len(text)>1800:
        raise ProgramDenied('invalid_program_text',400)
    rendered = render_plan(MessageBody((TextNode(0,text),)),endpoint)
    if rendered.status!='ready' or len(rendered.parts)!=1:
        raise ProgramDenied('invalid_program_text',400)
    return rendered


def load(cursor, action_id):
    row = cursor.execute('SELECT * FROM qq_official_program_actions WHERE action_id=?',(action_id,)).fetchone()
    if row is None or row['kind']!='say':
        raise OutboxInvariantError('program_source_unavailable')
    return row


def check(cursor, token, row, admission, now):
    sessions = VMSessions(None,token,private_admission=admission,clock=lambda:now)
    actions = ProgramActions(sessions)
    actions.check(cursor,row)
    from .program_execution import check_reply
    check_reply(cursor,actions,row)


def validate(cursor, *, token, action_id, rendered, requester, logical_key, source_scope_key,
             source_native_message_id, source_canonical_message_id, turn_id, purpose, expires_at,
             source_generation, admission, now):
    row = load(cursor,action_id)
    check(cursor,token,row,admission,now)
    expected = rendered_say(source(row).endpoint,json.loads(row['payload_json']).get('text'))
    if (rendered.to_json()!=expected.to_json() or requester!=snapshot(cursor,rendered.endpoint.account).bot_actor
            or logical_key!='program:'+action_id or source_scope_key!=row['scope_key']
            or source_native_message_id or source_canonical_message_id is not None or turn_id is not None
            or purpose!='unsolicited' or expires_at!=row['expires_at'] or source_generation!=row['conversation_generation']):
        raise OutboxInvariantError('program_source_mismatch')


def matches(cursor, row, plan):
    endpoint = source(row).endpoint
    expected = stable_json(dict(v=1,rendered=rendered_say(endpoint,json.loads(row['payload_json'])['text']).to_json(),
        control=None,source_native_message_id=''))
    return (plan['endpoint_key']==endpoint.key and plan['requester_actor_key']==snapshot(cursor,endpoint.account).bot_actor.key
        and plan['source_scope_key']==row['scope_key'] and plan['purpose']=='unsolicited'
        and plan['source_canonical_message_id'] is None and plan['turn_id'] is None
        and plan['conversation_generation']==row['conversation_generation'] and plan['expires_at']==row['expires_at']
        and plan['part_count']==1 and plan['payload_mode']=='persistent'
        and plan['manifest_json']==expected and plan['manifest_digest']==digest(expected))


def bind(cursor, action_id, plan):
    row = load(cursor,action_id)
    if row['plan_id'] not in (None,plan['plan_id']) or not matches(cursor,row,plan):
        raise OutboxInvariantError('program_plan_mismatch')
    if row['plan_id'] is None:
        cursor.execute("UPDATE qq_official_program_actions SET plan_id=?,state='queued' WHERE action_id=?",
            (plan['plan_id'],action_id))


def denial(cursor, *, token, delivery, admission, now):
    row = cursor.execute('SELECT * FROM qq_official_program_actions WHERE plan_id=?',(delivery['plan_id'],)).fetchone()
    if row is None or row['kind']!='say':
        return 'program_source_unavailable'
    try:
        check(cursor,token,row,admission,now)
        plan = cursor.execute('SELECT * FROM delivery_plans WHERE plan_id=?',(row['plan_id'],)).fetchone()
        valid = (plan is not None and matches(cursor,row,plan)
            and delivery['requester_actor_key']==plan['requester_actor_key']
            and delivery['endpoint_key']==plan['endpoint_key'])
        return None if valid else 'program_plan_mismatch'
    except PermissionError as error:
        code = str(error)
        return code if code in {'program_disabled','program_action_disabled','program_action_expired','conversation_stopped_or_replaced',
            'vm_token_revoked','vm_route_replaced','vm_endpoint_unavailable'} else 'program_source_unavailable'


def say(actions, current, *, generation, program, request_id, text, transaction_cursor=None):
    from .plans import enqueue_plan
    rendered = rendered_say(current.endpoint,text)
    with (nullcontext(transaction_cursor) if transaction_cursor is not None else actions.transaction()) as cursor:
        row,_ = actions.reserve(current,generation=generation,kind='say',program=program,
            request_id=request_id,payload={'text':text},transaction_cursor=cursor)
        ref = enqueue_plan(cursor,token=actions.sessions.lease,rendered=rendered,
            requester=snapshot(cursor,current.endpoint.account).bot_actor,logical_key='program:'+row['action_id'],
            source_scope_key=current.scope_key,expires_at=row['expires_at'],now=int(actions.clock()),purpose='unsolicited',
            admission=actions.sessions.private_admission,program_action_id=row['action_id'],source_generation=generation)
        return row['action_id'],ref
