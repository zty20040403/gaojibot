"""Minimal turn source metadata for history, files and durable child tasks.

Ordinary turns run in memory. No job lease, payload hash, retry or frozen result
is required; these rows only connect existing history and child-task records.
"""
from dataclasses import dataclass, field
import hashlib
import json
from ...conversation_scope import ConversationScope
import secrets

from .lease import assert_epoch
from ...message_ir import MessageBody, body_from_json, body_to_json
from ...platform_types import ActorKey, EndpointKey, InboundMessage, ReplyCredential


class ExecutionError(RuntimeError):
    pass


class ExecutionFenceLost(ExecutionError):
    pass


@dataclass(eq=False)
class ExecutionHandle:
    execution_id: str
    job_id: int | None
    inbox_id: str = field(repr=False)
    endpoint: EndpointKey = field(repr=False)
    actor: ActorKey = field(repr=False)
    canonical_message_id: int
    source_native_message_id: str = field(repr=False)
    scope_key: str = field(repr=False)
    endpoint_generation: int
    route_epoch: int
    account_fencing_epoch: int
    worker_owner: str = field(repr=False)
    job_lease_generation: int
    execution_revision: int
    deadline_at: int
    result_purpose: str
    execution_kind: str
    result: MessageBody | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ExecutionDecision:
    state: str
    reason: str = ""
    handle: ExecutionHandle | None = field(default=None, repr=False)
    journal_turn_id: int | None = None
    plan_id: str | None = None


def _issue(row):
    handle = ExecutionHandle(row["execution_id"], row["job_id"], row["inbox_id"],
        EndpointKey.parse(row["endpoint_key"]), ActorKey.parse(row["requester_actor_key"]),
        row["canonical_message_id"], row["source_native_message_id"], row["scope_key"], row["endpoint_generation"], row["route_epoch"],
        row["account_fencing_epoch"], row["worker_owner"], row["job_lease_generation"],
        row["execution_revision"], row["deadline_at"], row["result_purpose"], row["execution_kind"])
    return handle


def _require(handle):
    if not isinstance(handle, ExecutionHandle):
        raise ExecutionError('invalid_turn_source')


def claim_execution(cursor, *, token, job, now, deadline_at, admission, result_purpose='final', execution_kind='chat'):
    assert_epoch(cursor, token)
    inbox = cursor.execute('SELECT * FROM qq_official_inbound_events WHERE inbox_id=? AND account_key=?',
        (job.payload['inbox_id'], token.account_key)).fetchone()
    if inbox is None or inbox['canonical_message_id'] is None:
        raise ExecutionError('turn_source_unavailable')
    message = cursor.execute('SELECT m.*,c.scope_key FROM messages m JOIN conversations c USING(conversation_id) WHERE canonical_message_id=?',
        (inbox['canonical_message_id'],)).fetchone()
    endpoint, actor = EndpointKey.parse(inbox['endpoint_key']), ActorKey.parse(message['sender_actor_key'])
    if not admission(cursor, endpoint, actor):
        raise ExecutionError('turn_source_unavailable')
    existing = cursor.execute('SELECT * FROM qq_official_turn_executions WHERE inbox_id=?', (inbox['inbox_id'],)).fetchone()
    if existing is not None:
        return ExecutionDecision('already_running' if existing['state']=='executing' else existing['state'])
    fields = dict(execution_id=secrets.token_hex(16), account_key=token.account_key,
        endpoint_key=endpoint.key, requester_actor_key=actor.key, inbox_id=inbox['inbox_id'], job_id=job.job_id,
        canonical_message_id=message['canonical_message_id'], source_native_message_id=inbox['native_message_id'],
        scope_key=message['scope_key'], endpoint_generation=inbox['endpoint_generation'], route_epoch=inbox['route_epoch'],
        account_fencing_epoch=token.epoch, worker_owner=token.holder_id, job_lease_generation=1, execution_revision=1,
        state='prepared', input_digest=hashlib.sha256(inbox['inbox_id'].encode()).hexdigest(), reason_code='',
        result_purpose=result_purpose, execution_kind=execution_kind, deadline_at=deadline_at, created_at=now, updated_at=now)
    row = cursor.execute('INSERT INTO qq_official_turn_executions ('+','.join(fields)+') VALUES ('+
        ','.join('?' for _ in fields)+') RETURNING *', tuple(fields.values())).fetchone()
    return ExecutionDecision('prepared', handle=_issue(row))


def assert_execution_current(cursor, *, token, handle, job, now, admission):
    assert_epoch(cursor, token)
    if handle.endpoint.account.key != token.account_key or not admission(cursor, handle.endpoint, handle.actor):
        raise ExecutionFenceLost('turn_not_admitted')
    row = cursor.execute('SELECT * FROM qq_official_turn_executions WHERE execution_id=?', (handle.execution_id,)).fetchone()
    if row is None or row['account_fencing_epoch'] != token.epoch or row['state'] in {'cancelled','needs_attention'}:
        raise ExecutionFenceLost('turn_closed')
    return row


def begin_execution(cursor, *, journal_turn_id, **args):
    row = assert_execution_current(cursor, **args)
    cursor.execute("UPDATE qq_official_turn_executions SET state='executing',journal_turn_id=?,started_at=?,updated_at=? WHERE execution_id=?",
        (journal_turn_id, args['now'], args['now'], row['execution_id']))


def begin_command_execution(cursor, **args):
    begin_execution(cursor, journal_turn_id=None, **args)


def capture_result(cursor, *, body, turn_id, status, **args):
    row = assert_execution_current(cursor, **args)
    args['handle'].result = body
    cursor.execute("UPDATE qq_official_turn_executions SET state='result_ready',result_status=?,result_at=?,updated_at=? WHERE execution_id=?",
        (status, args['now'], args['now'], row['execution_id']))
    return True


