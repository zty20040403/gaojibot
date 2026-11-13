"""Immutable display/attribution metadata, read from verified host source rows."""
import json


def metadata(cursor,source):
    result = {'person':None,'display':None,'programs':[],'commands':[]}
    actor = source.actor
    if actor is not None:
        row = cursor.execute('''SELECT principal_id,display_name FROM principal_identities
            WHERE platform=? AND account_key=? AND identity_kind=? AND identity_namespace=? AND native_user_id=?''',
            (actor.account.platform,actor.account.key,actor.identity_kind,actor.identity_namespace,actor.native_user_id)).fetchone()
        if row is not None:
            result.update(person=row['principal_id'],display=(row['display_name'] or '')[:200])
    if source.program_wake_id is not None:
        # input_ids_json was fixed when the host claimed the wake. A later
        # reply_action, unused coalesced input or same-named new unit is not its
        # origin. Commands' completion wakes are not resident-program spending.
        rows = cursor.execute('''SELECT a.program_name,a.source_command_id FROM qq_official_program_wakes w
            JOIN LATERAL jsonb_array_elements_text(w.input_ids_json::jsonb) AS ids(action_id) ON TRUE
            JOIN qq_official_program_actions a ON a.action_id=ids.action_id AND a.wake_id=w.wake_id
            WHERE w.wake_id=? AND w.endpoint_key=? AND a.endpoint_key=w.endpoint_key AND a.scope_key=? AND a.kind='wake' ''',
            (source.program_wake_id,source.endpoint.key,source.endpoint.scope.key)).fetchall()
        result['programs'] = sorted({row['program_name'] for row in rows if row['source_command_id'] is None})
        result['commands'] = sorted({row['source_command_id'] for row in rows if row['source_command_id'] is not None})
    return json.dumps(result,ensure_ascii=False,sort_keys=True,separators=(',',':'))
