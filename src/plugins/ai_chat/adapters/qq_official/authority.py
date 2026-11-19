"""Configured QQ owner authority and retained binding-link validation."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable

from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey


@dataclass(frozen=True)
class ConsoleAuthorization:
    account_id: str = field(repr=False)
    account_version: int
    session_hash: str = field(repr=False)


@dataclass(frozen=True)
class ConfiguredOwner:
    account_key: str = field(repr=False)
    c2c_actor_key: str = field(repr=False)


@dataclass(frozen=True)
class OwnerGrant(ConfiguredOwner):
    grant_id: str
    revision: int
    state: str


@dataclass(frozen=True)
class AuthorityDecision:
    status: str
    grant: OwnerGrant | None = None


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
    return OwnerGrant(account_key=row["account_key"], c2c_actor_key=row["c2c_actor_key"],
                      grant_id=row["grant_id"], revision=int(row["revision"]), state=row["state"])


class OwnerAuthority:
    def __init__(self, *, account: AccountKey, owner_c2c_ids: tuple[str, ...] = (),
                 clock: Callable[[], float] = time.time):
        self.clock = clock
        self.account = account
        self.owner_c2c_ids = owner_c2c_ids
        self.actors = tuple(ActorKey(account, "c2c_user", "", value) for value in owner_c2c_ids)
        self.owner_actor_keys = frozenset(actor.key for actor in self.actors)

    def configured_owner(self, actor: ActorKey) -> ConfiguredOwner | None:
        return ConfiguredOwner(actor.account.key, actor.key) if actor.key in self.owner_actor_keys else None

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
        if self.configured_owner(actor) is None:
            return None
        row = cursor.execute("""SELECT * FROM qq_official_owner_grants
            WHERE account_key=? AND c2c_actor_key=? AND state='active'""" + (" FOR UPDATE" if lock else ""),
            (actor.account.key, actor.key)).fetchone()
        return _grant(row)

    def owner_for_actor(self, cursor, endpoint: EndpointKey,
                        actor: ActorKey) -> ConfiguredOwner | None:
        if (self.account != endpoint.account or not actor.matches_endpoint(endpoint)
                or endpoint.account.platform != "qq-official"):
            return None
        if endpoint.kind == "private":
            return self.configured_owner(actor)
        row = cursor.execute("""SELECT g.* FROM qq_official_owner_actor_links l
            JOIN qq_official_owner_grants g ON g.grant_id=l.owner_grant_id
            JOIN qq_official_endpoints e ON e.endpoint_key=l.endpoint_key
            WHERE l.endpoint_key=? AND l.actor_key=? AND l.state='active'
              AND g.account_key=? AND g.state='active' AND g.revision=l.owner_revision AND e.account_key=g.account_key
              AND e.generation=l.endpoint_generation AND e.binding_state='active'
              AND e.membership_state='present'""",
            (endpoint.key, actor.key, endpoint.account.key)).fetchone()
        if row is None or row["c2c_actor_key"] not in self.owner_actor_keys:
            return None
        return _grant(row)

    def apply_trusted_config(self, cursor, account: AccountKey, owner_c2c_ids: tuple[str, ...], source_revision: str) -> tuple[AuthorityDecision, ...]:
        """Maintain legacy foreign-key rows without using them as owner authority."""
        if (account != self.account or owner_c2c_ids != self.owner_c2c_ids
                or not isinstance(source_revision, str)
                or not source_revision or len(source_revision) > 256
                or not self.lock_account(cursor, account)):
            return (AuthorityDecision("unavailable"),)
        now = int(self.clock())
        rows = cursor.execute("""SELECT * FROM qq_official_owner_grants
            WHERE account_key=? AND source='config' ORDER BY grant_id FOR UPDATE""",
            (account.key,)).fetchall()
        for row in rows:
            if row["c2c_actor_key"] not in self.owner_actor_keys and row["state"] == "active":
                self._revoke(cursor, row, actor="trusted-config", now=now)
        cursor.execute("""UPDATE qq_official_owner_actor_links l SET state='revoked',revision=l.revision+1,
            updated_at=? FROM qq_official_owner_grants g WHERE g.grant_id=l.owner_grant_id
            AND l.account_key=? AND l.state='active' AND g.c2c_actor_key<>ALL(?)""",
            (now, account.key, list(self.owner_actor_keys)))
        decisions = []
        for actor in self.actors:
            row = cursor.execute("SELECT * FROM qq_official_owner_grants WHERE account_key=? AND c2c_actor_key=? FOR UPDATE",
                                 (account.key, actor.key)).fetchone()
            if row is not None:
                decisions.append(AuthorityDecision("already_active" if row["state"] == "active" else "revoked", _grant(row)))
                continue
            grant = self._insert(cursor, actor, source_revision, now)
            audit(cursor, account=account, action="owner.created", actor="trusted-config",
                  target=grant.grant_id, outcome="active", now=now)
            decisions.append(AuthorityDecision("created", grant))
        return tuple(decisions)

    def _insert(self, cursor, actor: ActorKey, source_revision: str, now: int) -> OwnerGrant:
        grant_id = "OG-" + secrets.token_hex(16)
        cursor.execute("""INSERT INTO qq_official_owner_grants
            (grant_id,account_key,c2c_actor_key,source,source_ref,revision,state,created_at,updated_at)
            VALUES (?,?,?,'config',?,1,'active',?,?)""",
            (grant_id, actor.account.key, actor.key, source_revision, now, now))
        return OwnerGrant(account_key=actor.account.key, c2c_actor_key=actor.key,
                          grant_id=grant_id, revision=1, state="active")

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
