"""Atomic official reception, privacy admission and gateway checkpointing.

The gateway acknowledges only after this transaction commits. This is input
evidence, not a second task queue: the existing durable job store owns workers.
"""
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

from src.bot_storage import open_store_connection
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey
from src.qq_official.events import EventParseError, LIFECYCLE_EVENTS, MESSAGE_EVENTS, parse_dispatch
from src.qq_official.gateway import GatewayCheckpoint

from .authority import OwnerAuthority
from .binding import BindingContext, BindingDecision, BindingReducer
from .input_normalization import normalize_input
from .approval_control import (ControlResult, prepare_approval_input, control_commitment, apply_control)
from .control_receipts import record_decision, enqueue_control_job
from .codec import DecodedMessage, decode_message
from .lease import assert_epoch
from .keys import account_job_kind


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
class Acceptance:
    disposition: str
    inbox_id: str | None = None
    reason: str = ""
    binding: BindingDecision | None = field(default=None, repr=False)
    approval: ControlResult | None = field(default=None, repr=False)
    _approval_input: object = field(default=None, repr=False)
    _native_admission: object = field(default=None, repr=False)


class OfficialInboxStore:
    def __init__(self, database, *, jobs, binding: BindingReducer,
                 authority: OwnerAuthority | None = None, private_policy: str = "owner_only",
                 private_allowed_openids: tuple[str, ...] = (), payload_retention_hours: int = 24,
                 clock=time.time, epoch_guard=assert_epoch):
        if private_policy not in {"owner_only", "allowlist", "public"}:
            raise ValueError("Invalid official private admission policy")
        if not 1 <= payload_retention_hours <= 168:
            raise ValueError("Invalid official inbox retention")
        self.database = database
        self.path, self._connection = open_store_connection(database)
        self.jobs, self.binding = jobs, binding
        self.authority = authority or binding.authority
        self.private_policy = private_policy
        self.private_allowed_openids = frozenset(private_allowed_openids)
        self.payload_retention_seconds = payload_retention_hours * 3600
        self.clock, self.epoch_guard = clock, epoch_guard
        self._lock = threading.RLock()
        # Set only by the current authenticated runtime epoch; no factory = deny.
        self.approval_store = None
        self.source_authority = None

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
        """Trusted local factory only; never called by an inbound event.

        Existing disabled/paused accounts are not silently reactivated. Explicit
        control-plane action is required after an operator has stopped one.
        """
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
        if self.authority.c2c_grant(cursor, actor) is not None:
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
                previous = cursor.execute("SELECT session_id FROM qq_official_accounts WHERE account_key=?", (token.account_key,)).fetchone()
                if previous["session_id"] is None:
                    return
                # A new Identify cannot prove lifecycle continuity. Retain the
                # ordinary manual binding as desired configuration, but require
                # fresh presence and retire all prior sensitive grants/routes.
                cursor.execute("""UPDATE qq_official_endpoints SET membership_state='unknown',
                    generation=generation+1,route_epoch=route_epoch+1,capability_revision=capability_revision+1,
                    proactive_state='unknown',updated_at=? WHERE account_key=?""", (int(self.clock()), token.account_key))
                self._retire_evidence(cursor, token.account_key, int(self.clock()))
                cursor.execute("""UPDATE qq_official_accounts SET session_id=NULL,durable_seq=NULL,
                    gateway_state='connecting',updated_at=? WHERE account_key=?""",
                    (int(self.clock()), token.account_key))
                return
            row = cursor.execute("SELECT session_id,durable_seq FROM qq_official_accounts WHERE account_key=?",
                                 (token.account_key,)).fetchone()
            if row["session_id"] != checkpoint.session_id or row["durable_seq"] != checkpoint.seq:
                raise IngressError("Official checkpoint was not atomically accepted")

    def pause(self, token, reason: str):
        if not re.fullmatch(r"[a-z0-9_]{1,80}", reason):
            raise ValueError("Invalid official pause reason")
        with self.transaction() as cursor:
            self.epoch_guard(cursor, token)
            cursor.execute("""UPDATE qq_official_accounts SET gateway_state='paused',pause_reason=?,updated_at=?
                WHERE account_key=?""", (reason, int(self.clock()), token.account_key))

    def accept(self, frame: dict, *, token, session_id: str, received_at: int | None = None) -> Acceptance:
        account = AccountKey.parse(token.account_key)
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
            if result._approval_input is not None:
                control = apply_control(cursor, store=self.approval_store, inbox_id=result.inbox_id,
                    session_id=session_id, seq=seq, command=result._approval_input, now=now)
                result = replace(result, approval=control, _approval_input=None)
            if result.binding is not None and result.inbox_id is not None:
                control_id = record_decision(cursor, token, inbox_id=result.inbox_id,
                    source_session_id=session_id, source_seq=seq, now=now)
                enqueue_control_job(cursor, token, control_id, self.jobs, now)
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

    def _aliases(self, account, endpoint, event_id, native_id):
        aliases = []
        if valid_identifier(event_id):
            aliases.append((durable_key("event", account.key, event_id), "event", None, event_id))
        if endpoint is not None and valid_identifier(native_id):
            aliases.append((durable_key("message", account.key, endpoint.key, native_id), "message", endpoint.key, native_id))
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
        if (not same_family or original["endpoint_key"] != current_endpoint
                or original["native_message_id"] != native_id):
            return self._terminal(cursor, account, event_type, now, "conflicting_message_aliases")
        if actor is not None and original["payload_json"] is not None:
            first = decode_message(parse_dispatch(original["payload_json"]), account, original["received_at"])
            if not isinstance(first, DecodedMessage) or first.actor != actor:
                return self._terminal(cursor, account, event_type, now, "conflicting_message_aliases")
        self._save_aliases(cursor, account, aliases, inbox_id, now)
        if (trigger_requested and actor is not None and original["echo_state"] == "not_echo"
                and original["payload_json"] is not None
                and original["payload_expires_at"] is not None and original["payload_expires_at"] > now
                and original["status"] in {"pending", "processed"}
                and (original["status"] == "pending" or original["canonical_message_id"] is not None)):
            target = cursor.execute("SELECT * FROM qq_official_endpoints WHERE endpoint_key=? FOR UPDATE", (endpoint.key,)).fetchone()
            if (target["generation"] == original["endpoint_generation"] and target["route_epoch"] == original["route_epoch"]
                    and target["canonical_scope_key"] == endpoint.scope.key and self.admitted(cursor, endpoint, actor)):
                if self.source_authority is not None:
                    from .migration_types import MigrationDenied
                    try:
                        self.source_authority.require_inbox(cursor, inbox_id, endpoint=endpoint,
                            actor=actor, operation='ingest')
                    except MigrationDenied:
                        return Acceptance('duplicate', inbox_id, 'migration_route_unavailable')
                cursor.execute("UPDATE qq_official_inbound_events SET trigger_requested=TRUE,updated_at=? WHERE inbox_id=?", (now, inbox_id))
                if original["status"] == "processed":
                    from .ingest import ensure_turn_dispatch
                    ensure_turn_dispatch(cursor, original, target, self.jobs, now)
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
        duplicate = self._duplicate(cursor, account, aliases, now,
            event_type=dispatch.event_type, endpoint=endpoint, native_id=native_id,
            actor=decoded.actor if isinstance(decoded, DecodedMessage) else None,
            trigger_requested=decoded.mentions_bot if isinstance(decoded, DecodedMessage) else False)
        if duplicate is not None:
            return duplicate
        if isinstance(decoded, DecodedMessage) and endpoint_row["membership_state"] != "present":
            if endpoint_row["membership_state"] == "absent":
                cursor.execute("""UPDATE qq_official_endpoints SET generation=generation+1,route_epoch=route_epoch+1,
                    capability_revision=capability_revision+1,proactive_state='unknown',updated_at=? WHERE endpoint_key=?""",
                    (now, endpoint.key))
                self._retire_evidence(cursor, account.key, now, endpoint_key=endpoint.key)
            # Current authenticated transport proves the bot can receive here.
            # A prior DEL's revoked binding is deliberately not reactivated.
            cursor.execute("UPDATE qq_official_endpoints SET membership_state='present',updated_at=? WHERE endpoint_key=?", (now, endpoint.key))
            endpoint_row = cursor.execute("SELECT * FROM qq_official_endpoints WHERE endpoint_key=?", (endpoint.key,)).fetchone()
        native_admission = None
        if isinstance(decoded, DecodedMessage) and self.source_authority is not None:
            from .migration_types import MigrationDenied
            try:
                native_admission = self.source_authority.admit(cursor, token=token,
                    decoded=decoded, received_at=now)
            except MigrationDenied:
                # A paused route is terminal metadata, not a retried event or
                # permission to run a binding/approval reducer before the fence.
                result = self._terminal(cursor, account, dispatch.event_type, now, 'migration_route_unavailable')
                self._save_aliases(cursor, account, aliases, result.inbox_id, now)
                return result
        decision = None
        approval_input = None
        approval = None
        payload = None
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
            admin_input = prepare_approval_input(dispatch, endpoint)
            if sent is not None:
                reason, echo = "self_echo", "confirmed_self"
            elif prepared_input.kind == "quarantine":
                status, reason = "quarantined", prepared_input.reason
            elif command is not None:
                decision = self.binding.apply(cursor,
                    context=BindingContext(endpoint, decoded.actor,
                        durable_key("binding-receipt", account.key, str(dispatch.seq), event_id or "", native_id), now),
                    command=command, lease_token=token)
                status, reason = "processed", "binding_control"
                # A reducer can change this source route. Freeze the actual
                # post-decision route and observe ONLY the original event time.
                endpoint_row = cursor.execute("SELECT * FROM qq_official_endpoints WHERE endpoint_key=?", (endpoint.key,)).fetchone()
                if decision.status != "limited":
                    from .ingest import observe_reply_window
                    observe_reply_window(cursor, decoded, endpoint_row, now)
            elif admin_input.kind == 'quarantine':
                status, reason = 'quarantined', 'input_sensitive_approval'
            elif admin_input.kind == 'control':
                status, reason = 'processed', 'admin_control_unavailable'
                approval = ControlResult('denied')
                store = self.approval_store
                if store is not None:
                    from src.bot_security.store import SecurityError
                    from .approval_transport import Recipient
                    if store.transport is None or store.transport.token != token:
                        raise IngressError('Official approval epoch mismatch')
                    status, reason, approval_input = 'processed', 'admin_control', admin_input
                    # Only a current real admin may obtain a reply window, and
                    # only this authenticated event's ORIGINAL timestamp is used.
                    try:
                        store.transport.account_for_recipient(cursor, Recipient(endpoint, decoded.actor))
                    except (SecurityError, PermissionError):
                        pass
                    else:
                        from .ingest import observe_reply_window
                        observe_reply_window(cursor, decoded, endpoint_row, now)
            elif self.admitted(cursor, endpoint, decoded.actor):
                payload = json.dumps({"op": 0, "t": dispatch.event_type, "s": dispatch.seq,
                                      "id": dispatch.event_id, "d": dispatch.data}, ensure_ascii=False, separators=(",", ":"))
                if len(payload.encode()) > 262144:
                    status, reason, payload = "quarantined", "payload_storage_limit", None
                else:
                    status, reason = "pending", None
        inbox_id = self._insert(cursor, account, dispatch.event_type, now, endpoint=endpoint, endpoint_row=endpoint_row,
            native_id=native_id, event_id=event_id, status=status, reason=reason, payload=payload, echo=echo,
            trigger=isinstance(decoded, DecodedMessage) and decoded.mentions_bot and status == "pending")
        if approval_input is not None:
            commitment = control_commitment(self.approval_store.transport.secret,
                inbox_id=inbox_id, endpoint=endpoint, actor=decoded.actor, command=approval_input)
            cursor.execute("""UPDATE qq_official_inbound_events
                SET admin_control_actor_key=?,admin_control_commitment=? WHERE inbox_id=?""",
                (decoded.actor.key, commitment, inbox_id))
        if decision is not None:
            cursor.execute("""UPDATE qq_official_inbound_events
                SET control_actor_key=?,control_action=?,control_decision_code=? WHERE inbox_id=?""",
                (decoded.actor.key, command.action, decision.status, inbox_id))
        self._save_aliases(cursor, account, aliases, inbox_id, now)
        if status == "pending":
            kind = account_job_kind("ingest", account.key)
            lane = "qq-endpoint:" + durable_key("endpoint", endpoint.key)
            job_payload = {"inbox_id": inbox_id}
            job, _ = self.jobs.enqueue(kind=kind,
                idempotency_key="qq-ingest:" + durable_key("ingest", account.key, inbox_id),
                scope_key=lane, payload=job_payload,
                resume_on_lease_loss=True, now=now, transaction_cursor=cursor)
            if job.kind != kind or job.scope_key != lane or job.payload != job_payload:
                raise IngressError("Official ingest job identity conflict")
            cursor.execute("UPDATE qq_official_inbound_events SET ingest_job_id=? WHERE inbox_id=?", (job.job_id, inbox_id))
        disposition = "inbox" if status == "pending" else "quarantined" if status == "quarantined" else "control" if decision or approval else "skipped"
        return Acceptance(disposition, inbox_id, reason or "", decision, approval, approval_input, native_admission)

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
            # A fresh lifecycle event creates a new membership generation.
            # Rejoining alone cannot revive a prior binding or owner link.
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
            cursor.execute("UPDATE qq_official_reply_windows SET state='revoked',updated_at=? WHERE endpoint_key=?", (now, endpoint.key))
            cursor.execute("UPDATE qq_official_owner_actor_links SET state='revoked',revision=revision+1,updated_at=? WHERE endpoint_key=? AND state='active'", (now, endpoint.key))
            cursor.execute("""UPDATE qq_official_binding_challenges SET state='cancelled',code_hmac='',updated_at=?,consumed_at=?
                WHERE challenge_id IN (SELECT challenge_id FROM qq_official_binding_candidates WHERE endpoint_key=? AND state='pending')""",
                (now, now, endpoint.key))
            cursor.execute("""UPDATE qq_official_binding_candidates SET state='cancelled',updated_at=?,consumed_at=?
                WHERE endpoint_key=? AND state='pending'""", (now, now, endpoint.key))
        elif dispatch.event_type.endswith("_MSG_REJECT"):
            cursor.execute("""UPDATE qq_official_endpoints SET proactive_state='denied',capability_revision=capability_revision+1,
                updated_at=? WHERE endpoint_key=?""", (now, endpoint.key))
        elif dispatch.event_type.endswith("_MSG_RECEIVE"):
            # A lifecycle permission signal remains separate from implemented
            # support and from unverified permission for a particular payload.
            cursor.execute("""UPDATE qq_official_endpoints SET proactive_state='unknown',capability_revision=capability_revision+1,
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
            cursor.execute("""UPDATE qq_official_inbound_events i SET payload_json=NULL,updated_at=?
                WHERE i.account_key=? AND i.payload_json IS NOT NULL AND i.payload_expires_at<=?
                  AND i.status<>'pending'
                  AND NOT EXISTS (SELECT 1 FROM durable_jobs j WHERE j.job_id IN (i.ingest_job_id,i.dispatch_job_id)
                      AND j.status IN ('pending','running'))""", (timestamp, token.account_key, timestamp))
            return max(cursor.rowcount, 0)

    def check_pending_ingest(self, token):
        """Do not advertise a healthy receiver with an unrecoverable queue gap.

        Cancelled/failed jobs require an explicit operator decision; silently
        recreating them could undo that decision. Active leases recover through
        the existing durable queue, including its crash-resume policy.
        """
        with self.transaction() as cursor:
            self.epoch_guard(cursor, token)
            broken = cursor.execute("""SELECT i.inbox_id FROM qq_official_inbound_events i
                LEFT JOIN durable_jobs j ON j.job_id=i.ingest_job_id
                WHERE i.account_key=? AND i.status='pending' AND
                  (j.job_id IS NULL OR j.status NOT IN ('pending','running')
                   OR (j.status='pending' AND j.attempts>=j.max_attempts)) LIMIT 1""",
                (token.account_key,)).fetchone()
            if broken is not None:
                raise IngressError("Official pending inbox requires queue recovery")

    @staticmethod
    def _retire_evidence(cursor, account_key, now, *, endpoint_key=None):
        clause = "account_key=?" + (" AND endpoint_key=?" if endpoint_key is not None else "")
        params = (account_key, endpoint_key) if endpoint_key is not None else (account_key,)
        cursor.execute(f"UPDATE qq_official_reply_windows SET state='revoked',updated_at=? WHERE {clause}", (now, *params))
        cursor.execute(f"""UPDATE qq_official_owner_actor_links SET state='revoked',revision=revision+1,updated_at=?
            WHERE {clause} AND state='active'""", (now, *params))
        cursor.execute(f"""UPDATE qq_official_binding_challenges SET state='cancelled',code_hmac='',updated_at=?,consumed_at=?
            WHERE challenge_id IN (SELECT challenge_id FROM qq_official_binding_candidates WHERE {clause} AND state='pending')""",
            (now, now, *params))
        cursor.execute(f"""UPDATE qq_official_binding_candidates SET state='cancelled',updated_at=?,consumed_at=?
            WHERE {clause} AND state='pending'""", (now, now, *params))
