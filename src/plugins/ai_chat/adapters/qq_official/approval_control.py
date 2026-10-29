"""Internal TxA control preparation/reduction; no network, body storage or queue.

Root ingress must classify before storing raw payload, insert body-free source
and authenticated gateway receipt, then call apply_control in that SAME TX.
The source row's HMAC commits the actual normalized RAM command. Models never
receive this API. Unknown root source assembly is not manufactured here.
"""
from dataclasses import dataclass,field
import hashlib
import hmac
import re

from src.bot_security.service import approval_command
from src.bot_security.store import SecurityError,canonical
from .approval_transport import Recipient
from .input_normalization import normalize_input
from ...platform_types import ActorKey,EndpointKey

_VERBS={'确认':'confirm','confirm':'confirm','取消':'cancel','cancel':'cancel','重发':'resend','resend':'resend',
        '复核':'review','review':'review','撤销复核':'revoke_review','审批状态':'status'}
_EMBEDDED=re.compile(r'(?:^|[\s>])/?(?:确认|取消|重发|审批状态|复核|撤销复核|confirm|cancel|resend|review)\s+AP-',re.I)
_ID=re.compile(r'AP-[0-9A-F]{12}')

@dataclass(frozen=True)
class ApprovalInput:
 kind:str
 text:str=field(default='',repr=False)
 action:str='invalid'
 approval_id:str|None=field(default=None,repr=False)
 code:str=field(default='',repr=False)

@dataclass(frozen=True)
class ControlResult:
 decision:str
 duplicate:bool=False
 # Only the post-commit runtime may hand these RAM fields to MobileAuthorization.
 request:dict|None=field(default=None,repr=False)
 code:str|None=field(default=None,repr=False)


def prepare_approval_input(dispatch,endpoint):
 normalized=normalize_input(dispatch)
 if normalized.kind!='message': return ApprovalInput('none')
 text=normalized.input_text
 private=endpoint.kind=='private'
 if not approval_command(text,private=private):
  original=dispatch.message.content
  if (_EMBEDDED.search(original) or re.fullmatch(r'[0-9]{6}',original.strip())
      or re.fullmatch(r'(?:\s*<@[^>]{1,257}>){1,16}\s*[0-9]{6}\s*',original)):
   return ApprovalInput('quarantine')
  return ApprovalInput('none')
 if not private: return ApprovalInput('quarantine')
 if re.fullmatch(r'[0-9]{6}',text.strip()):return ApprovalInput('control',text,'confirm',code=text.strip())
 parts=text.strip().lstrip('/').split()
 action=_VERBS.get(parts[0].lower(),'invalid') if parts else 'invalid'
 identifier=parts[1].upper() if len(parts)>1 else ''
 if not _ID.fullmatch(identifier):return ApprovalInput('control',text)
 if action=='confirm':
  if len(parts)!=3 or not re.fullmatch(r'[0-9]{6}',parts[2]):return ApprovalInput('control',text)
  return ApprovalInput('control',text,action,identifier,parts[2])
 if len(parts)!=2:return ApprovalInput('control',text)
 return ApprovalInput('control',text,action,identifier)


def control_commitment(secret,*,inbox_id,endpoint,actor,command):
 if not isinstance(secret,bytes) or len(secret)<32 or not isinstance(command,ApprovalInput) or command.kind!='control':
  raise SecurityError('approval_control_invalid')
 data=[inbox_id,endpoint.key,actor.key,command.text,command.action,command.approval_id,command.code]
 return hmac.new(secret,b'approval-control-v1:'+canonical(data).encode(),hashlib.sha256).hexdigest()


def apply_control(cursor,*,store,inbox_id,session_id,seq,command,now):
 transport=store.transport
 if transport is None:raise SecurityError('approval_control_unavailable')
 transport.begin(cursor)
 row=cursor.execute('''SELECT i.* FROM qq_official_inbound_events i
   JOIN qq_official_gateway_receipts r ON r.inbox_id=i.inbox_id AND r.account_key=i.account_key
   WHERE i.inbox_id=? AND i.account_key=? AND r.session_id=? AND r.seq=?
     AND r.disposition='control' AND r.event_type=i.event_type
     AND i.event_type='C2C_MESSAGE_CREATE' AND i.status='processed' AND i.reason_code='admin_control'
     AND i.echo_state='not_echo' AND i.payload_json IS NULL AND i.turn_text IS NULL
     AND i.admin_control_actor_key IS NOT NULL AND i.admin_control_commitment IS NOT NULL FOR UPDATE OF i''',
   (inbox_id,transport.token.account_key,session_id,seq)).fetchone()
 if row is None:raise SecurityError('authenticated_approval_source_required')
 recipient=Recipient(EndpointKey.parse(row['endpoint_key']),ActorKey.parse(row['admin_control_actor_key']))
 if type(now)is not int or now!=transport.now() or now<row['received_at'] or now>=row['received_at']+30:
  raise SecurityError('approval_control_source_expired')
 expected=control_commitment(transport.secret,inbox_id=inbox_id,endpoint=recipient.endpoint,actor=recipient.actor,command=command)
 if not hmac.compare_digest(expected,row['admin_control_commitment']):raise SecurityError('approval_control_source_conflict')
 prior=cursor.execute('SELECT decision FROM qq_official_admin_control_receipts WHERE inbox_id=?',(inbox_id,)).fetchone()
 if prior:return ControlResult(prior['decision'],True)
 result=ControlResult('denied')
 associated=None
 try:
  with store.enlist(cursor):
   # Same actual admin binding as console/bootstrap; C2C presence is not admin.
   account=transport.account_for_recipient(cursor,recipient)
   action=command.action
   if action=='confirm':
    request=store.confirm(command.approval_id,None,None,command.code,recipient=recipient)
    associated=request['approval_id'];result=ControlResult('accepted')
   elif action in {'review','resend'}:
    function=store.request_review if action=='review' else store.resend
    request,code=function(command.approval_id,None,None,recipient=recipient)
    associated=request['approval_id'];result=ControlResult('sending' if code else 'status',request=request,code=code)
   elif action in {'cancel','revoke_review'}:
    function=store.cancel if action=='cancel' else store.revoke_review
    function(command.approval_id,None,None,recipient=recipient)
    associated=command.approval_id;result=ControlResult('cancelled')
   elif action=='status':
    request=store.get(command.approval_id,account['account_id'])
    associated=request['approval_id'];result=ControlResult('status')
 except (SecurityError,PermissionError):
  # No raw exception/code/argument is persisted. Inner savepoints retain the
  # original SecurityStore semantics, including committed failed-code counts.
  result=ControlResult('denied')
 cursor.execute('''INSERT INTO qq_official_admin_control_receipts
   (inbox_id,account_key,endpoint_key,actor_key,source_session_id,source_seq,action,decision,approval_id,created_at)
   VALUES (?,?,?,?,?,?,?,?,?,?)''',(inbox_id,transport.token.account_key,recipient.endpoint.key,recipient.actor.key,
    session_id,seq,command.action,result.decision,associated,now))
 return result