def load_result(cursor, **args):
    row = assert_execution_current(cursor, **args)
    if args['handle'].result is not None:
        return args['handle'].result
    return body_from_json(row['result_body_json']) if row['result_body_json'] else MessageBody()


def logical_result_key(handle):
    return 'qq-turn-result:' + handle.execution_id


def input_text(cursor, **args):
    row = assert_execution_current(cursor, **args)
    return cursor.execute('SELECT turn_text FROM qq_official_inbound_events WHERE inbox_id=?', (row['inbox_id'],)).fetchone()['turn_text']


def attach_plan(cursor, *, plan_id, **args):
    row = assert_execution_current(cursor, **args)
    cursor.execute("UPDATE qq_official_turn_executions SET state='handed_off',plan_id=?,handed_off_at=?,updated_at=? WHERE execution_id=?",
        (plan_id, args['now'], args['now'], row['execution_id']))
    return True


def complete_without_plan(cursor, **args):
    return attach_plan(cursor, plan_id=None, **args)


def interrupt_execution(cursor, *, reason, **args):
    cursor.execute("UPDATE qq_official_turn_executions SET state='needs_attention',reason_code=?,updated_at=? WHERE execution_id=? AND state IN ('prepared','executing')",
        (reason, args['now'], args['handle'].execution_id))
    return 'needs_attention'


class TurnInputError(RuntimeError):
    """Fixed diagnostic only."""


def _current_reply(cursor, handle, message):
    expected = message["reply_to_native_message_id"]
    if not expected:
        return None
    source = cursor.execute("""SELECT ref_msg_idx FROM qq_official_messages
        WHERE account_key=? AND endpoint_key=? AND native_message_id=?
          AND canonical_message_id=? AND direction='inbound'""",
        (handle.endpoint.account.key, handle.endpoint.key, message["native_message_id"],
         handle.canonical_message_id)).fetchone()
    if source is None or not source["ref_msg_idx"]:
        return None
    targets = cursor.execute("""SELECT i.native_message_id,i.canonical_message_id,i.ambiguous,
        q.canonical_message_id AS mapped_id,m.source_endpoint_key,m.native_message_id AS ledger_native
        FROM qq_official_message_indexes i LEFT JOIN qq_official_messages q
          ON q.account_key=i.account_key AND q.endpoint_key=i.endpoint_key AND q.native_message_id=i.native_message_id
        LEFT JOIN messages m ON m.canonical_message_id=i.canonical_message_id
        WHERE i.account_key=? AND i.endpoint_key=? AND i.index_value=? AND i.index_kind IN ('msg_idx','ref_idx')""",
        (handle.endpoint.account.key, handle.endpoint.key, source["ref_msg_idx"])).fetchall()
    if not targets or any(row["ambiguous"] or row["native_message_id"] != expected
        or row["canonical_message_id"] is None or row["mapped_id"] != row["canonical_message_id"]
        or row["source_endpoint_key"] != handle.endpoint.key or row["ledger_native"] != expected for row in targets):
        return None
    return expected


def load_turn_input(cursor, *, execution_api, token, handle, job, now, admission):
    execution_api.assert_execution_current(cursor, token=token, handle=handle,
        job=job, now=now, admission=admission)
    row = cursor.execute("""SELECT m.*,i.event_id,i.received_at,i.trigger_requested
        FROM messages m JOIN qq_official_inbound_events i ON i.canonical_message_id=m.canonical_message_id
        WHERE i.inbox_id=? AND i.account_key=? AND i.endpoint_key=? AND i.dispatch_job_id IS NOT DISTINCT FROM ?
          AND m.canonical_message_id=?""", (handle.inbox_id, token.account_key,
        handle.endpoint.key, job.job_id, handle.canonical_message_id)).fetchone()
    if row is None:
        raise TurnInputError("turn_source_unavailable")
    try:
        body = body_from_json(row["body_json"])
        if json.loads(body_to_json(body)) != json.loads(row["body_json"]):
            raise ValueError
        scope = ConversationScope.from_key(handle.scope_key)
    except (TypeError, ValueError):
        raise TurnInputError("turn_source_body_invalid") from None
    # Revalidate references even on an explicit @ turn: a later conflicting
    # index must not introduce a wrong quoted context or journal continuation.
    # Credential values always come from the original persisted window, even
    # when expired; M3 independently checks and reserves final send authority.
    window = cursor.execute("""SELECT * FROM qq_official_reply_windows
        WHERE account_key=? AND endpoint_key=? AND endpoint_generation=? AND route_epoch=?
          AND credential_kind='msg_id' AND credential_value=? AND state IN ('active','expired')""",
        (token.account_key, handle.endpoint.key, handle.endpoint_generation,
         handle.route_epoch, row["native_message_id"])).fetchone()
    credential = None
    if window is not None:
        credential = ReplyCredential(handle.endpoint, window["credential_kind"], window["credential_value"],
            window["issued_at"], window["received_at"], window["expires_at"], window["max_replies"])
    return InboundMessage(endpoint=handle.endpoint, actor=handle.actor, event_id=row["event_id"],
        native_message_id=row["native_message_id"], sender_display=row["sender_display"],
        platform_role="", body=body, mentions_bot=bool(row["trigger_requested"]),
        reply_to_native_message_id=_current_reply(cursor, handle, row),
        occurred_at=row["occurred_at"], reply_credential=credential,
        canonical_scope=scope if scope != handle.endpoint.scope else None,
        route_epoch=handle.route_epoch)
