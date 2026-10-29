"""An explicit bounded branch in the EXISTING Sub-Agent external journal.

No independent queue, model loop or automatic submission recovery. An unknown
first response has no safe operation ID and is terminal needs_attention. Resume
reads the stored operation only; a new normal tool step must request approval.
"""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import re

from src.bot_security.store import SecurityError, canonical
from src.plugins.ai_chat.agent.control import JobFence, active_job_fence
from src.plugins.ai_chat.platform_types import BackgroundToolSource
from .approval_transport import HostAction, TaskPolicy, task_effect_scope

_PATH = re.compile(r'/v1/operations/(op_[a-f0-9]{32})(/approve)?')

class OfficialExternalOperations:
    def __init__(self, *, store, authority, source_port, transaction_factory):
        if source_port.token != authority.transport.token:
            raise SecurityError('official_external_account_mismatch')
        self.store, self.authority, self.source_port = store, authority, source_port
        self.transaction_factory = transaction_factory
        self._source = ContextVar('official_external_source', default=None)

    @contextmanager
    def bind(self, task, job, *, route_epoch):
        background = BackgroundToolSource(task.endpoint, task.requester_actor, 'subagent',
                                         'task:'+str(task.task_id), self.store.control(task.task_id)['revision'])
        with self.source_port.bind_job(job):
            with self.transaction_factory() as cursor:
                source = self.source_port.source_for(cursor, background=background,
                    route_epoch=route_epoch, now=self.authority.now())
            def guard(cursor):
                self.source_port.require_current(cursor, source=source, policy=self._read_policy(),
                    operation='journal', now=self.authority.now())
            def check():
                with self.transaction_factory() as cursor:
                    guard(cursor)
                return True
            fence = active_job_fence.set(JobFence(job.job_id, job.lease_owner, job.attempts,
                check, job.lease_generation, guard))
            value = self._source.set(source)
            from src.plugins.ai_chat.agent.external import active_official_external
            official = active_official_external.set(self)
            try:
                with self.authority.bind(source):
                    self.guard(task.task_id)
                    yield source
            finally:
                active_official_external.reset(official)
                self._source.reset(value)
                active_job_fence.reset(fence)

    @staticmethod
    def _read_policy():
        # This policy describes a local check, not an approval scope.
        return TaskPolicy((HostAction('fleet.operation.read', 'local_source_check', hashlib.sha256(b'{}').hexdigest()),))

    def guard(self, task_id):
        source = self._source.get()
        if source is None or source.task_id != task_id:
            raise SecurityError('official_external_source_required')
        with self.transaction_factory() as cursor:
            self.source_port.require_current(cursor, source=source, policy=self._read_policy(),
                operation='read', now=self.authority.now())
            self.authority._identity(cursor, source)
        return source

    def _request(self, tracker, method, path, body, actor, origin):
        source = self.guard(tracker.task_id)
        match = _PATH.fullmatch(path)
        if (not tracker.call_id or not isinstance(tracker.call_id,str) or len(tracker.call_id.encode()) > 256
                or actor != 'official' or origin != source.scope.key
                or not ((method == 'POST' and path == '/v1/operations/prepare')
                    or (method == 'GET' and match and not match[2])
                    or (method == 'POST' and match and match[2]))):
            raise SecurityError('official_external_contract_unsupported')
        body = dict(body) if body is not None else None
        if path == '/v1/operations/prepare':
            if body is None:
                raise SecurityError('official_external_contract_unsupported')
            identity = canonical([tracker.task_id,tracker.revision,tracker.run_id,tracker.call_id])
            body['idempotency_key'] = 'subagent:' + hashlib.sha256(identity.encode()).hexdigest()
        return source, {'method':method,'path':path,'body':body,'actor':actor,'origin':origin,
            'tool_name':tracker.tool_name,'tool_arguments':tracker.arguments,'official_source':source.data()}

    async def request(self, tracker, method, path, body, *, actor, origin, perform):
        source, request = self._request(tracker,method,path,body,actor,origin)
        prior = next((c for c in self.store.external_calls(tracker.task_id,run_id=tracker.run_id)
                      if c['call_id'] == tracker.call_id), None)
        once = self.store.begin_external(tracker.task_id,tracker.revision,tracker.run_id,tracker.call_id,request,
            official_once_path=path if method=='POST' and path.endswith('/approve') else None)
        if once is not None:
            item = next(c for c in self.store.external_calls(tracker.task_id)
                        if (c['run_id'],c['call_id']) == once)
            prior = item
        else:
            item = next(c for c in self.store.external_calls(tracker.task_id,run_id=tracker.run_id) if c['call_id']==tracker.call_id)
        if prior is not None:
            evidence=self.validate_item(source,item)
            if evidence is not None and (item['status']=='resolved' or path.endswith('/approve')):
                _,context,contract=evidence
                if not self.granted(context,contract):
                    raise SecurityError('official_external_grant_unavailable')
            if item['status'] == 'resolved' or item['remote_path']:
                return item['response']
            # The prior process may have reached the network. Never retry even
            # prepare automatically merely because its response is unavailable.
            self._unknown(item)
            return {'ok':False,'status':'needs_attention','executed':None,'error':'official_external_receipt_unknown'}
        try:
            response = await perform(request['body'])
        except BaseException as exc:
            # Persist when still fenced; a lost fence must leave the durable
            # intent for its new owner to park, never grant stale settlement.
            self._unknown(item)
            if isinstance(exc,asyncio.CancelledError):
                raise
            if not isinstance(exc,Exception):
                raise
            return {'ok':False,'status':'needs_attention','executed':None,'error':'official_external_receipt_unknown'}
        self.guard(tracker.task_id)
        self._record(source,response)
        self.store.finish_external(item,response)
        return response

    def _unknown(self,item):
        self.guard(item['task_id'])
        self.store.finish_external(item,{'ok':False,'status':'needs_attention','executed':None,
                                        'error':'official_external_receipt_unknown'})

    def _record(self,source,response):
        if not isinstance(response,dict):
            raise SecurityError('official_external_receipt_invalid')
        record=response.get('operation')
        if not isinstance(record,dict):record=response
        with self.transaction_factory() as cursor:
            account,recipient=self.authority._identity(cursor,source)
        context,contract=self.authority.context_for(record,source,account,recipient)
        return record,context,contract

    def validate_item(self,source,item):
        request=item['request']
        if (item['task_id'] != source.task_id or item['revision'] != source.revision
                or request.get('official_source') != source.data()
                or request.get('actor') != 'official' or request.get('origin') != source.scope.key):
            raise SecurityError('official_external_source_changed')
        if item['remote_path']:
            record,context,contract=self._record(source,item['response'])
            if item['remote_path'] != '/v1/operations/'+record['operation_id']:
                raise SecurityError('official_external_receipt_changed')
            return record,context,contract
        if item['status']=='resolved' and item['response'].get('status') != 'needs_attention':
            raise SecurityError('official_external_receipt_invalid')
        return None

    def granted(self,context,contract):
        with self.transaction_factory() as cursor:
            account,_=self.authority._identity(cursor,context.source)
        request=self.authority.mobile.store.task_authorization(account,None,context.payload())
        if request is None or request['status'] != 'succeeded':
            return False
        with task_effect_scope(self.authority.mobile.store,context=context,
                operation='fleet.operation.approve',target_id=contract['operation_id'],arguments=contract):
            return True

    def hydrate(self,task_id):
        source=self.guard(task_id)
        for item in self.store.external_calls(task_id):
            evidence=self.validate_item(source,item)
            if evidence is not None and item['status']=='resolved':
                _,context,contract=evidence
                if not self.granted(context,contract):
                    raise SecurityError('official_external_grant_unavailable')

    async def poll(self,task,client):
        source=self.guard(task.task_id)
        for item in self.store.external_calls(task.task_id):
            if item['status']!='pending':
                continue
            self.guard(task.task_id)
            prior=self.validate_item(source,item)
            if prior is None:
                self._unknown(item)
                continue
            old,_,old_contract=prior
            # Bypass only the local ExternalCalls tracker; authorization is
            # still the SAME source-aware FleetAuthorization implementation.
            response=await client._authorized_request('GET',item['remote_path'],actor='official',origin=source.scope.key)
            current,context,contract=self._record(source,response)
            if current['operation_id'] != old['operation_id'] or contract != old_contract:
                raise SecurityError('official_external_contract_changed')
            if not self.granted(context,contract):
                continue
            resume = (current['status']=='awaiting_approval'
                      and item['request']['path']=='/v1/operations/prepare')
            # The exact prior prepare fact becomes consumable by the ORIGINAL
            # Coordinator checkpoint. It is not a remote execution receipt.
            result={'operation':current,'executed':current['status']=='succeeded',
                    'approval_required':False,'prepared':resume}
            self.store.finish_external(item,result,official_resume_ready=resume)
        return not any(c['status']=='pending' for c in self.store.external_calls(task.task_id))
