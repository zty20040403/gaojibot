"""Record the bot identity returned by the authenticated READY or self API."""
from dataclasses import dataclass, field
from ...platform_types import AccountKey, ActorKey
from .lease import assert_epoch


class IdentityStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerifiedAuthor:
    bot_actor: ActorKey = field(repr=False)
    source: str
    revision: int


@dataclass(frozen=True)
class IdentityUpdate:
    status: str
    revision: int
    changed: bool
    reason: str


def snapshot(cursor, account):
    row = cursor.execute('SELECT bot_native_id,bot_identity_source,bot_identity_revision FROM qq_official_accounts WHERE account_key=?',
        (account.key,)).fetchone()
    if row is None or not row['bot_native_id']:
        return None
    return VerifiedAuthor(ActorKey(account, 'bot', '', row['bot_native_id']),
        row['bot_identity_source'], row['bot_identity_revision'])


def _observe(cursor, token, user, now, source):
    assert_epoch(cursor, token)
    account = AccountKey.parse(token.account_key)
    current = snapshot(cursor, account)
    revision = current.revision if current else 0
    if not isinstance(user, dict) or not isinstance(user.get('id'), str) or not user['id'] or user.get('bot') is False:
        return IdentityUpdate('verified' if current else 'unknown', revision, False, 'identity_missing')
    changed = current is None or current.bot_actor.native_user_id != user['id']
    revision += int(changed)
    cursor.execute('''UPDATE qq_official_accounts SET bot_native_id=?,bot_identity_source=?,
        bot_identity_verified_at=?,bot_identity_revision=?,bot_identity_contract='{}',updated_at=? WHERE account_key=?''',
        (user['id'], source, now, revision, now, account.key))
    return IdentityUpdate('verified', revision, changed, 'identity_updated' if changed else 'identity_unchanged')


def observe_ready(cursor, token, payload, now):
    data = payload.get('d', {}) if isinstance(payload, dict) else {}
    return _observe(cursor, token, data.get('user'), now, 'ready')


def observe_self_api(cursor, token, payload, now):
    return _observe(cursor, token, payload, now, 'self_api')
