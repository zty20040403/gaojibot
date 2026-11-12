"""File-specific program outbox proof and revocable upload lifecycle."""
import asyncio
import json

from .program_actions import ProgramDenied
from .program_delivery import rendered_say
from .send_types import digest, stable_json
from .vm_sessions import source


def artifact(cursor, row):
    item = cursor.execute('SELECT * FROM qq_official_file_artifacts WHERE source_program_action_id=?',
        (row['action_id'],)).fetchone()
    if item is None or item['source_extension_kind']!='program':
        raise ProgramDenied('program_file_source_unavailable')
    return item


def rendered(cursor, row):
    item = artifact(cursor,row)
    return rendered_say(source(row).endpoint,'文件：'+item['filename'])


def matches(cursor, row, plan, *, binding=False):
    item = artifact(cursor,row)
    if not 0<plan['expires_at']<=row['expires_at']:
        return False
    manifest = json.loads(plan['manifest_json'])
    if binding and 'rendered' in manifest:
        return manifest==dict(v=1,rendered=rendered(cursor,row).to_json(),control=None,source_native_message_id='')
    delivery = cursor.execute('SELECT * FROM qq_official_file_deliveries WHERE artifact_id=?',(item['artifact_id'],)).fetchone()
    return (delivery is not None and delivery['active_plan_id']==plan['plan_id']
        and set(manifest)=={'v','media','artifact_id','source_native_message_id'}
        and manifest['v']==1 and manifest['source_native_message_id']=='' and manifest['artifact_id']==item['artifact_id']
        and isinstance(manifest['media'],dict) and set(manifest['media'])=={'file_info'}
        and isinstance(manifest['media']['file_info'],str) and bool(manifest['media']['file_info'])
        and plan['manifest_digest']==digest(stable_json(manifest)))


def check(service, artifact_id):
    from .program_file_source import assert_artifact
    with service.transaction() as cursor:
        service._account(cursor)
        item = cursor.execute('SELECT * FROM qq_official_file_artifacts WHERE artifact_id=?',(artifact_id,)).fetchone()
        if item is None:
            raise ProgramDenied('program_file_source_unavailable')
        assert_artifact(cursor,token=service.token,artifact=item,admission=service.admission,now=int(service.clock()))


async def uploading(service, item, content):
    async def current():
        await service.calls(check,service,item['artifact_id'])
    await current()
    pending = asyncio.create_task(service.uploader.upload(source_endpoint(item),content,item['filename'],current=current))
    try:
        while not pending.done():
            await current()
            await asyncio.wait((pending,),timeout=0.25)
        return await pending
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending,return_exceptions=True)


def source_endpoint(item):
    from ...platform_types import EndpointKey
    return EndpointKey.parse(item['endpoint_key'])


def failed(service, artifact_id):
    with service.transaction() as cursor:
        service._account(cursor)
        item = cursor.execute('SELECT source_program_action_id FROM qq_official_file_artifacts WHERE artifact_id=? AND account_key=?',
            (artifact_id,service.token.account_key)).fetchone()
        if item is None or item['source_program_action_id'] is None:
            return False
        # Do not overwrite actual delivery outcomes, including accepted/unknown.
        cursor.execute("""UPDATE qq_official_program_actions SET state='failed',error_code='program_file_upload_failed'
            WHERE action_id=? AND plan_id IS NULL""",(item['source_program_action_id'],))
        cursor.execute("""UPDATE qq_official_file_deliveries SET state='failed',reason_code='upload_failed',updated_at=?
            WHERE artifact_id=? AND active_plan_id IS NULL""",(int(service.clock()),artifact_id))
        return True
