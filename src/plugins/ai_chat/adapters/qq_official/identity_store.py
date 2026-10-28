"""Fenced bot identity persistence, for authenticated internal runtime calls only.

Lease possession is NOT proof that a payload came from QQ. Runtime must call
observe_ready only with the full READY accepted on its authenticated account
session, and observe_self_api only after M0 validates that same account's
GET /users/@me HTTP/body success. Never expose these entry points to chat/tools.

All database work joins the caller cursor; no transaction, commit or network
operation is opened here. snapshot is read-only and does not grant permission:
the sender calls it inside its already-fenced reservation transaction. Existing
historical attempts retain their captured author evidence independently.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
from typing import Literal

from src.bot_storage.database import StoreCursor
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey

from . import identity
from .lease import LeaseToken, assert_epoch

_CONTRACT_VERSION = 1
_SOURCE_PATHS = {"ready": "botpy/types/gateway.py", "self_api": "botpy/api.py"}
_FIELDS = """account_key,bot_native_id,bot_identity_source,bot_identity_verified_at,
    bot_identity_revision,bot_identity_contract"""


class IdentityStoreError(RuntimeError):
    """Fixed safe diagnostics; never carry payload, identity or DB parameters."""


@dataclass(frozen=True)
class VerifiedAuthor:
    bot_actor: ActorKey = field(repr=False)
    source: str
    revision: int


@dataclass(frozen=True)
class IdentityUpdate:
    status: Literal["unknown", "verified", "mismatch"]
    revision: int
    changed: bool
    reason: str


def _account(value: AccountKey) -> AccountKey:
    try:
        if not isinstance(value, AccountKey) or value.platform != "qq-official" or len(value.key.encode("utf-8")) > 512:
            raise ValueError
        return value
    except Exception:
        raise IdentityStoreError("Official bot identity account is invalid") from None


def _time(value: int) -> None:
    if type(value) is not int or not 0 <= value <= identity.MAX_OBSERVATION_TIME:
        raise IdentityStoreError("Official bot identity observation time is invalid")


def _execute(cursor: StoreCursor, query: str, parameters=()):
    try:
        return cursor.execute(query, parameters).fetchone()
    except Exception:
        raise IdentityStoreError("Official bot identity storage operation failed") from None


def _contract(evidence: tuple[identity.IdentityEvidence, ...], conflict=None) -> str:
    # Only fixed contract/source identifiers and bounded first-observation times.
    # Native ID lives in its dedicated field; no payload/name/requester is stored.
    data = {"v": _CONTRACT_VERSION, "c": identity.OFFICIAL_SDK_COMMIT,
            "e": [[item.source, item.first_observed_at] for item in sorted(evidence, key=lambda item: item.source)]}
    if conflict is not None:
        data["x"] = [conflict.source, conflict.first_observed_at]
    encoded = json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 256:
        raise IdentityStoreError("Official bot identity contract exceeds storage bounds")
    return encoded


def _source(evidence: tuple[identity.IdentityEvidence, ...]) -> str:
    return "ready_and_self_api" if len(evidence) == 2 else evidence[0].source


def _restore(account: AccountKey, row: Mapping) -> tuple[identity.IdentityState, int]:
    """Validate a trusted DB row structurally; types alone are not provenance."""
    try:
        if row["account_key"] != account.key:
            raise ValueError
        native_id, source = row["bot_native_id"], row["bot_identity_source"]
        verified_at, revision = row["bot_identity_verified_at"], row["bot_identity_revision"]
        encoded = row["bot_identity_contract"]
        if type(revision) is not int or not 0 <= revision <= identity.MAX_OBSERVATION_TIME:
            raise ValueError
        if native_id is None:
            if source is not None or verified_at is not None or encoded is not None or revision != 0:
                raise ValueError
            return identity.IdentityState(account), 0
        if revision == 0 or source not in {"ready", "self_api", "ready_and_self_api", "mismatch"}:
            raise ValueError
        _time(verified_at)
        if not isinstance(encoded, str) or not 1 <= len(encoded.encode("utf-8")) <= 256:
            raise ValueError
        data = json.loads(encoded)
        expected = {"v", "c", "e", "x"} if source == "mismatch" else {"v", "c", "e"}
        if (not isinstance(data, dict) or set(data) != expected or type(data["v"]) is not int
                or data["v"] != _CONTRACT_VERSION or data["c"] != identity.OFFICIAL_SDK_COMMIT
                or not isinstance(data["e"], list) or not 1 <= len(data["e"]) <= 2):
            raise ValueError

        def evidence(entry):
            if not isinstance(entry, list) or len(entry) != 2 or entry[0] not in _SOURCE_PATHS:
                raise ValueError
            observed_source, observed_at = entry
            _time(observed_at)
            url = f"https://github.com/tencent-connect/botpy/blob/{identity.OFFICIAL_SDK_COMMIT}/{_SOURCE_PATHS[observed_source]}"
            return identity.IdentityEvidence(observed_source, identity.OFFICIAL_SDK_COMMIT, url, observed_at, observed_at)

        observations = tuple(evidence(entry) for entry in data["e"])
        restored = identity.VerifiedBotIdentity(native_id, observations)
        if verified_at not in {item.first_observed_at for item in observations}:
            raise ValueError
        conflict = evidence(data["x"]) if source == "mismatch" else None
        if _contract(observations, conflict) != encoded:
            raise ValueError
        if source != "mismatch" and _source(observations) != source:
            raise ValueError
        state = identity.IdentityState(account, "mismatch" if source == "mismatch" else "verified", restored)
        return state, revision
    except Exception:
        # Includes unsupported legacy rows lacking a verified contract. Do not
        # silently turn an arbitrary old bot_native_id into a proven author.
        raise IdentityStoreError("Official bot identity stored evidence is invalid") from None


def snapshot(cursor: StoreCursor, account: AccountKey) -> VerifiedAuthor | None:
    """Read the current author; no write/fence/permission is implied by this API."""
    account = _account(account)
    row = _execute(cursor, f"SELECT {_FIELDS} FROM qq_official_accounts WHERE account_key=?", (account.key,))
    if row is None:
        return None
    state, revision = _restore(account, row)
    if state.bot_actor is None:
        return None
    return VerifiedAuthor(state.bot_actor, row["bot_identity_source"], revision)


def _observe(cursor: StoreCursor, token: LeaseToken, payload: object, now: int, *, source: str) -> IdentityUpdate:
    # Must precede payload access or any identity write, including duplicate calls.
    assert_epoch(cursor, token)
    _time(now)
    try:
        account = _account(AccountKey.parse(token.account_key))
    except Exception:
        raise IdentityStoreError("Official bot identity account is invalid") from None
    row = _execute(cursor, f"SELECT {_FIELDS} FROM qq_official_accounts WHERE account_key=?", (account.key,))
    if row is None:
        raise IdentityStoreError("Official bot identity account is unavailable")
    state, revision = _restore(account, row)
    if state.status == "mismatch":
        return IdentityUpdate("mismatch", revision, False, "identity_conflict_requires_review")
    context = identity.TrustedTransportObservation(account, True, now, source == "self_api")
    try:
        observe = identity.observe_ready if source == "ready" else identity.observe_self_api
        result = observe(state, context, payload)
    except Exception:
        raise IdentityStoreError("Official bot identity observation is invalid") from None
    if not result.accepted and result.state.status != "mismatch":
        return IdentityUpdate(state.status, revision, False, result.reason)
    if state.identity is None:
        native_id = result.state.identity.native_id
        observations = result.state.identity.evidence
        next_source, verified_at, next_revision = _source(observations), now, revision + 1
        encoded = _contract(observations)
        reason = "identity_verified"
    elif result.state.status == "mismatch":
        native_id = state.identity.native_id
        observations = state.identity.evidence
        next_source, verified_at = "mismatch", row["bot_identity_verified_at"]
        # At BIGINT exhaustion, sticky blocking still wins over overflow that
        # would roll back the conflict and leave a sendable verified identity.
        next_revision = min(identity.MAX_OBSERVATION_TIME, revision + 1)
        encoded = _contract(observations, result.conflicting_evidence)
        reason = "identity_conflict"
    else:
        # Duplicate source/native observations do not refresh first verification
        # or last-seen timestamps. Replayed frames are fully idempotent.
        if source in {item.source for item in state.identity.evidence}:
            return IdentityUpdate("verified", revision, False, "identity_unchanged")
        native_id = state.identity.native_id
        observations = result.state.identity.evidence
        next_source, verified_at, next_revision = _source(observations), row["bot_identity_verified_at"], revision
        encoded = _contract(observations)
        reason = "identity_source_confirmed"
    # Catch loss while reading/validating evidence as well as before entry.
    assert_epoch(cursor, token)
    changed = _execute(cursor, """UPDATE qq_official_accounts SET
        bot_native_id=?,bot_identity_source=?,bot_identity_verified_at=?,
        bot_identity_revision=?,bot_identity_contract=?,updated_at=GREATEST(updated_at,?)
        WHERE account_key=? AND bot_identity_revision=? RETURNING bot_identity_revision""",
        (native_id, next_source, verified_at, next_revision, encoded, now, account.key, revision))
    if changed is None:
        raise IdentityStoreError("Official bot identity update could not be applied")
    return IdentityUpdate(result.state.status, next_revision, True, reason)


def observe_ready(cursor: StoreCursor, token: LeaseToken, payload: object, now: int) -> IdentityUpdate:
    """Persist a full authenticated READY frame; runtime owns transport provenance."""
    return _observe(cursor, token, payload, now, source="ready")


def observe_self_api(cursor: StoreCursor, token: LeaseToken, payload: object, now: int) -> IdentityUpdate:
    """Persist successful authenticated GET /users/@me data; never perform that GET."""
    return _observe(cursor, token, payload, now, source="self_api")
