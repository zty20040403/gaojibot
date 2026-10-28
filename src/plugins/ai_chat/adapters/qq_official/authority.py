"""M2 owner grants. All mutations use the caller's transaction.

No transport SDK, automatic first-contact enrollment, or personal identity merge.
The caller must commit safe denials as well as successful decisions.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey


@dataclass(frozen=True)
class ConsoleAuthorization:
    account_id: str = field(repr=False)
    account_version: int
    session_hash: str = field(repr=False)


@dataclass(frozen=True)
class OwnerGrant:
    grant_id: str
    account_key: str = field(repr=False)
    c2c_actor_key: str = field(repr=False)
    revision: int
    state: str


@dataclass(frozen=True)
class AuthorityDecision:
    status: str
    grant: OwnerGrant | None = None


def _native_id(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value):
        return False
    try:
        return len(value.encode("utf-8")) <= 256
    except UnicodeEncodeError:
        return False


def audit(cursor, *, account: AccountKey, action: str, actor: str,
          target: str, outcome: str, now: int) -> None:
    """Existing security audit; no raw text, code, session, or error payload."""
    detail = json.dumps({"account_ref": hashlib.sha256(account.key.encode()).hexdigest(),
                         "actor_ref": hashlib.sha256(actor.encode()).hexdigest(),
                         "outcome": outcome}, separators=(",", ":"))
    cursor.execute("""INSERT INTO admin_security_audit
        (audit_id,account_id,action,target,detail,created_at) VALUES (?,?,?,?,?,?)""",
        (secrets.token_hex(16), "", "qq.binding." + action, target, detail, now))


def _grant(row) -> OwnerGrant | None:
    if row is None:
        return None
    return OwnerGrant(row["grant_id"], row["account_key"], row["c2c_actor_key"],
                      int(row["revision"]), row["state"])


class OwnerAuthority:
    def __init__(self, *, clock: Callable[[], float] = time.time):
        self.clock = clock

    def lock_account(self, cursor, account: AccountKey) -> bool:
        return cursor.execute("SELECT account_key FROM qq_official_accounts WHERE account_key=? FOR UPDATE",
                              (account.key,)).fetchone() is not None

    def authenticate_console(self, cursor, *, session_token: str,
                             csrf_token: str) -> ConsoleAuthorization | None:
        if not session_token or not csrf_token or len(session_token) > 256 or len(csrf_token) > 256:
            return None
        session_hash = hashlib.sha256(session_token.encode()).hexdigest()
        row = cursor.execute("""SELECT a.account_id,a.version,s.csrf_hash FROM admin_sessions s
            JOIN admin_accounts a ON a.account_id=s.account_id
            WHERE s.session_hash=? AND s.expires_at>? AND a.enabled=1
              AND a.role='admin' AND a.version=s.account_version FOR SHARE OF a,s""",
            (session_hash, int(self.clock()))).fetchone()
        if row is None or not hmac.compare_digest(row["csrf_hash"], hashlib.sha256(csrf_token.encode()).hexdigest()):
            return None
        return ConsoleAuthorization(row["account_id"], int(row["version"]), session_hash)

    def console_current(self, cursor, authorization: ConsoleAuthorization) -> bool:
        if not isinstance(authorization, ConsoleAuthorization):
            return False
        return cursor.execute("""SELECT 1 FROM admin_sessions s JOIN admin_accounts a
            ON a.account_id=s.account_id WHERE s.session_hash=? AND a.account_id=?
            AND a.version=? AND a.version=s.account_version AND a.enabled=1 AND a.role='admin'
            AND s.expires_at>? FOR SHARE OF a,s""",
            (authorization.session_hash, authorization.account_id, authorization.account_version,
             int(self.clock()))).fetchone() is not None

    def c2c_grant(self, cursor, actor: ActorKey, *, lock: bool = False) -> OwnerGrant | None:
        if actor.account.platform != "qq-official" or actor.identity_kind != "c2c_user":
            return None
        row = cursor.execute("""SELECT * FROM qq_official_owner_grants
            WHERE account_key=? AND c2c_actor_key=? AND state='active'""" + (" FOR UPDATE" if lock else ""),
            (actor.account.key, actor.key)).fetchone()
        return _grant(row)

    def owner_for_actor(self, cursor, endpoint: EndpointKey, actor: ActorKey) -> OwnerGrant | None:
        if not actor.matches_endpoint(endpoint) or endpoint.account.platform != "qq-official":
            return None
        if endpoint.kind == "private":
            return self.c2c_grant(cursor, actor)
        row = cursor.execute("""SELECT g.* FROM qq_official_owner_actor_links l
            JOIN qq_official_owner_grants g ON g.grant_id=l.owner_grant_id
            JOIN qq_official_endpoints e ON e.endpoint_key=l.endpoint_key
            WHERE l.endpoint_key=? AND l.actor_key=? AND l.state='active'
              AND g.account_key=? AND g.state='active' AND g.revision=l.owner_revision
              AND e.account_key=g.account_key AND e.generation=l.endpoint_generation
              AND e.binding_state='active' AND e.membership_state='present'""",
            (endpoint.key, actor.key, endpoint.account.key)).fetchone()
        return _grant(row)

    def apply_trusted_config(self, cursor, account: AccountKey, owner_c2c_ids: tuple[str, ...], source_revision: str) -> tuple[AuthorityDecision, ...]:
        """Provision absent config owners; removed entries revoke config grants.

        A revoked owner is never silently restored by rereading a config file.
        Regrant requires an explicit, versioned authenticated-console operation.
        Only the trusted local runtime factory calls this method. The factory
        verifies protected configuration provenance before opening a transaction.
        """
        if (account.platform != "qq-official" or not isinstance(owner_c2c_ids, tuple)
                or len(owner_c2c_ids) > 32 or not all(_native_id(x) for x in owner_c2c_ids)
                or len(set(owner_c2c_ids)) != len(owner_c2c_ids)
                or not isinstance(source_revision, str) or not source_revision or len(source_revision) > 256
                or not self.lock_account(cursor, account)):
            return (AuthorityDecision("unavailable"),)
        now = int(self.clock())
        actors = tuple(ActorKey(account, "c2c_user", "", value) for value in owner_c2c_ids)
        if len(account.key.encode()) > 512 or any(len(actor.key.encode()) > 1280 for actor in actors):
            return (AuthorityDecision("unavailable"),)
        desired = {actor.key for actor in actors}
        rows = cursor.execute("""SELECT * FROM qq_official_owner_grants
            WHERE account_key=? AND source='config' ORDER BY grant_id FOR UPDATE""",
            (account.key,)).fetchall()
        for row in rows:
            if row["c2c_actor_key"] not in desired and row["state"] == "active":
                self._revoke(cursor, row, actor="trusted-config", now=now)
        decisions = []
        for actor in actors:
            row = cursor.execute("SELECT * FROM qq_official_owner_grants WHERE account_key=? AND c2c_actor_key=? FOR UPDATE",
                                 (account.key, actor.key)).fetchone()
            if row is not None:
                decisions.append(AuthorityDecision("already_active" if row["state"] == "active" else "revoked", _grant(row)))
                continue
            grant = self._insert(cursor, actor, source="config", source_ref=source_revision, now=now)
            audit(cursor, account=account, action="owner.created", actor="trusted-config",
                  target=grant.grant_id, outcome="active", now=now)
            decisions.append(AuthorityDecision("created", grant))
        return tuple(decisions)

    def set_console_owner(self, cursor, *, authorization: ConsoleAuthorization,
                          actor: ActorKey, active: bool, expected_revision: int) -> AuthorityDecision:
        if (actor.account.platform != "qq-official" or actor.identity_kind != "c2c_user"
                or type(active) is not bool or type(expected_revision) is not int
                or expected_revision < 0 or not _native_id(actor.native_user_id)
                or len(actor.account.key.encode()) > 512 or len(actor.key.encode()) > 1280
                or not self.lock_account(cursor, actor.account)
                or not self.console_current(cursor, authorization)):
            return AuthorityDecision("denied")
        row = cursor.execute("SELECT * FROM qq_official_owner_grants WHERE account_key=? AND c2c_actor_key=? FOR UPDATE",
                             (actor.account.key, actor.key)).fetchone()
        if (row is None and expected_revision != 0) or (row is not None and row["revision"] != expected_revision):
            return AuthorityDecision("conflict")
        now = int(self.clock())
        control_actor = "admin-account:" + authorization.account_id
        if row is None:
            if not active:
                return AuthorityDecision("not_found")
            grant = self._insert(cursor, actor, source="console", source_ref=control_actor, now=now)
            audit(cursor, account=actor.account, action="owner.created", actor=control_actor,
                  target=grant.grant_id, outcome="active", now=now)
            return AuthorityDecision("created", grant)
        if not active:
            if row["state"] == "active":
                self._revoke(cursor, row, actor=control_actor, now=now)
            return AuthorityDecision("revoked", _grant(cursor.execute("SELECT * FROM qq_official_owner_grants WHERE grant_id=?",
                                                                      (row["grant_id"],)).fetchone()))
        if row["state"] == "active":
            return AuthorityDecision("already_active", _grant(row))
        cursor.execute("""UPDATE qq_official_owner_grants SET state='active',revision=revision+1,
            source='console',source_ref=?,updated_at=?,revoked_at=NULL WHERE grant_id=?""",
            (control_actor, now, row["grant_id"]))
        audit(cursor, account=actor.account, action="owner.regranted", actor=control_actor,
              target=row["grant_id"], outcome="active", now=now)
        return AuthorityDecision("regranted", _grant(cursor.execute("SELECT * FROM qq_official_owner_grants WHERE grant_id=?",
                                                                    (row["grant_id"],)).fetchone()))

    def _insert(self, cursor, actor: ActorKey, *, source: str, source_ref: str, now: int) -> OwnerGrant:
        grant_id = "OG-" + secrets.token_hex(16)
        cursor.execute("""INSERT INTO qq_official_owner_grants
            (grant_id,account_key,c2c_actor_key,source,source_ref,revision,state,created_at,updated_at)
            VALUES (?,?,?,?,?,1,'active',?,?)""",
            (grant_id, actor.account.key, actor.key, source, source_ref, now, now))
        return OwnerGrant(grant_id, actor.account.key, actor.key, 1, "active")

    def _revoke(self, cursor, row, *, actor: str, now: int) -> None:
        cursor.execute("""UPDATE qq_official_owner_grants SET state='revoked',revision=revision+1,
            revoked_at=?,updated_at=? WHERE grant_id=?""", (now, now, row["grant_id"]))
        cursor.execute("""UPDATE qq_official_owner_actor_links SET state='revoked',revision=revision+1,
            updated_at=? WHERE owner_grant_id=? AND state='active'""", (now, row["grant_id"]))
        cursor.execute("""UPDATE qq_official_binding_candidates SET state='cancelled',version=version+1,
            updated_at=? WHERE state='pending' AND challenge_id IN
            (SELECT challenge_id FROM qq_official_binding_challenges WHERE owner_grant_id=?)""",
            (now, row["grant_id"]))
        cursor.execute("""UPDATE qq_official_binding_challenges SET state='cancelled',code_hmac='',
            updated_at=? WHERE owner_grant_id=? AND state IN ('sending','issued','candidate')""",
            (now, row["grant_id"]))
        audit(cursor, account=AccountKey.parse(row["account_key"]), action="owner.revoked",
              actor=actor, target=row["grant_id"], outcome="revoked", now=now)
