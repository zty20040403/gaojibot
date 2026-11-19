"""Official QQ gateway reception, deduplication, admission, and single-pass ledger write."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import hashlib
import json
import re
import secrets
import threading
import time
import unicodedata

from src.bot_storage import StoreCursor, open_store_connection
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.image_captions import ingest_captions
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MentionNode, MessageBody, TextNode, fallback_text
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey
from src.qq_official.events import EventParseError, LIFECYCLE_EVENTS, MESSAGE_EVENTS, parse_dispatch
from src.qq_official.gateway import GatewayCheckpoint

from . import peer_bots
from .admin_identity import command_commitment, prepare_identity_input
from .authority import OwnerAuthority
from .binding import BindingCommand, BindingContext, BindingDecision, BindingReducer
from .codec import DecodedMessage, RejectedMessage, decode_message
from .conversation_control import ingest as ingest_control
from .input_normalization import normalize_input, retired_approval_input
from .lease import LeaseToken, assert_epoch
from .proactive_ingress import record_group_activity


class IngressError(RuntimeError):
    """Fixed transport/invariant diagnostic; never contains payload or identity."""


def durable_key(namespace: str, *parts: str) -> str:
    encoded = json.dumps([namespace, *parts], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def valid_identifier(value, limit: int = 256) -> bool:
    try:
        return (isinstance(value, str) and bool(value.strip())
                and len(value.encode("utf-8")) <= limit
                and not any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value))
    except UnicodeError:
        return False


@dataclass(frozen=True)
class IngestResult:
    status: str
    reason_code: str = ""
    canonical_message_id: int | None = None
    echo_state: str = "unknown"
    reply_requested: bool = False
    endpoint_key: str = ""
    sender_actor_key: str = ""
    route_epoch: int = 0
    turn_text: str = ""
    conversation_control: dict | None = None
    conversation_generation: int = 0
    conversation_stopped: bool = False


@dataclass(frozen=True)
class Acceptance:
    disposition: str
    inbox_id: str | None = None
    reason: str = ""
    binding: BindingDecision | None = field(default=None, repr=False)
    _native_admission: object = field(default=None, repr=False)
    _identity_input: object = field(default=None, repr=False)
    _prepared: object = field(default=None, repr=False)
    ingested: IngestResult | None = field(default=None, repr=False)


def _ledger_body(decoded: DecodedMessage) -> MessageBody:
    return MessageBody(tuple(
        TextNode(node.segment_index, fallback_text(node))
        if isinstance(node, MentionNode) and (node.raw_data.get("is_you") is True or node.raw_data.get("bot") is True)
        else node for node in decoded.body.nodes
    ))


def _reply_target(cursor: StoreCursor, decoded: DecodedMessage, scope_key: str) -> str | None:
    reference = decoded.reference
    if reference.conflicting or reference.referenced_index is None:
        return None
    rows = cursor.execute("""SELECT i.native_message_id,i.canonical_message_id,i.ambiguous,
            m.canonical_message_id AS mapped_canonical,
            c.native_message_id AS ledger_native,c.source_endpoint_key,v.scope_key
        FROM qq_official_message_indexes i JOIN qq_official_messages m
          ON m.endpoint_key=i.endpoint_key AND m.native_message_id=i.native_message_id
            AND m.account_key=i.account_key
        LEFT JOIN messages c ON c.canonical_message_id=i.canonical_message_id
        LEFT JOIN conversations v ON v.conversation_id=c.conversation_id
        WHERE i.account_key=? AND i.endpoint_key=? AND i.index_value=?
          AND i.index_kind IN ('msg_idx','ref_idx')""",
        (decoded.endpoint.account.key, decoded.endpoint.key, reference.referenced_index)).fetchall()
    targets = set()
    for row in rows:
        if row["ambiguous"] or row["canonical_message_id"] != row["mapped_canonical"]:
            return None
        if row["canonical_message_id"] is not None and (
            row["source_endpoint_key"] != decoded.endpoint.key
            or row["scope_key"] != scope_key
            or row["ledger_native"] != row["native_message_id"]
        ):
            return None
        targets.add((str(row["native_message_id"]), row["canonical_message_id"]))
    return next(iter(targets))[0] if len(targets) == 1 else None


def _record_index(cursor: StoreCursor, decoded: DecodedMessage, canonical: int, now: int) -> None:
    native_index = decoded.reference.message_index
    if decoded.reference.conflicting or native_index is None:
        return
    cursor.execute("""INSERT INTO qq_official_message_indexes
        (endpoint_key,account_key,index_kind,index_value,native_message_id,
         canonical_message_id,ambiguous,created_at,updated_at)
        VALUES (?,?,'msg_idx',?,?,?,FALSE,?,?)
        ON CONFLICT(endpoint_key,index_kind,index_value) DO UPDATE SET
            ambiguous=qq_official_message_indexes.ambiguous
                OR qq_official_message_indexes.native_message_id<>EXCLUDED.native_message_id
                OR (qq_official_message_indexes.canonical_message_id IS NOT NULL
                    AND qq_official_message_indexes.canonical_message_id<>EXCLUDED.canonical_message_id),
            updated_at=EXCLUDED.updated_at""",
        (decoded.endpoint.key, decoded.endpoint.account.key, native_index, decoded.native_message_id,
         canonical, now, now))


def _write_to_ledger(
    cursor: StoreCursor, *, inbox_id: str, account: AccountKey, endpoint: EndpointKey,
    endpoint_row, decoded: DecodedMessage, input_text: str, input_version: int,
    trigger_requested: bool, ledger: MessageLedger, now: int, owner_authority=None,
) -> IngestResult:
    scope_key = str(endpoint_row["canonical_scope_key"])
    reply_target = _reply_target(cursor, decoded, scope_key)
    recorded = ledger.record_message(
        ConversationScope.from_key(scope_key), native_message_id=decoded.native_message_id,
        sender_native_user_id=decoded.actor.native_user_id, sender_display=peer_bots.display(decoded),
        body=_ledger_body(decoded), occurred_at=decoded.occurred_at,
        reply_to_native_message_id=reply_target, actor=decoded.actor, source_endpoint=endpoint,
        raw_event={"source": "qq-official", "message_type": decoded.evidence.message_type,
                   "reference_conflict": decoded.reference.conflicting,
                   "codec_warnings": list(decoded.evidence.warnings), "truncated": decoded.evidence.truncated,
                   "sender_is_bot": decoded.sender_is_bot},
        transaction_cursor=cursor,
    )
    ingest_captions(cursor, recorded, now)
    canonical = recorded.canonical_message_id
    cursor.execute("""INSERT INTO qq_official_messages
        (message_id,account_key,endpoint_key,native_message_id,canonical_message_id,
         direction,msg_idx,ref_msg_idx,inbox_id,created_at)
        VALUES (?,?,?,?,?,'inbound',?,?,?,?)""",
        (secrets.token_hex(16), account.key, endpoint.key, decoded.native_message_id, canonical,
         decoded.reference.message_index, decoded.reference.referenced_index, inbox_id, now))
    _record_index(cursor, decoded, canonical, now)
    cursor.execute("""UPDATE qq_official_inbound_events SET status='processed',reason_code=NULL,
        canonical_message_id=?,echo_state='not_echo',turn_text=?,turn_text_version=?,
        processed_at=?,updated_at=? WHERE inbox_id=? AND account_key=?""",
        (canonical, input_text, input_version, now, now, inbox_id, account.key))
    receipt, current = ingest_control(
        cursor, inbox_id=inbox_id, decoded=decoded, text=input_text, now=now, authority=owner_authority,
    )
    command = input_text.lstrip().startswith(("/", "!"))
    reply = endpoint.kind == "private" or trigger_requested or decoded.mentions_bot or reply_target is not None
    reply = bool(receipt) or bool(reply and (not current["stopped"] or command))
    if reply and decoded.sender_is_bot and peer_bots.exchange_exhausted(cursor, endpoint.key, now):
        reply = False
    cursor.execute("UPDATE qq_official_inbound_events SET trigger_requested=? WHERE inbox_id=? AND account_key=?",
                   (reply, inbox_id, account.key))
    record_group_activity(cursor, endpoint, canonical, now, direct=reply or command or current['stopped'])
    return IngestResult(
        "processed", "", canonical, echo_state="not_echo", reply_requested=reply,
        endpoint_key=endpoint.key, sender_actor_key=decoded.actor.key,
        route_epoch=int(endpoint_row["route_epoch"]), turn_text=input_text,
        conversation_control=receipt, conversation_generation=int(current["stop_generation"]),
        conversation_stopped=bool(current["stopped"]),
    )


def record_pending_inbox(
    cursor: StoreCursor, *, inbox_id: str, lease_token: LeaseToken, ledger: MessageLedger,
    epoch_guard=assert_epoch, admission=None, now: int | None = None, owner_authority=None,
) -> IngestResult:
    """Record a pending inbox event into the ledger inside the caller's transaction."""
    timestamp = int(time.time()) if now is None else now
    epoch_guard(cursor, lease_token)
    account = AccountKey.parse(lease_token.account_key)
    row = cursor.execute("SELECT * FROM qq_official_inbound_events WHERE inbox_id=? AND account_key=?",
                         (inbox_id, account.key)).fetchone()
    if row is None:
        return IngestResult("missing", "inbox_not_found")
    if row["status"] != "pending":
        return IngestResult(str(row["status"]), str(row["reason_code"] or ""),
                            row["canonical_message_id"], str(row["echo_state"]), bool(row["trigger_requested"]))
    endpoint_row = cursor.execute("""SELECT * FROM qq_official_endpoints
        WHERE endpoint_key=? AND account_key=? FOR UPDATE""", (row["endpoint_key"], account.key)).fetchone()
    row = cursor.execute("""SELECT * FROM qq_official_inbound_events
        WHERE inbox_id=? AND account_key=? FOR UPDATE""", (inbox_id, account.key)).fetchone()
    if row["status"] != "pending":
        return IngestResult(str(row["status"]), str(row["reason_code"] or ""),
                            row["canonical_message_id"], str(row["echo_state"]), bool(row["trigger_requested"]))

    def finish(status="processed", reason="", canonical=None, echo="not_echo"):
        cursor.execute("""UPDATE qq_official_inbound_events SET status=?,reason_code=?,
            canonical_message_id=?,echo_state=?,processed_at=?,updated_at=? WHERE inbox_id=? AND account_key=?""",
            (status, reason or None, canonical, echo, timestamp, timestamp, inbox_id, account.key))
        return IngestResult(status, reason, canonical, echo)

    if endpoint_row is None:
        return finish("quarantined", "missing_endpoint")
    endpoint = EndpointKey.parse(str(row["endpoint_key"]))
    if (row["endpoint_generation"] != endpoint_row["generation"]
            or row["route_epoch"] != endpoint_row["route_epoch"]
            or endpoint_row["binding_state"] == "revoked" or endpoint_row["membership_state"] == "absent"
            or (endpoint.kind == "group" and (endpoint_row["binding_state"] != "active"
                                              or endpoint_row["membership_state"] != "present"))):
        return finish("revoked", "endpoint_no_longer_admitted")
    if row["payload_expires_at"] is not None and row["payload_expires_at"] <= timestamp:
        return finish("skipped", "payload_expired")
    try:
        dispatch = parse_dispatch(row["payload_json"])
        decoded = decode_message(dispatch, account, row["received_at"])
    except (EventParseError, ValueError, TypeError):
        return finish("quarantined", "invalid_payload")
    if isinstance(decoded, RejectedMessage):
        return finish("processed" if decoded.disposition == "bot_origin" else "quarantined",
                      "bot_origin" if decoded.disposition == "bot_origin" else decoded.reason,
                      echo="possible_self" if decoded.disposition == "bot_origin" else "not_echo")
    existing = cursor.execute("""SELECT * FROM qq_official_messages
        WHERE account_key=? AND endpoint_key=? AND native_message_id=?""",
        (account.key, endpoint.key, decoded.native_message_id)).fetchone()
    if existing is not None and existing["direction"] == "outbound":
        return finish(reason="outbound_echo", echo="confirmed_self")
    if admission is None or admission(cursor, endpoint, decoded.actor) is not True:
        return finish("revoked", "policy_denied")
    if existing is not None:
        return finish(reason="duplicate_message", canonical=existing["canonical_message_id"])
    prepared_input = normalize_input(dispatch)
    if prepared_input.kind != "message" or retired_approval_input(
            prepared_input, dispatch.message.content, private=endpoint.kind == "private"):
        cursor.execute("UPDATE qq_official_inbound_events SET payload_json=NULL WHERE inbox_id=?", (inbox_id,))
        return finish("quarantined", "input_sensitive_control")
    return _write_to_ledger(
        cursor, inbox_id=inbox_id, account=account, endpoint=endpoint, endpoint_row=endpoint_row,
        decoded=decoded, input_text=prepared_input.input_text, input_version=prepared_input.version,
        trigger_requested=bool(row["trigger_requested"]), ledger=ledger, now=timestamp,
        owner_authority=owner_authority,
    )


