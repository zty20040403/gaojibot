"""Durable multipart stages inside the existing artifact job, never a queue.

Each stage is committed before network I/O. Only exact confirmed stages can be
skipped on recovery; an unresolved physical call stops the artifact. Session
and final file_info are independently fsynced before their DB confirmations.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
import secrets

from ...platform_types import EndpointKey
from ...agent.control import LeaseLost
from ...workers.durable_jobs import JobDeferred
from .image_formats import file_type_for_mime
from .native_media import (CONTRACT, PreparedUpload, UploadedMedia, build_prepare,
    build_part_finish, build_complete, fingerprint)
from .native_upload import OfficialMediaUploader, UploadIntent, UploadGuardError, StageRequest, UploadSourceLost, issue_upload_permit
from .native_vault import NativeUploadBinding
from .native_session_vault import NativeSessionBinding, NativeSessionReceipt


class _Halt(Exception):
    def __init__(self,reason,*,unknown=False):
        self.reason,self.unknown=reason,unknown


def _receipt(stage):
    return hashlib.sha256(json.dumps(['qq-upload-stage-200-v1',stage.operation,
        stage.request_commitment,stage.part_index],separators=(',',':')).encode()).hexdigest()


class MultipartDispatcher:
    def __init__(self,parent,configuration,vault,transport):
        self.parent,self.configuration,self.vault,self.transport=parent,configuration,vault,transport
        self.limits=replace(parent.limits,max_parts=configuration.max_parts)

    async def __call__(self,job,grant,artifact):
        try:
            return await self._run(job,grant,artifact)
        except UploadSourceLost as error:
            # Persist only historical local handoff evidence. This cannot grant
            # another attempt, and the original control interruption remains.
            try:
                if error.result.stage_attempt_id is not None:
                    state='unsent' if error.result.disposition=='proven_unsent' else 'unknown'
                    await self.parent._db(self.parent._settle_stage,error.result.stage_attempt_id,
                        error.stage,state,None,error.result)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Failed historical persistence remains unresolved; it cannot
                # authorize a retry. Do not mask a new shutdown cancellation.
                pass
            raise LeaseLost('file_source_unavailable') from None
        except _Halt as error:
            return await self.parent._stop(job,artifact['artifact_id'],
                'upload_unknown' if error.unknown else 'failed',error.reason)

    def _uploader(self,job,grant,intent,required):
        async def current(value,operation):
            if value!=intent:raise UploadGuardError()
            await self.parent._db(self.parent._current,grant,'native')
        async def begin(stage):
            if stage.intent!=intent:raise UploadGuardError()
            attempt=await self.parent._db(self._begin,grant,job,stage,required.get((stage.operation,stage.part_index),()))
            return issue_upload_permit(stage,stage_attempt_id=attempt,check_current=self.parent._local)
        return OfficialMediaUploader(self.parent.rest,ensure_current=current,before_network=begin,
            clock=self.parent._now,limits=self.limits,allowed_part_hosts=self.configuration.allowed_part_hosts,
            part_transport=self.transport)

    def _rows(self,cursor,stage):
        rows=cursor.execute('''SELECT s.* FROM qq_official_file_stage_attempts s
            JOIN qq_official_file_artifacts a ON a.artifact_id=s.artifact_id
            WHERE s.artifact_id=? AND a.account_key=? AND s.operation=?
            AND s.part_index IS NOT DISTINCT FROM ? ORDER BY s.created_at,s.stage_attempt_id''',
            (stage.intent.artifact_id,self.parent.token.account_key,stage.operation,stage.part_index)).fetchall()
        remaining=[]
        for row in rows:
            if (row['request_commitment']!=stage.request_commitment or row['expires_at']!=stage.intent.deadline_at):
                raise UploadGuardError()
            if not self.parent._fresh_unsent(row):remaining.append(row)
        if len(remaining)>1 or not remaining and len(rows)>=32:raise UploadGuardError()
        return dict(remaining[0]) if remaining else None

    def _existing(self,cursor,grant,stage):
        self.parent._current(cursor,grant,'native')
        return self._rows(cursor,stage)

    def _begin(self,cursor,grant,job,stage,required):
        artifact,delivery=self.parent._current(cursor,grant,'native')
        if (stage.operation not in {'upload_prepare','upload_put','upload_part_finish','upload_complete'}
                or stage.intent.artifact_id!=artifact['artifact_id'] or stage.intent.artifact.file_type!=4
                or delivery['active_plan_id'] is not None or delivery['credential_ref'] is not None
                or self._rows(cursor,stage) is not None):
            raise UploadGuardError()
        self.parent._job(cursor,job,artifact['artifact_id'],self.parent._now())
        if stage.operation!='upload_prepare':
            session=cursor.execute('SELECT * FROM qq_official_file_upload_sessions WHERE artifact_id=?',
                (artifact['artifact_id'],)).fetchone()
            if (session is None or session['expires_at']!=stage.intent.deadline_at
                    or session['expires_at']<=self.parent._now() or session['contract']!=CONTRACT):
                raise UploadGuardError()
        for prior in required:
            row=self._rows(cursor,prior)
            if row is None or row['state']!='confirmed':raise UploadGuardError()
            if prior.operation!='upload_prepare' and row['receipt_commitment']!=_receipt(prior):
                raise UploadGuardError()
        attempt,now=secrets.token_hex(16),self.parent._now()
        if now>=stage.intent.deadline_at:raise UploadGuardError()
        cursor.execute('''INSERT INTO qq_official_file_stage_attempts
            (stage_attempt_id,artifact_id,operation,part_index,request_commitment,job_id,job_lease_generation,
             account_epoch,state,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,'dispatched',?,?)''',
            (attempt,artifact['artifact_id'],stage.operation,stage.part_index,stage.request_commitment,
             job.job_id,job.lease_generation,self.parent.token.epoch,now,stage.intent.deadline_at))
        cursor.execute("UPDATE qq_official_file_deliveries SET state='uploading',route='native',updated_at=? WHERE artifact_id=?",
            (now,artifact['artifact_id']))
        return attempt

    async def _failed(self,job,grant,stage,result):
        if result.stage_attempt_id is None:
            # An unreturned commit can still have created the durable intent.
            row=await self.parent._db(self._rows,stage)
            if row is not None:raise _Halt('upload_intent_unknown',unknown=True)
            if result.disposition=='proven_unsent' and result.category=='recipient_evidence_unavailable':
                raise JobDeferred('recipient_evidence_unavailable',15)
            raise _Halt('upload_stage_unavailable')
        state={'confirmed_rejected':'rejected','proven_unsent':'unsent'}.get(result.disposition,'unknown')
        await self.parent._db(self.parent._settle_stage,result.stage_attempt_id,stage,state,None,result)
        if state=='unsent' and result.category=='recipient_evidence_unavailable':
            raise JobDeferred('recipient_evidence_unavailable',15)
        raise _Halt('upload_stage_unknown' if state=='unknown' else 'upload_stage_rejected',unknown=state=='unknown')

    def _session_record(self,cursor,attempt,stage,binding,reference,receipt,result=None):
        # Historical evidence only; current authority is checked again before
        # any subsequent network operation or M3 handoff.
        self.parent._settle_stage(cursor,attempt,stage,'confirmed',reference.binding_digest,result)
        values=(stage.intent.artifact_id,attempt,reference.credential_ref,reference.binding_digest,
            receipt.received_at,reference.expires_at,len(receipt.prepared.parts),CONTRACT)
        cursor.execute('''INSERT INTO qq_official_file_upload_sessions
            (artifact_id,prepare_stage_attempt_id,credential_ref,binding_digest,prepared_at,expires_at,part_count,contract)
            VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(artifact_id) DO NOTHING''',values)
        row=cursor.execute('SELECT * FROM qq_official_file_upload_sessions WHERE artifact_id=?',
            (stage.intent.artifact_id,)).fetchone()
        if row is None or tuple(row[key] for key in ('artifact_id','prepare_stage_attempt_id','credential_ref',
                'binding_digest','prepared_at','expires_at','part_count','contract'))!=values:
            raise UploadGuardError()

    async def _prepare(self,job,grant,artifact,intent,fp):
        uploader=self._uploader(job,grant,intent,{})
        request=build_prepare(intent.endpoint,fp,limits=self.limits)
        stage=uploader._stage(intent,request.stage,[request.path,request.body])
        row=await self.parent._db(self._existing,grant,stage)
        if row is not None:
            if row['state'] not in {'dispatched','confirmed','unknown'}:
                raise _Halt('upload_prepare_previous_failure')
            binding=NativeSessionBinding(NativeUploadBinding(artifact['artifact_id'],row['stage_attempt_id'],
                intent.endpoint,fp,artifact['mime']),intent.deadline_at,self.configuration)
            try:reference,receipt=await self.parent.calls(self.vault.recover_stage,binding)
            except Exception:raise _Halt('upload_session_evidence_unavailable',unknown=True) from None
            await self.parent._db(self._session_record,row['stage_attempt_id'],stage,binding,reference,receipt)
            return stage,receipt
        result=await uploader.prepare(intent)
        if result.disposition!='confirmed' or not isinstance(result.value,PreparedUpload) or result.stage_attempt_id is None:
            await self._failed(job,grant,stage,result)
        binding=NativeSessionBinding(NativeUploadBinding(artifact['artifact_id'],result.stage_attempt_id,
            intent.endpoint,fp,artifact['mime']),intent.deadline_at,self.configuration)
        now=self.parent._now()
        expiry=min(intent.deadline_at,now+self.configuration.max_session_seconds,now+result.value.retry_timeout_seconds)
        receipt=NativeSessionReceipt(result.value,now,expiry)
        reference=await self.parent.calls(self.vault.save,binding,receipt)
        await self.parent._db(self._session_record,result.stage_attempt_id,stage,binding,reference,receipt,result)
        return stage,receipt

    async def _simple(self,job,grant,stage,invoke):
        row=await self.parent._db(self._existing,grant,stage)
        if row is not None:
            if (row['state']=='confirmed' and row['receipt_commitment']==_receipt(stage)
                    and row['http_status']==200):return
            raise _Halt('upload_stage_unresolved',unknown=row['state'] in {'dispatched','unknown','confirmed'})
        result=await invoke()
        if (result.disposition!='confirmed' or result.http_status!=200 or result.stage_attempt_id is None):
            await self._failed(job,grant,stage,result)
        await self.parent._db(self.parent._settle_stage,result.stage_attempt_id,stage,'confirmed',_receipt(stage),result)

    async def _complete(self,job,grant,artifact,intent,fp,uploader,prepared):
        request=build_complete(prepared)
        stage=uploader._stage(intent,request.stage,[request.path,request.body])
        row=await self.parent._db(self._existing,grant,stage)
        if row is not None:
            if row['state'] not in {'dispatched','confirmed','unknown'}:raise _Halt('upload_merge_previous_failure')
            binding=NativeUploadBinding(artifact['artifact_id'],row['stage_attempt_id'],intent.endpoint,fp,artifact['mime'])
            try:reference,_=await self.parent.calls(self.parent.payloads.native_vault.recover_stage,binding)
            except Exception:raise _Halt('upload_merge_evidence_unavailable',unknown=True) from None
            await self.parent._db(self.parent._settle_stage,row['stage_attempt_id'],stage,'confirmed',reference.binding_digest,None)
        else:
            result=await uploader.complete(intent,prepared)
            if result.disposition!='confirmed' or not isinstance(result.value,UploadedMedia) or result.stage_attempt_id is None:
                await self._failed(job,grant,stage,result)
            binding=NativeUploadBinding(artifact['artifact_id'],result.stage_attempt_id,intent.endpoint,fp,artifact['mime'])
            reference=await self.parent.calls(self.parent.payloads.native_vault.save,binding,result.value)
            await self.parent._db(self.parent._settle_stage,result.stage_attempt_id,stage,'confirmed',reference.binding_digest,result)
        return await self.parent._db(self.parent._handoff_native,grant,binding,reference)

    def _final_row(self,cursor,artifact_id):
        rows=cursor.execute('''SELECT s.* FROM qq_official_file_stage_attempts s
            JOIN qq_official_file_artifacts a ON a.artifact_id=s.artifact_id
            WHERE s.artifact_id=? AND a.account_key=? AND s.operation='upload_complete'
            ORDER BY s.created_at,s.stage_attempt_id''',(artifact_id,self.parent.token.account_key)).fetchall()
        physical=[dict(row) for row in rows if not self.parent._fresh_unsent(row)]
        if len(physical)>1:raise UploadGuardError()
        return physical[0] if physical else None

    async def _restore_final(self,grant,artifact,intent,fp):
        row=await self.parent._db(self._final_row,artifact['artifact_id'])
        if row is None:return None
        if row['state'] not in {'dispatched','confirmed','unknown'}:
            raise _Halt('upload_merge_previous_failure')
        binding=NativeUploadBinding(artifact['artifact_id'],row['stage_attempt_id'],intent.endpoint,fp,artifact['mime'])
        try:reference,_=await self.parent.calls(self.parent.payloads.native_vault.recover_stage,binding)
        except Exception:raise _Halt('upload_merge_evidence_unavailable',unknown=True) from None
        # Exact immutable DB intent + same-stage final private receipt suffice
        # for historical settlement. No expired prepare URL/session is needed.
        stage=StageRequest(replace(intent,deadline_at=row['expires_at']),'upload_complete',row['request_commitment'])
        await self.parent._db(self.parent._settle_stage,row['stage_attempt_id'],stage,'confirmed',reference.binding_digest,None)
        return await self.parent._db(self.parent._handoff_native,grant,binding,reference)

    async def _run(self,job,grant,artifact):
        await self.parent._db(self.parent._current,grant,'native')
        content=await self.parent.calls(self.parent.artifacts.read_verified,artifact['artifact_id'],
            sha256=artifact['sha256'],byte_size=artifact['byte_size'])
        fp=fingerprint([content],filename=artifact['filename'],file_type=file_type_for_mime(artifact['mime']),limits=self.limits)
        if fp.file_type!=4:raise UploadGuardError()
        intent=UploadIntent(artifact['artifact_id'],EndpointKey.parse(artifact['endpoint_key']),fp,
            min(artifact['pending_deadline_at'],artifact['retain_until']))
        completed=await self._restore_final(grant,artifact,intent,fp)
        if completed is not None:return completed
        prepare,receipt=await self._prepare(job,grant,artifact,intent,fp)
        intent=replace(intent,deadline_at=receipt.reusable_until)
        required={}
        uploader=self._uploader(job,grant,intent,required)
        finishes=[]
        for part in receipt.prepared.parts:
            block=content[part.offset:part.offset+part.length]
            put=uploader._stage(intent,'upload_put',[receipt.prepared.upload_id,part.presigned_url,
                part.offset,part.length,hashlib.sha256(block).hexdigest()],part.index)
            required[('upload_put',part.index)]=(prepare,)
            await self._simple(job,grant,put,lambda p=part,b=block:uploader.put_part(intent,receipt.prepared,p.index,b))
            request=build_part_finish(receipt.prepared,part.index,block)
            finish=uploader._stage(intent,request.stage,[request.path,request.body],part.index)
            required[('upload_part_finish',part.index)]=(put,)
            await self._simple(job,grant,finish,
                lambda p=part,b=block:uploader.finish_part(intent,receipt.prepared,p.index,b))
            finishes.append(finish)
        required[('upload_complete',None)]=tuple(finishes)
        return await self._complete(job,grant,artifact,intent,fp,uploader,receipt.prepared)
