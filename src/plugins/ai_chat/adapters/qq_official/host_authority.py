"""Bounded official host authority over the existing Mobile/Fleet state machines.

Default denied. Internal host composition must bind the actual persisted source;
model-supplied ActorKeys, task objectives, URLs or operation labels confer no
rights. This first contract supports exact service operations only, not arbitrary
Ops catalogs, deployments, host shells, worker jobs or file routes.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass,field
import hashlib
import json
import secrets
import re
import time

from src.bot_security.service import assert_approved
from src.bot_security.store import SecurityError,canonical,digest
from src.cluster_control.execution_contracts import OperationProposal,content_hash,OPERATION_ACTIONS
from .approval_transport import ApprovalContext,FrozenSource,HostAction,Recipient,TaskPolicy,task_effect_scope

_OPERATION=re.compile(r'/v1/operations/(op_[a-f0-9]{32})(/approve)?')
_CONTRACT=('task_ref','step_ref','actor_id','origin_scope','host_id','resource_ref','operation','operation_version',
 'arguments','backend_ref','backend_binding_version','expected_state','resource_version','policy_version','deadline_at',
 'resource_budget','idempotency_key','verification','compensation')

@dataclass(frozen=True)
class AllowedService:
 host_id:str
 resource_ref:str
 backend_ref:str
 backend_binding_version:int=1
 actions:frozenset[str]=OPERATION_ACTIONS

 def __post_init__(self):
  if (not isinstance(self.host_id,str) or not isinstance(self.resource_ref,str)
    or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',self.host_id) is None
    or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@:-]{0,119}\.service',self.resource_ref) is None
    or not isinstance(self.backend_ref,str) or not 1<=len(self.backend_ref)<=120
    or type(self.backend_binding_version)is not int or self.backend_binding_version<1
    or type(self.actions)is not frozenset or not self.actions or not self.actions<=OPERATION_ACTIONS):
   raise SecurityError('official_host_configuration_invalid')

@dataclass(frozen=True)
class OfficialHostScope:
 source:FrozenSource=field(repr=False)
 authority:object=field(repr=False)
 # This is a routing value, not a bearer capability. Every use checks real DB.


class OfficialHostAuthority:
 def __init__(self,*,transport,mobile,services=(),clock=time.time):
  if mobile.store.transport is not transport or any(not isinstance(item,AllowedService) for item in services):
   raise SecurityError('official_host_configuration_invalid')
  self.transport,self.mobile,self.clock=transport,mobile,clock
  self.services={(item.host_id,item.resource_ref):item for item in services}
  if len(self.services)!=len(services):raise SecurityError('official_host_configuration_invalid')
  self.tasks=None

 def now(self):return self.transport.now()

 @contextmanager
 def bind(self,source):
  from ...server_task_authorization import server_task
  if not isinstance(source,FrozenSource) or source.endpoint.account.key!=self.transport.token.account_key:
   raise SecurityError('official_host_source_required')
  value=server_task.set(OfficialHostScope(source,self))
  try:yield
  finally:server_task.reset(value)

 def _scope(self):
  from ...server_task_authorization import server_task
  scope=server_task.get()
  if not isinstance(scope,OfficialHostScope) or scope.authority is not self:
   raise SecurityError('official_host_source_required')
  return scope.source

 def _identity(self,cursor,source):
  admin=self.transport.admins.resolve_admin(cursor,endpoint=source.endpoint,actor=source.actor)
  recipient=Recipient(admin.c2c_endpoint,admin.c2c_actor)
  return self.transport.account_for_recipient(cursor,recipient),recipient

 @staticmethod
 def origin(source):
  return 'qq-official-source:'+hashlib.sha256(canonical(source.data()).encode()).hexdigest()

 def _preparation(self,body,source):
  if not isinstance(body,dict) or set(body)-{'host_id','resource_ref','operation','arguments','expected_state','verification',
       'compensation','deadline_at','idempotency_key','task_ref','step_ref'}:
   raise SecurityError('official_host_contract_unsupported')
  if type(body.get('deadline_at'))is not int or not self.now()<body['deadline_at']<=source.deadline_at:
   raise SecurityError('official_host_contract_expired')
  try:proposal=OperationProposal.parse(body,now=self.now())
  except (ValueError,TypeError,KeyError):raise SecurityError('official_host_contract_invalid') from None
  allowed=self.services.get((proposal.host_id,proposal.resource_ref))
  if (allowed is None or proposal.operation not in allowed.actions
      or any((proposal.arguments,proposal.expected_state,proposal.verification,proposal.compensation))
      or proposal.task_ref or proposal.step_ref):
   raise SecurityError('official_host_contract_unsupported')
  return proposal

 @contextmanager
 def _base_scope(self,source,policy,operation):
  with self.mobile.store.transaction() as cursor:
   if source.endpoint.account.key!=self.transport.token.account_key:raise SecurityError('official_host_source_required')
   account,recipient=self._identity(cursor,source)
   self.transport.source_port.require_current(cursor,source=source,policy=policy,operation=operation,now=self.now())
   yield cursor,account,recipient

 async def _identity_snapshot(self,source,policy,operation):
  """Commit a short current-source check before any external I/O."""
  def checked():
   with self._base_scope(source,policy,operation) as (_,account,recipient):
    return dict(account),recipient
  return await asyncio.to_thread(checked)

 def _claim_effect(self,context,contract):
  # This is an attempt attached to the EXISTING successful task approval,
  # not another approval queue. Never remove a claim, including unknown I/O.
  with task_effect_scope(self.mobile.store,context=context,
      operation='fleet.operation.approve',target_id=contract['operation_id'],arguments=contract) as cursor:
   row=cursor.execute("""SELECT * FROM admin_approvals WHERE identity_transport='qq_official'
     AND official_account_key=? AND kind='server_task' AND payload_hash=? AND status='succeeded'
     ORDER BY created_at DESC LIMIT 1 FOR UPDATE""",
     (self.transport.token.account_key,digest(canonical(context.payload())))).fetchone()
   if row is None:raise SecurityError('official_host_approval_unavailable')
   result=json.loads(row['result_json']);effects=result.setdefault('official_effects',{})
   if type(effects)is not dict or len(effects)>32:raise SecurityError('official_host_attempt_invalid')
   key=contract['operation_id'];binding=digest(canonical([context.payload(),contract]))
   if key in effects:
    if effects[key].get('binding')!=binding:raise SecurityError('official_host_attempt_changed')
    return None
   claim={'binding':binding,'nonce':secrets.token_hex(16),'state':'admitted','admitted_at':self.now()}
   effects[key]=claim
   cursor.execute('UPDATE admin_approvals SET result_json=?,updated_at=? WHERE approval_id=?',
     (canonical(result),self.now(),row['approval_id']))
   return row['approval_id'],key,claim

 def _settle_effect(self,claim,*,remote_status=None):
  """Append a factual result to an exact admitted attempt, even after revocation.

  This deliberately does not enter SecurityStore's CURRENT-authority transaction.
  It cannot admit an effect, change approval status, or extend any source lease.
  """
  approval_id,key,expected=claim
  connection=self.mobile.store._database.store_connection();cursor=connection.cursor()
  try:
   row=cursor.execute("""SELECT result_json FROM admin_approvals WHERE approval_id=?
     AND identity_transport='qq_official' AND official_account_key=? FOR UPDATE""",
     (approval_id,self.transport.token.account_key)).fetchone()
   if row is None:raise SecurityError('official_host_attempt_unavailable')
   result=json.loads(row['result_json']);item=result.get('official_effects',{}).get(key)
   if not isinstance(item,dict) or any(item.get(k)!=expected[k] for k in ('binding','nonce','admitted_at')):
    raise SecurityError('official_host_attempt_changed')
   if item['state']=='admitted' or item['state']=='unknown' and remote_status is not None:
    item.update(state='observed' if remote_status is not None else 'unknown',observed_at=self.now())
    if remote_status is not None:item['remote_status']=remote_status
    cursor.execute('UPDATE admin_approvals SET result_json=? WHERE approval_id=?',(canonical(result),approval_id))
   connection.commit()
  except BaseException:
   connection.rollback();raise
  finally:
   cursor.close();connection.close()

 def contract(self,record,source,account):
  if (not isinstance(record,dict) or any(key not in record for key in
        (*_CONTRACT,'operation_id','contract_hash','capability_status','status'))
      or any(not isinstance(record[key],str) for key in ('operation_id','contract_hash','host_id','resource_ref',
           'operation','backend_ref','actor_id','origin_scope','idempotency_key','task_ref','step_ref'))
      or re.fullmatch(r'op_[a-f0-9]{32}',record['operation_id']) is None
      or re.fullmatch(r'[a-f0-9]{64}',record['contract_hash']) is None
      or type(record['backend_binding_version']) is not int
      or any(type(record[key]) is not dict or record[key] for key in ('arguments','expected_state','verification','compensation'))
      or type(record['resource_budget']) is not dict or set(record['resource_budget'])!={'wall_seconds'}
      or type(record['resource_budget']['wall_seconds']) is not int
      or not 1<=record['resource_budget']['wall_seconds']<=300
      or not 8<=len(record['idempotency_key'])<=160
      or any(ch.isspace() or ord(ch)<32 for ch in record['idempotency_key'])):
   raise SecurityError('official_host_contract_unsupported')
  actual={key:record[key] for key in _CONTRACT}
  allowed=self.services.get((record['host_id'],record['resource_ref']))
  if (allowed is None or record['operation'] not in allowed.actions
      or record['backend_ref']!=allowed.backend_ref or record['backend_binding_version']!=allowed.backend_binding_version
      or type(record['resource_version'])is not int or record['resource_version']<1
      or type(record['operation_version'])is not int or record['operation_version']!=1
      or type(record['policy_version'])is not int or record['policy_version']!=1
      or record['actor_id']!='admin:'+account['username'] or record['origin_scope']!=self.origin(source)
      or record['capability_status']!='available' or type(record['deadline_at'])is not int
      or not self.now()<record['deadline_at']<=source.deadline_at
      or any(record[key] for key in ('arguments','expected_state','verification','compensation','task_ref','step_ref'))
      or record['contract_hash']!=content_hash(actual) or len(canonical(actual).encode())>16384):
   raise SecurityError('official_host_contract_changed')
  return {'operation_id':record['operation_id'],'contract_hash':record['contract_hash'],**actual}

 def context_for(self,record,source,account,recipient):
  contract=self.contract(record,source,account)
  policy=TaskPolicy((HostAction('fleet.operation.approve',record['operation_id'],digest(canonical(contract)),canonical(contract)),))
  return ApprovalContext(recipient,source,policy),contract

 async def request(self,client,method,path,body,*,actor,origin):
  source=self._scope()
  # Strings supplied by model/tool callers never choose the principal or route.
  if actor or origin:
   if actor!='official' or origin!=source.scope.key:raise SecurityError('official_host_principal_mismatch')
  if method=='POST' and path=='/v1/operations/prepare':
   self._preparation(body,source)
   # A caller-supplied fresh UUID cannot turn the same source/parameters into
   # another remotely prepared operation. Background also has external_calls.
   body={**body,'idempotency_key':'official:'+digest(canonical([source.data(),
      {k:v for k,v in body.items() if k!='idempotency_key'}]))}
   policy=TaskPolicy((HostAction('fleet.operation.prepare',body['host_id'],digest(canonical(body))),))
   account,recipient=await self._identity_snapshot(source,policy,'prepare')
   record=await client._raw_request(method,path,body,actor='admin:'+account['username'],origin=self.origin(source))
   context,contract=self.context_for(record,source,account,recipient)
   ready=await self.tasks.ensure(context,account,wait=False) if self.tasks is not None else False
   if not ready:return {'operation':record,'approval_required':True,'executed':False}
   return await self._approve(client,record,context,account,contract)
  match=_OPERATION.fullmatch(path)
  if match is None or method not in {'GET','POST'} or (method=='GET' and match[2]) or (method=='POST' and not match[2]):
   raise SecurityError('official_host_contract_unsupported')
  policy=TaskPolicy((HostAction('fleet.operation.read',match[1],digest(canonical({}))),))
  account,recipient=await self._identity_snapshot(source,policy,'read')
  record=await client._raw_request('GET',path.removesuffix('/approve'),None,
     actor='admin:'+account['username'],origin=self.origin(source))
  context,contract=self.context_for(record,source,account,recipient)
  if method=='GET':return record
  if body!={'contract_hash':contract['contract_hash'],'resource_version':contract['resource_version']}:
   raise SecurityError('official_host_contract_changed')
  if self.tasks is None or not await self.tasks.ensure(context,account,wait=False):
   return {'operation':record,'approval_required':True,'executed':False}
  return await self._approve(client,record,context,account,contract)

 async def _approve(self,client,record,context,account,contract):
  path='/v1/operations/'+record['operation_id']
  # The preceding GET is evidence, not authorization. The short claim transaction
  # below repeats current source, grant, quorum and exact-contract checks.
  if record['status']!='awaiting_approval':
   return {'operation':record,'approval_required':False,'executed':record['status']=='succeeded',
           'submitted':record['status'] in {'queued','running','verifying'}}
  claim=await asyncio.to_thread(self._claim_effect,context,contract)
  if claim is None:
   return {'operation':record,'approval_required':False,'executed':None,
           'status':'needs_attention','error':'official_host_outcome_unknown'}
  try:
   if self.now()>=contract['deadline_at']:raise SecurityError('official_host_contract_expired')
   # No SQL, account, route, job or approval lock is held during this one RPC.
   result=await client._raw_request('POST',path+'/approve',
      {'contract_hash':contract['contract_hash'],'resource_version':contract['resource_version']},
      actor='admin:'+account['username'],origin=self.origin(context.source))
   if (not isinstance(result,dict) or result.get('operation_id')!=record['operation_id']
       or any(result.get(k)!=contract[k] for k in ('contract_hash',*_CONTRACT))
       or result.get('status') not in {'planned','awaiting_approval','queued','running','verifying','succeeded',
          'failed','cancelled','cancelling','needs_attention'}):
    raise SecurityError('official_host_receipt_invalid')
  except BaseException:
   # Unknown remains claimed forever. Failure to persist this observation still
   # leaves durable admitted evidence, so no caller may dispatch it again.
   try:await asyncio.shield(asyncio.to_thread(self._settle_effect,claim))
   except Exception:pass
   raise
  await asyncio.to_thread(self._settle_effect,claim,remote_status=result['status'])
  return {'operation':result,'approval_required':False,'executed':result.get('status')=='succeeded',
          'submitted':result.get('status') in {'queued','running','verifying'}}

 async def ensure(self,context,account,*,wait=True):
  if not isinstance(context,ApprovalContext):raise SecurityError('official_host_source_required')
  def current():
   with self._base_scope(context.source,context.policy,'approve') as (_,live,recipient):
    if live['account_id']!=account['account_id'] or live['version']!=account['version'] or recipient!=context.recipient:
     raise SecurityError('official_host_admin_changed')
   return self.mobile.store.task_authorization(account,None,context.payload())
  request=await asyncio.to_thread(current)
  if request is None:
   # Contract fields live in the prepared source snapshot displayed below, not
   # a task objective. The policy's digest commits their exact canonical bytes.
   request=await self.mobile.propose(account,kind='server_task',payload=context.payload(),transport_context=context,
     reference='official-task-policy:'+digest(canonical([account['account_id'],context.payload()])),
     summary=('本任务范围的精确宿主授权：\n来源会话：'+context.source.endpoint.native_conversation_id
      +'\n源版本：'+str(context.source.revision)+'\n精确合同：\n'
      +'\n'.join(action.evidence_json or canonical(action.data()) for action in context.policy.actions)))
  deadline=time.monotonic()+min(180,max(0,context.source.deadline_at-self.now()))
  while True:
   request=await asyncio.to_thread(current)
   if request is None:raise SecurityError('official_host_approval_revoked')
   if request['status']=='succeeded':return True
   if request['status'] not in {'sending','pending','awaiting_quorum','queued','executing'}:
    raise SecurityError('official_host_approval_rejected')
   if not wait:return False
   if time.monotonic()>=deadline:raise SecurityError('official_host_approval_expired')
   await asyncio.sleep(.25)

 async def grant(self,request):
  assert_approved('server_task',request['payload'])
  context=self.transport.context(request)
  if request['payload']!=context.payload():raise SecurityError('official_host_contract_changed')
  await asyncio.to_thread(self.mobile.store.validate_execution_approval,request['approval_id'],self.mobile.owner)
  def validate():
   with self._base_scope(context.source,context.policy,'approve') as (_,account,recipient):
    if account['account_id']!=request['account_id'] or recipient!=context.recipient:
     raise SecurityError('official_host_admin_changed')
  await asyncio.to_thread(validate)
  return {'ok':True,'message':'仅已核对的任务与精确宿主参数获准；执行仍须当前权限及远端版本检查。'}