class OfficialInboxStore:
    def __init__(self, database, *, binding: BindingReducer, jobs=None,
                 authority: OwnerAuthority | None = None, private_policy: str = "owner_only",
                 private_allowed_openids: tuple[str, ...] = (), payload_retention_hours: int = 24,
                 ledger: MessageLedger | None = None, clock=time.time, epoch_guard=assert_epoch):
        if private_policy not in {"owner_only", "allowlist", "public"}:
            raise ValueError("Invalid official private admission policy")
        if not 1 <= payload_retention_hours <= 168:
            raise ValueError("Invalid official inbox retention")
        self.database = database
        self.path, self._connection = open_store_connection(database)
        self.binding = binding
        self.authority = authority or binding.authority
        self.private_policy = private_policy
        self.private_allowed_openids = frozenset(private_allowed_openids)
        self.payload_retention_seconds = payload_retention_hours * 3600
        self.ledger = ledger
        self.clock, self.epoch_guard = clock, epoch_guard
        self._lock = threading.RLock()
        self.source_authority = None
        self.admin_identity_service = None

    def close(self):
        with self._lock:
            self._connection.close()

    @contextmanager
    def transaction(self):
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute("BEGIN")
                yield cursor
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()
            finally:
                cursor.close()

    def ensure_account(self, account: AccountKey):
        if account.platform != "qq-official" or len(account.key.encode()) > 512:
            raise ValueError("Invalid official account configuration")
        now = int(self.clock())
        with self.transaction() as cursor:
            cursor.execute("""INSERT INTO qq_official_accounts
                (account_key,enabled,gateway_state,created_at,updated_at)
                VALUES (?,TRUE,'idle',?,?) ON CONFLICT(account_key) DO NOTHING""",
                (account.key, now, now))

    def apply_configuration(self, token, owner_openids: tuple[str, ...]):
        account = AccountKey.parse(token.account_key)
        revision = durable_key("owner-config-v1", account.key, *sorted(owner_openids))
        with self.transaction() as cursor:
            self.epoch_guard(cursor, token)
            return self.authority.apply_trusted_config(cursor, account, owner_openids, revision)

    def admitted(self, cursor, endpoint: EndpointKey, actor: ActorKey) -> bool:
        if not actor.matches_endpoint(endpoint) or actor.identity_kind == "bot":
            return False
        row = cursor.execute("SELECT * FROM qq_official_endpoints WHERE endpoint_key=? AND account_key=?",
                             (endpoint.key, endpoint.account.key)).fetchone()
        if row is None or row["binding_state"] == "revoked" or row["membership_state"] == "absent":
            return False
        if endpoint.kind == "group":
            return row["binding_state"] == "active" and row["membership_state"] == "present"
        if self.private_policy == "public":
            return True
        if self.authority.owner_for_actor(cursor, endpoint, actor) is not None:
            return True
        return self.private_policy == "allowlist" and actor.native_user_id in self.private_allowed_openids

    def checkpoint(self, token) -> GatewayCheckpoint | None:
        with self.transaction() as cursor:
            self.epoch_guard(cursor, token)
            row = cursor.execute("SELECT session_id,durable_seq FROM qq_official_accounts WHERE account_key=?",
                                 (token.account_key,)).fetchone()
            return GatewayCheckpoint(row["session_id"], int(row["durable_seq"])) if row["session_id"] else None

    def checkpoint_callback(self, token, checkpoint: GatewayCheckpoint | None):
        with self.transaction() as cursor:
            self.epoch_guard(cursor, token)
            if checkpoint is None:
                previous = cursor.execute("SELECT session_id FROM qq_official_accounts WHERE account_key=?",
                                          (token.account_key,)).fetchone()
                if previous["session_id"] is None:
                    return
                cursor.execute("""UPDATE qq_official_accounts SET session_id=NULL,durable_seq=NULL,
                    gateway_state='connecting',updated_at=? WHERE account_key=?""",
                    (int(self.clock()), token.account_key))
                return
            row = cursor.execute("SELECT session_id,durable_seq FROM qq_official_accounts WHERE account_key=?",
                                 (token.account_key,)).fetchone()
            if row["session_id"] != checkpoint.session_id:
                raise IngressError("Official checkpoint session changed")
            cursor.execute("""UPDATE qq_official_accounts SET durable_seq=GREATEST(durable_seq,?),updated_at=?
                WHERE account_key=?""", (checkpoint.seq, int(self.clock()), token.account_key))

    def pause(self, token, reason: str):
        if not re.fullmatch(r"[a-z0-9_]{1,80}", reason):
            raise ValueError("Invalid official pause reason")
        with self.transaction() as cursor:
            self.epoch_guard(cursor, token)
            cursor.execute("""UPDATE qq_official_accounts SET gateway_state='paused',pause_reason=?,updated_at=?
                WHERE account_key=?""", (reason, int(self.clock()), token.account_key))

    def accept(self, frame: dict, *, token, session_id: str, received_at: int | None = None,
               ledger: MessageLedger | None = None) -> Acceptance:
        account = AccountKey.parse(token.account_key)
        active_ledger = ledger if ledger is not None else self.ledger
        now = int(self.clock()) if received_at is None else received_at
        if (not isinstance(frame, dict) or frame.get("op") != 0 or type(frame.get("s")) is not int
                or not 0 <= frame["s"] < 2**63 or not valid_identifier(session_id, 512)
                or type(now) is not int or not 0 <= now < 2**63 - self.payload_retention_seconds):
            raise IngressError("Invalid official dispatch sequence envelope")
        seq = frame["s"]
        supplied_type = frame.get("t")
        event_type = supplied_type if isinstance(supplied_type, str) and re.fullmatch(r"[A-Z0-9_]{1,128}", supplied_type) else "UNKNOWN"
        try:
            dispatch = parse_dispatch(frame)
        except (EventParseError, ValueError, TypeError, RecursionError):
            dispatch = None
        with self.transaction() as cursor:
            self.epoch_guard(cursor, token)
            state = cursor.execute("SELECT session_id,durable_seq FROM qq_official_accounts WHERE account_key=?",
                                   (account.key,)).fetchone()
            if event_type == "READY":
                if state["session_id"] not in {None, session_id} or dispatch is None or dispatch.data.get("session_id") != session_id:
                    raise IngressError("Official READY does not match checkpoint session")
            elif state["session_id"] != session_id:
                raise IngressError("Official dispatch does not match checkpoint session")
            existing = cursor.execute("""SELECT * FROM qq_official_gateway_receipts
                WHERE account_key=? AND session_id=? AND seq=?""", (account.key, session_id, seq)).fetchone()
            if existing is not None:
                if existing["event_type"] != event_type:
                    raise IngressError("Official sequence identity conflict")
                return Acceptance("duplicate", existing["inbox_id"], "duplicate_sequence")
            previous = state["durable_seq"] if state["session_id"] == session_id else None
            if previous is not None and seq != previous + 1:
                raise IngressError("Official sequence is not contiguous")
            if previous is None and event_type != "READY":
                raise IngressError("Official session requires READY")
            if dispatch is None or event_type == "UNKNOWN":
                result = self._terminal(cursor, account, event_type, now, "invalid_payload")
            elif event_type in MESSAGE_EVENTS:
                result = self._message(cursor, account, dispatch, now, token)
            elif event_type in LIFECYCLE_EVENTS:
                result = self._lifecycle(cursor, account, dispatch, now)
            else:
                result = Acceptance("control" if event_type in {"READY", "RESUMED"} else "skipped", reason="control_event")
            cursor.execute("""INSERT INTO qq_official_gateway_receipts
                (account_key,session_id,seq,event_type,disposition,inbox_id,received_at) VALUES (?,?,?,?,?,?,?)""",
                (account.key, session_id, seq, event_type, result.disposition, result.inbox_id, now))
            if result._native_admission is not None:
                self.source_authority.record(cursor, token=token, proof=result._native_admission,
                    inbox_id=result.inbox_id, session_id=session_id, seq=seq)
                result = replace(result, _native_admission=None)
            if result._identity_input is not None and self.admin_identity_service is not None:
                self.admin_identity_service.observe(cursor, inbox_id=result.inbox_id, command=result._identity_input)
                result = replace(result, _identity_input=None)
            prepared = result._prepared
            if result._prepared is not None:
                result = replace(result, _prepared=None)
            if active_ledger is not None and result.disposition == "inbox":
                if prepared is not None:
                    decoded, endpoint, endpoint_row, prepared_input = prepared
                    ingested = _write_to_ledger(
                        cursor, inbox_id=result.inbox_id, account=account, endpoint=endpoint,
                        endpoint_row=endpoint_row, decoded=decoded, input_text=prepared_input.input_text,
                        input_version=prepared_input.version, trigger_requested=decoded.mentions_bot,
                        ledger=active_ledger, now=now, owner_authority=self.authority,
                    )
                else:
                    row = cursor.execute("""SELECT i.*,m.sender_actor_key FROM qq_official_inbound_events i
                        LEFT JOIN messages m USING(canonical_message_id) WHERE i.inbox_id=?""",
                        (result.inbox_id,)).fetchone()
                    if row is not None and row["status"] == "pending":
                        ingested = record_pending_inbox(
                            cursor, inbox_id=result.inbox_id, lease_token=token, ledger=active_ledger,
                            epoch_guard=self.epoch_guard, admission=self.admitted, now=now,
                            owner_authority=self.authority,
                        )
                    elif row is not None:
                        ingested = IngestResult(
                            str(row["status"]), str(row["reason_code"] or ""), row["canonical_message_id"],
                            echo_state=str(row["echo_state"]), reply_requested=bool(row["trigger_requested"]),
                            endpoint_key=str(row["endpoint_key"] or ""),
                            sender_actor_key=str(row["sender_actor_key"] or ""),
                            route_epoch=int(row["route_epoch"] or 0),
                            turn_text=str(row["turn_text"] or ""),
                            conversation_control=row["conversation_control"],
                            conversation_generation=int(row["conversation_generation"] or 0),
                            conversation_stopped=bool(row["conversation_stopped"]),
                        )
                    else:
                        ingested = IngestResult("missing", "inbox_not_found")
                result = replace(result, ingested=ingested)
            cursor.execute("""UPDATE qq_official_accounts SET session_id=?,durable_seq=?,gateway_state='active',updated_at=?
                WHERE account_key=?""", (session_id, seq, now, account.key))
            return result

    def _endpoint(self, cursor, endpoint: EndpointKey, now: int):
        cursor.execute("""INSERT INTO qq_official_endpoints
            (endpoint_key,account_key,kind,native_conversation_id,canonical_scope_key,membership_state,created_at,updated_at)
            VALUES (?,?,?,?,?,'present',?,?) ON CONFLICT(endpoint_key) DO NOTHING""",
            (endpoint.key, endpoint.account.key, endpoint.kind, endpoint.native_conversation_id, endpoint.scope.key, now, now))
        row = cursor.execute("SELECT * FROM qq_official_endpoints WHERE endpoint_key=? FOR UPDATE", (endpoint.key,)).fetchone()
        if row["account_key"] != endpoint.account.key or row["kind"] != endpoint.kind or row["native_conversation_id"] != endpoint.native_conversation_id:
            raise IngressError("Official endpoint identity conflict")
        return row

    @staticmethod
    def _aliases(account, endpoint, event_id, native_id):
        aliases = []
        if valid_identifier(event_id):
            aliases.append((durable_key("event", account.key, endpoint.key if endpoint else "", event_id),
                            "event", endpoint.key if endpoint else None, event_id))
        if endpoint is not None and valid_identifier(native_id):
            aliases.append((durable_key("message", account.key, endpoint.key, native_id),
                            "message", endpoint.key, native_id))
        return aliases

    def _duplicate(self, cursor, account, aliases, now, *, event_type, endpoint, native_id=None,
                   actor=None, trigger_requested=False):
        inbox_ids = set()
        for key, kind, alias_endpoint, native in aliases:
            row = cursor.execute("SELECT * FROM qq_official_inbound_dedup WHERE dedup_key=?", (key,)).fetchone()
            if row is not None:
                if (row["account_key"], row["key_kind"], row["endpoint_key"], row["native_id"]) != (account.key, kind, alias_endpoint, native):
                    raise IngressError("Official dedup identity conflict")
                inbox_ids.add(row["inbox_id"])
        if len(inbox_ids) > 1:
            return self._terminal(cursor, account, event_type, now, "conflicting_message_aliases")
        if not inbox_ids:
            return None
        inbox_id = inbox_ids.pop()
        original = cursor.execute("SELECT * FROM qq_official_inbound_events WHERE inbox_id=?", (inbox_id,)).fetchone()
        current_endpoint = endpoint.key if endpoint else None
        same_family = (original["event_type"] == event_type
                       or original["event_type"] in MESSAGE_EVENTS and event_type in MESSAGE_EVENTS)
        if not same_family or original["endpoint_key"] != current_endpoint or original["native_message_id"] != native_id:
            return self._terminal(cursor, account, event_type, now, "conflicting_message_aliases")
        if actor is not None and original["payload_json"] is not None:
            first = decode_message(parse_dispatch(original["payload_json"]), account, original["received_at"])
            if not isinstance(first, DecodedMessage) or first.actor != actor:
                return self._terminal(cursor, account, event_type, now, "conflicting_message_aliases")
        self._save_aliases(cursor, account, aliases, inbox_id, now)
        if (trigger_requested and not original["trigger_requested"] and actor is not None
                and original["echo_state"] == "not_echo" and original["payload_json"] is not None
                and original["payload_expires_at"] is not None and original["payload_expires_at"] > now
                and original["status"] in {"pending", "processed"}
                and (original["status"] == "pending" or original["canonical_message_id"] is not None)):
            target = cursor.execute("SELECT * FROM qq_official_endpoints WHERE endpoint_key=? FOR UPDATE",
                                    (endpoint.key,)).fetchone()
            if (target["generation"] == original["endpoint_generation"]
                    and target["route_epoch"] == original["route_epoch"]
                    and target["canonical_scope_key"] == endpoint.scope.key
                    and self.admitted(cursor, endpoint, actor)):
                if self.source_authority is not None:
                    from .migration_types import MigrationDenied
                    try:
                        self.source_authority.require_inbox(cursor, inbox_id, endpoint=endpoint,
                                                            actor=actor, operation="ingest")
                    except MigrationDenied:
                        return Acceptance("duplicate", inbox_id, "migration_route_unavailable")
                cursor.execute("UPDATE qq_official_inbound_events SET trigger_requested=TRUE,updated_at=? WHERE inbox_id=?",
                               (now, inbox_id))
                if original['status'] == 'processed':
                    record_group_activity(cursor, endpoint, original['canonical_message_id'], now, direct=True)
                return Acceptance("inbox", inbox_id, "new_at_event")
        return Acceptance("duplicate", inbox_id, "duplicate_message")

    @staticmethod
    def _save_aliases(cursor, account, aliases, inbox_id, now):
        for key, kind, endpoint, native in aliases:
            cursor.execute("""INSERT INTO qq_official_inbound_dedup
                (dedup_key,account_key,key_kind,endpoint_key,native_id,inbox_id,created_at)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(dedup_key) DO NOTHING""",
                (key, account.key, kind, endpoint, native, inbox_id, now))

    def _insert(self, cursor, account, event_type, now, *, endpoint=None, endpoint_row=None,
                native_id=None, event_id=None, status="pending", reason=None, payload=None, echo="unknown", trigger=False):
        inbox_id = secrets.token_hex(16)
        cursor.execute("""INSERT INTO qq_official_inbound_events
            (inbox_id,account_key,endpoint_key,endpoint_generation,route_epoch,event_type,native_message_id,event_id,
             payload_json,status,reason_code,echo_state,trigger_requested,received_at,payload_expires_at,created_at,updated_at,processed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (inbox_id, account.key, endpoint.key if endpoint else None,
             endpoint_row["generation"] if endpoint else None, endpoint_row["route_epoch"] if endpoint else None,
             event_type, native_id, event_id, payload, status, reason, echo, trigger, now,
             now + self.payload_retention_seconds if payload is not None else None, now, now,
             None if status == "pending" else now))
        return inbox_id

    def _terminal(self, cursor, account, event_type, now, reason):
        inbox_id = self._insert(cursor, account, event_type, now, status="quarantined", reason=reason)
        return Acceptance("quarantined", inbox_id, reason)

    def _message(self, cursor, account, dispatch, now, token):
        decoded = decode_message(dispatch, account, now)
        endpoint = decoded.endpoint
        endpoint_row = self._endpoint(cursor, endpoint, now) if endpoint is not None else None
        native_id, event_id = decoded.native_message_id, decoded.event_id
        aliases = self._aliases(account, endpoint, event_id, native_id)
        duplicate = self._duplicate(
            cursor, account, aliases, now, event_type=dispatch.event_type, endpoint=endpoint, native_id=native_id,
            actor=decoded.actor if isinstance(decoded, DecodedMessage) else None,
            trigger_requested=decoded.mentions_bot if isinstance(decoded, DecodedMessage) else False,
        )
        if duplicate is not None:
            return duplicate
        if isinstance(decoded, DecodedMessage) and endpoint_row["membership_state"] != "present":
            if endpoint_row["membership_state"] == "absent":
                cursor.execute("""UPDATE qq_official_endpoints SET generation=generation+1,route_epoch=route_epoch+1,
                    capability_revision=capability_revision+1,proactive_state='unknown',updated_at=? WHERE endpoint_key=?""",
                    (now, endpoint.key))
                self._retire_evidence(cursor, account.key, now, endpoint_key=endpoint.key)
            cursor.execute("UPDATE qq_official_endpoints SET membership_state='present',updated_at=? WHERE endpoint_key=?",
                           (now, endpoint.key))
            endpoint_row = cursor.execute("SELECT * FROM qq_official_endpoints WHERE endpoint_key=?",
                                          (endpoint.key,)).fetchone()
        native_admission = None
        if isinstance(decoded, DecodedMessage) and self.source_authority is not None:
            from .migration_types import MigrationDenied
            try:
                native_admission = self.source_authority.admit(cursor, token=token, decoded=decoded, received_at=now)
            except MigrationDenied:
                result = self._terminal(cursor, account, dispatch.event_type, now, "migration_route_unavailable")
                self._save_aliases(cursor, account, aliases, result.inbox_id, now)
                return result
        decision = None
        identity_input = None
        payload = None
        prepared = None
        status, reason, echo = "skipped", "not_admitted", "not_echo"
        if not isinstance(decoded, DecodedMessage):
            if decoded.disposition == "bot_origin":
                status, reason, echo = "skipped", "bot_origin", "possible_self"
            else:
                status, reason = "quarantined", decoded.reason
        else:
            sent = cursor.execute("""SELECT 1 FROM qq_official_messages
                WHERE account_key=? AND endpoint_key=? AND native_message_id=? AND direction='outbound'""",
                (account.key, endpoint.key, native_id)).fetchone()
            prepared_input = normalize_input(dispatch)
            command = prepared_input.command
            if (command is None and endpoint.kind == 'group' and prepared_input.kind == 'message'
                    and re.fullmatch(r'[0-9]{6}', prepared_input.input_text.strip())
                    and cursor.execute('SELECT 1 FROM qq_official_accounts WHERE account_key=? AND owner_link_code_hmac IS NOT NULL',
                                       (account.key,)).fetchone() is not None):
                command = BindingCommand('owner_entry', code=prepared_input.input_text.strip())
            identity_command = prepare_identity_input(dispatch)
            if sent is not None:
                reason, echo = "self_echo", "confirmed_self"
            elif identity_command.kind != "ordinary":
                if (identity_command.kind == "control" and endpoint.kind == "private"
                        and decoded.platform_occurred_at is not None):
                    status, reason, identity_input = "processed", "admin_identity_control", identity_command
                else:
                    status, reason = "quarantined", "input_sensitive_identity"
            elif prepared_input.kind == "quarantine":
                status, reason = "quarantined", prepared_input.reason
            elif command is not None:
                decision = self.binding.apply(
                    cursor,
                    context=BindingContext(endpoint, decoded.actor,
                        durable_key("binding-receipt", account.key, str(dispatch.seq), event_id or "", native_id),
                        now, native_id),
                    command=command, lease_token=token,
                )
                status, reason = "processed", "binding_control"
                endpoint_row = cursor.execute("SELECT * FROM qq_official_endpoints WHERE endpoint_key=?",
                                              (endpoint.key,)).fetchone()
            elif admin_input := retired_approval_input(prepared_input, dispatch.message.content,
                                                       private=endpoint.kind == 'private'):
                status, reason = (('processed', 'admin_control_unavailable') if admin_input == 'control'
                                  else ('quarantined', 'input_sensitive_approval'))
            elif self.admitted(cursor, endpoint, decoded.actor):
                payload = json.dumps({"op": 0, "t": dispatch.event_type, "s": dispatch.seq,
                                      "id": dispatch.event_id, "d": dispatch.data},
                                     ensure_ascii=False, separators=(",", ":"))
                if len(payload.encode()) > 262144:
                    status, reason, payload = "quarantined", "payload_storage_limit", None
                else:
                    status, reason = "pending", None
                    prepared = (decoded, endpoint, endpoint_row, prepared_input)
        inbox_id = self._insert(
            cursor, account, dispatch.event_type, now, endpoint=endpoint, endpoint_row=endpoint_row,
            native_id=native_id, event_id=event_id, status=status, reason=reason, payload=payload, echo=echo,
            trigger=isinstance(decoded, DecodedMessage) and decoded.mentions_bot and status == "pending",
        )
        if identity_input is not None:
            cursor.execute("""UPDATE qq_official_inbound_events
                SET identity_control_actor_key=?,identity_control_occurred_at=?,identity_control_commitment=?
                WHERE inbox_id=?""",
                (decoded.actor.key, decoded.platform_occurred_at,
                 command_commitment(self.binding._pepper, inbox_id=inbox_id, endpoint=endpoint,
                                    actor=decoded.actor, native_id=native_id, command=identity_input), inbox_id))
        if decision is not None:
            cursor.execute("""UPDATE qq_official_inbound_events
                SET control_actor_key=?,control_action=?,control_decision_code=? WHERE inbox_id=?""",
                (decoded.actor.key, command.action, decision.status, inbox_id))
        self._save_aliases(cursor, account, aliases, inbox_id, now)
        disposition = ("inbox" if status == "pending" else "quarantined" if status == "quarantined"
                       else "control" if decision or identity_input or reason == "admin_control_unavailable" else "skipped")
        return Acceptance(disposition, inbox_id, reason or "", decision,
                          native_admission, identity_input, prepared)

    def _lifecycle(self, cursor, account, dispatch, now):
        body = dispatch.data
        if dispatch.event_id is not None and not valid_identifier(dispatch.event_id):
            return self._terminal(cursor, account, dispatch.event_type, now, "invalid_lifecycle_identity")
        group = dispatch.event_type.startswith("GROUP_")
        native = body.get("group_openid") if group else body.get("openid") or body.get("user_openid")
        if not valid_identifier(native):
            return self._terminal(cursor, account, dispatch.event_type, now, "invalid_lifecycle_endpoint")
        endpoint = EndpointKey(account, "group" if group else "private", native)
        if len(endpoint.key.encode()) > 1000:
            return self._terminal(cursor, account, dispatch.event_type, now, "invalid_lifecycle_endpoint")
        row = self._endpoint(cursor, endpoint, now)
        aliases = self._aliases(account, endpoint, dispatch.event_id, None)
        duplicate = self._duplicate(cursor, account, aliases, now, event_type=dispatch.event_type, endpoint=endpoint)
        if duplicate is not None:
            return duplicate
        if dispatch.event_type in {"GROUP_ADD_ROBOT", "GROUP_DEL_ROBOT"}:
            removed = dispatch.event_type == "GROUP_DEL_ROBOT"
            desired_membership = "absent" if removed else "present"
            if row["membership_state"] == desired_membership:
                inbox_id = self._insert(cursor, account, dispatch.event_type, now, endpoint=endpoint, endpoint_row=row,
                                        event_id=dispatch.event_id, status="processed", reason="lifecycle_no_change")
                self._save_aliases(cursor, account, aliases, inbox_id, now)
                return Acceptance("control", inbox_id, "lifecycle_no_change")
            binding_state = "revoked" if removed or row["binding_state"] != "unbound" else "unbound"
            cursor.execute("""UPDATE qq_official_endpoints SET generation=generation+1,route_epoch=route_epoch+1,
                capability_revision=capability_revision+1,binding_state=?,membership_state=?,proactive_state='unknown',updated_at=?
                WHERE endpoint_key=?""", (binding_state, "absent" if removed else "present", now, endpoint.key))
            self._retire_evidence(cursor, account.key, now, endpoint_key=endpoint.key)
        elif dispatch.event_type.endswith("_MSG_REJECT"):
            cursor.execute("""UPDATE qq_official_endpoints SET proactive_state='denied',proactive_probe_attempt_id=NULL,proactive_probe_at=NULL,
                updated_at=? WHERE endpoint_key=?""", (now, endpoint.key))
        elif dispatch.event_type.endswith("_MSG_RECEIVE"):
            cursor.execute("""UPDATE qq_official_endpoints SET proactive_state='allowed',proactive_probe_attempt_id=NULL,proactive_probe_at=NULL,
                updated_at=? WHERE endpoint_key=?""", (now, endpoint.key))
        inbox_id = self._insert(cursor, account, dispatch.event_type, now, endpoint=endpoint, endpoint_row=row,
                                event_id=dispatch.event_id if valid_identifier(dispatch.event_id) else None,
                                status="processed", reason="lifecycle_control")
        self._save_aliases(cursor, account, aliases, inbox_id, now)
        return Acceptance("control", inbox_id, "lifecycle_control")

    def purge_terminal_payloads(self, token, *, now: int | None = None) -> int:
        timestamp = int(self.clock()) if now is None else now
        with self.transaction() as cursor:
            self.epoch_guard(cursor, token)
            cursor.execute("""UPDATE qq_official_inbound_events SET payload_json=NULL,updated_at=?
                WHERE account_key=? AND payload_json IS NOT NULL AND payload_expires_at<=?
                  AND status<>'pending'""", (timestamp, token.account_key, timestamp))
            return max(cursor.rowcount, 0)

    @staticmethod
    def _retire_evidence(cursor, account_key, now, *, endpoint_key=None):
        clause = "account_key=?" + (" AND endpoint_key=?" if endpoint_key is not None else "")
        params = (account_key, endpoint_key) if endpoint_key is not None else (account_key,)
        cursor.execute(f"""UPDATE qq_official_owner_actor_links SET state='revoked',revision=revision+1,updated_at=?
            WHERE {clause} AND state='active'""", (now, *params))
        cursor.execute(f"""UPDATE qq_official_binding_challenges SET state='cancelled',code_hmac='',updated_at=?,consumed_at=?
            WHERE challenge_id IN (SELECT challenge_id FROM qq_official_binding_candidates WHERE {clause} AND state='pending')""",
            (now, now, *params))
        cursor.execute(f"""UPDATE qq_official_binding_candidates SET state='cancelled',updated_at=?,consumed_at=?
            WHERE {clause} AND state='pending'""", (now, now, *params))
