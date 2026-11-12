"""Bounded VM export to the ordinary immutable file queue; no host paths."""
import asyncio
import hashlib
import json
from pathlib import PurePosixPath

from ...application.owned_sandbox import workspace_path, safe_filename
from ...background_tools import workspace_owner
from .image_formats import artifact_filename
from .lease import assert_epoch
from .program_actions import ProgramDenied
from .program_file_source import issue, load

EXPORT_SECONDS = 60


class ProgramFiles:
    def __init__(self, lifecycle, runtime, logger):
        self.actions,self.manager,self.calls = lifecycle.actions,lifecycle.manager,lifecycle.calls
        self.runtime,self.logger,self.active = runtime,logger,{}

    def recover(self):
        with self.actions.transaction() as cursor:
            assert_epoch(cursor,self.actions.sessions.lease)
            cursor.execute('''UPDATE qq_official_program_actions a SET state='ambiguous',error_code='program_file_export_interrupted'
                FROM qq_official_endpoints e WHERE e.endpoint_key=a.endpoint_key AND e.account_key=?
                AND a.account_epoch<>? AND a.kind='send_file' AND a.state='pending'
                AND NOT EXISTS(SELECT 1 FROM qq_official_file_artifacts f WHERE f.source_program_action_id=a.action_id)''',
                (self.actions.sessions.lease.account_key,self.actions.sessions.lease.epoch))

    def fail(self, action_id, code):
        with self.actions.transaction() as cursor:
            assert_epoch(cursor,self.actions.sessions.lease)
            cursor.execute('''UPDATE qq_official_program_actions a SET state='failed',error_code=?
                WHERE action_id=? AND kind='send_file' AND state='pending'
                AND NOT EXISTS(SELECT 1 FROM qq_official_file_artifacts f WHERE f.source_program_action_id=a.action_id)''',
                (code,action_id))

    def source(self, action_id):
        with self.actions.transaction() as cursor:
            return issue(cursor,self.actions,action_id)

    def check(self, action_id):
        with self.actions.transaction() as cursor:
            load(cursor,self.actions,action_id)

    async def export(self, current, row, cap):
        await self.calls(self.check,row['action_id'])
        pending = asyncio.create_task(self.manager.export_artifact(workspace_owner(current.endpoint),current.sandbox_id,
            json.loads(row['payload_json'])['path'],file_only=True,max_bytes=cap))
        try:
            while not pending.done():
                await self.calls(self.check,row['action_id'])
                await asyncio.wait((pending,),timeout=0.25)
            content,archive = await pending
            if archive is not False or type(content) is not bytes or not 0<len(content)<=cap:
                raise ValueError('invalid_bounded_export')
            return content
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending,return_exceptions=True)

    async def submit(self, current, *, generation, path, request_id, program='manual', filename=None):
        path = workspace_path(path)
        filename = artifact_filename(safe_filename(filename if filename is not None else PurePosixPath(path).name),draft=False)
        service = getattr(self.runtime,'file_service',None)
        cap = getattr(self.manager,'max_file_bytes',None)
        if service is None or type(cap) is not int or cap<=0:
            raise ProgramDenied('program_files_unavailable',503)
        cap = min(cap,service.config.max_file_bytes,100*1024*1024)
        key = current.endpoint.key
        if self.active.get(key,0)>=2 or sum(self.active.values())>=4:
            raise ProgramDenied('program_file_exports_busy',429)
        self.active[key] = self.active.get(key,0)+1
        result = None
        code = 'program_file_export_interrupted'
        try:
            def reserve():
                nonlocal result
                # Retain the committed action if DatabaseCalls is cancelled
                # while draining its write, just as for wake claims.
                result = self.actions.reserve(current,generation=generation,kind='send_file',program=program,
                    request_id=request_id,payload={'path':path,'filename':filename})
            await self.calls(reserve)
            row,created = result
            if created:
                async with asyncio.timeout(EXPORT_SECONDS):
                    content = await self.export(current,row,cap)
                binding = await self.calls(self.source,row['action_id'])
                await self.calls(service.artifacts.register,binding,content=content,filename=filename,
                    logical_request_key=hashlib.sha256(('program-file:'+row['action_id']).encode()).hexdigest())
            return await self.calls(self.actions.get,current,row['action_id'])
        except ProgramDenied as error:
            code = str(error)
            raise
        except PermissionError:
            code = 'vm_source_revoked'
            raise
        except ValueError:
            if result is not None and result[1]:
                code = 'program_file_export_failed'
                raise ProgramDenied(code,502) from None
            raise
        except Exception as error:
            code = 'program_file_export_failed'
            self.logger.warning('Program file export failed error=%s',type(error).__name__)
            raise ProgramDenied(code,502) from None
        finally:
            self.active[key] -= 1
            if not self.active[key]:
                self.active.pop(key)
            if result is not None and result[1]:
                try:
                    await self.calls(self.fail,result[0]['action_id'],code)
                except Exception as error:
                    self.logger.warning('Program file export settlement failed error=%s',type(error).__name__)
