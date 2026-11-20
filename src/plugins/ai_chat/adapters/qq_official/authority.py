"""Configured QQ owner authority and retained binding-link validation."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Callable

from src.bot_security.store import write_security_audit
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey


@dataclass(frozen=True)
class ConfiguredOwner:
    account_key: str = field(repr=False)
    c2c_actor_key: str = field(repr=False)


def audit(cursor, *, account: AccountKey, action: str, actor: str,
          target: str, outcome: str, now: int) -> None:
    """Existing security audit; no raw text, code, session, or error payload."""
    detail = json.dumps({"account_ref": hashlib.sha256(account.key.encode()).hexdigest(),
                         "actor_ref": hashlib.sha256(actor.encode()).hexdigest(),
                         "outcome": outcome}, separators=(",", ":"))
    write_security_audit(cursor, "", "qq.binding." + action, target, detail, now)


class OwnerAuthority:
    def __init__(self, *, account: AccountKey, owner_c2c_ids: tuple[str, ...] = (),
                 clock: Callable[[], float] = time.time):
        self.clock = clock
        self.account = account
        self.owner_actor_keys = frozenset(ActorKey(account, "c2c_user", "", value).key for value in owner_c2c_ids)

    def configured_owner(self, actor: ActorKey) -> ConfiguredOwner | None:
        return ConfiguredOwner(actor.account.key, actor.key) if actor.key in self.owner_actor_keys else None

    def owner_for_actor(self, cursor, endpoint: EndpointKey,
                        actor: ActorKey) -> ConfiguredOwner | None:
        if self.account != endpoint.account or not actor.matches_endpoint(endpoint):
            return None
        if endpoint.account.platform != 'qq-official':
            return None
        if endpoint.kind == 'private':
            return self.configured_owner(actor)
        row = cursor.execute("""SELECT l.configured_owner_actor_key FROM qq_official_owner_actor_links l
            JOIN qq_official_endpoints e ON e.endpoint_key=l.endpoint_key AND e.account_key=l.account_key
            WHERE l.account_key=? AND l.endpoint_key=? AND l.actor_key=? AND l.state='active'
              AND l.endpoint_generation=e.generation AND e.binding_state='active' AND e.membership_state='present'""",
            (self.account.key, endpoint.key, actor.key)).fetchone()
        if row and row['configured_owner_actor_key'] in self.owner_actor_keys:
            return ConfiguredOwner(self.account.key, row['configured_owner_actor_key'])
        return None

    def apply_trusted_config(self, cursor) -> None:
        cursor.execute("""UPDATE qq_official_owner_actor_links SET state='revoked',revision=revision+1,
            updated_at=? WHERE account_key=? AND state='active' AND configured_owner_actor_key<>ALL(?)""",
            (int(self.clock()), self.account.key, list(self.owner_actor_keys)))
        cursor.execute("""UPDATE qq_official_accounts SET owner_link_actor_key=NULL,
            owner_link_code_hmac=NULL,owner_link_expires_at=NULL,owner_link_epoch=NULL
            WHERE account_key=?""", (self.account.key,))
