"""Fixed host-only control protocol; callers never supply URLs, headers or argv."""
import asyncio
from contextlib import suppress
from dataclasses import dataclass
import hashlib
import hmac
import json
import re
import time

import httpx

from .ops_plans import fingerprint

MAX_RESPONSE = 1024*1024
DEADLINE = 20
BACKENDS = {'ssh':('ssh.execute','ssh-management-v1'),'ops':('maxops.execute','ops-management-v2')}
STATES = {'awaiting_approval','queued','running','cancelling','succeeded','failed','cancelled','needs_attention'}


class OpsTransportError(RuntimeError):
    """Fixed diagnostics only; an interrupted approval may have taken effect."""


@dataclass(frozen=True)
class Reference:
    operation_id: str
    contract_hash: str
    resource_version: int

    def __post_init__(self):
        if (type(self.operation_id) is not str or not re.fullmatch(r'op_[a-f0-9]{32}',self.operation_id)
                or type(self.contract_hash) is not str or not re.fullmatch(r'[a-f0-9]{64}',self.contract_hash)
                or type(self.resource_version) is not int or self.resource_version<1):
            raise OpsTransportError('ops_remote_identity_invalid')


def invalid_constant(_):
    raise ValueError('invalid_json_number')


class OpsClient:
    def __init__(self,config,plan,parameters,*,action_id,deadline_at,current,clock=time.time):
        if type(action_id) is not str or not re.fullmatch(r'[a-f0-9]{32}',action_id):
            raise ValueError('invalid_ops_action_id')
        self.config,self.plan,self.params = config,plan,plan.render(parameters)
        self.secret = config.secret()
        self.binding = config.binding(plan,self.secret)
        self.origin = self.key = 'ops-f6:'+action_id
        if type(deadline_at) is not int or not 0<deadline_at<2**63:
            raise ValueError('invalid_ops_deadline')
        self.current,self.clock,self.deadline_at = current,clock,deadline_at
        self.backend = None

    async def authorized(self):
        await self.current()
        if self.clock()>=self.deadline_at:raise OpsTransportError('ops_source_deadline_expired')

    async def _request(self,method,path,payload=None,*,params=None):
        await self.authorized()
        raw = b'' if payload is None else json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
        url = httpx.URL(self.config.base_url+path)
        timestamp = str(int(self.clock()))
        signed = '\n'.join((method,url.path,self.config.actor,self.origin,timestamp,hashlib.sha256(raw).hexdigest())).encode()
        headers = {'Authorization':'Bearer '+self.secret,'X-KC-Actor':self.config.actor,'X-KC-Origin':self.origin,
            'X-KC-Time':timestamp,'X-KC-Signature':hmac.new(self.secret.encode('ascii'),signed,hashlib.sha256).hexdigest(),
            'Content-Type':'application/json','Accept':'application/json','Accept-Encoding':'identity'}
        async def request():
            await self.authorized()
            async with httpx.AsyncClient(timeout=DEADLINE,trust_env=False,follow_redirects=False) as client:
                async with client.stream(method,url,headers=headers,content=raw,params=params) as response:
                    if not 200<=response.status_code<300:
                        raise OpsTransportError('ops_control_http_rejected')
                    if response.headers.get('content-encoding','identity').lower()!='identity':
                        raise OpsTransportError('ops_control_encoding_invalid')
                    body = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(chunk)>MAX_RESPONSE-len(body):
                            raise OpsTransportError('ops_control_response_too_large')
                        body.extend(chunk)
                    try:
                        value = json.loads(body,parse_constant=invalid_constant)
                        if type(value) is not dict:raise ValueError
                        json.dumps(value,ensure_ascii=False,allow_nan=False).encode('utf-8')
                        return value
                    except (ValueError,RecursionError):
                        raise OpsTransportError('ops_control_response_invalid') from None
        pending = asyncio.create_task(request())
        try:
            async with asyncio.timeout(DEADLINE):
                while not pending.done():
                    await asyncio.wait({pending},timeout=0.25)
                    await self.authorized()
                value = await pending
                await self.authorized()
                return value
        except (TimeoutError,httpx.HTTPError):
            raise OpsTransportError('ops_control_request_interrupted') from None
        finally:
            pending.cancel()
            with suppress(BaseException):await pending

    async def catalog(self):
        value = await self._request('GET','/v1/ops/catalog',params={'operation':'exec.run'})
        definitions = value.get('operations')
        if (type(value.get('version')) is not int or value['version']!=2 or value.get('writes_require_approval') is not True
                or type(value.get('backend')) is not str or value['backend'] not in BACKENDS or type(value.get('hosts')) is not list
                or self.plan.host not in value['hosts'] or type(definitions) is not list or len(definitions)!=1):
            raise OpsTransportError('ops_control_catalog_incompatible')
        item = definitions[0]
        if (type(item) is not dict or item.get('name')!='exec.run' or item.get('read_only') is not False
                or item.get('kind')!='job_submission' or item.get('idempotency')!='required'):
            raise OpsTransportError('ops_control_catalog_incompatible')
        self.backend = value['backend']

    def validate(self,value,reference=None):
        if type(value) is not dict:raise OpsTransportError('ops_remote_record_invalid')
        args = value.get('arguments')
        if (type(args) is not dict or set(args)-{'op','params','binding_hash','upstream_idempotency','host_control'}
                or args.get('op')!='exec.run' or fingerprint(args.get('params'))!=fingerprint(self.params) or args.get('upstream_idempotency')!='required'
                or type(args.get('binding_hash')) is not str or not re.fullmatch(r'[a-f0-9]{64}',args['binding_hash'])
                or value.get('actor_id')!=self.config.actor or value.get('origin_scope')!=self.origin
                or value.get('idempotency_key')!=self.key or value.get('host_id')!=self.plan.host
                or type(value.get('deadline_at')) is not int or not 0<value['deadline_at']<=self.deadline_at
                or value.get('resource_ref')!='exec.run' or type(value.get('status')) is not str or value['status'] not in STATES
                or (value.get('operation'),value.get('backend_ref')) not in BACKENDS.values()):
            raise OpsTransportError('ops_remote_intent_mismatch')
        if self.backend is not None and (value['operation'],value['backend_ref'])!=BACKENDS[self.backend]:
            raise OpsTransportError('ops_remote_backend_changed')
        expected = fingerprint(dict(actor=self.config.actor,origin=self.origin,arguments=args))
        ref = Reference(value.get('operation_id'),value.get('contract_hash'),value.get('resource_version'))
        if ref.contract_hash!=expected or reference is not None and ref!=reference:
            raise OpsTransportError('ops_remote_contract_changed')
        return ref

    async def prepare(self):
        await self.catalog()
        response = await self._request('POST','/v1/ops/call',{'operation':'exec.run','params':self.params,'idempotency_key':self.key,'deadline_at':self.deadline_at})
        value = response.get('operation')
        ref = self.validate(value)
        if (response.get('ok') is not True or response.get('executed') is not False
                or response.get('approval_required') is not True or value['status']!='awaiting_approval'):
            raise OpsTransportError('ops_remote_not_awaiting_approval')
        return ref

    async def approve(self,ref):
        if type(ref) is not Reference:raise OpsTransportError('ops_remote_identity_invalid')
        value = await self._request('POST','/v1/operations/'+ref.operation_id+'/approve',
            {'contract_hash':ref.contract_hash,'resource_version':ref.resource_version})
        self.validate(value,ref)
        if value['status']=='awaiting_approval':raise OpsTransportError('ops_remote_approval_unconfirmed')
        return value

    async def status(self,ref):
        if type(ref) is not Reference:raise OpsTransportError('ops_remote_identity_invalid')
        value = await self._request('GET','/v1/operations/'+ref.operation_id)
        self.validate(value,ref)
        return value
