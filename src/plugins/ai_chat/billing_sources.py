"""Immutable display/attribution metadata, read from verified host source rows."""
import json


def metadata(cursor, source):
    result = {'person': None, 'display': None, 'programs': [], 'commands': []}
    actor = source.actor
    if actor is not None:
        row = cursor.execute(
            '''SELECT principal_id,display_name FROM principal_identities
            WHERE platform=? AND account_key=? AND identity_kind=? AND identity_namespace=? AND native_user_id=?''',
            (actor.account.platform, actor.account.key, actor.identity_kind, actor.identity_namespace, actor.native_user_id),
        ).fetchone()
        if row is not None:
            result.update(person=row['principal_id'], display=(row['display_name'] or '')[:200])
    return json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
