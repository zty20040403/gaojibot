"""Real isolated PG and fake loopback M3, using unapplied source overlay."""
import unittest

from src.bot_security.passwords import hash_password
from src.bot_security.store import SecurityStore, SecurityError, canonical, digest
from src.bot_security.service import MobileAuthorization
from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from src.plugins.ai_chat.adapters.qq_official.approval import (
 ApprovalContext, ApprovalOtpSender, FrozenSource, HostAction, TaskPolicy, Recipient, OfficialApprovalTransport)
from src.plugins.ai_chat.adapters.qq_official.admin_bindings import AdminBindingStore
from src.plugins.ai_chat.adapters.qq_official.authority import OwnerAuthority
from src.plugins.ai_chat.adapters.qq_official.turn_source import assert_execution_current
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.adapters.qq_official.send_store import OfficialSendStore
from src.plugins.ai_chat.adapters.qq_official.sender import OfficialSender
from src.plugins.ai_chat.delivery import DeliveryStore
from src.qq_official.events import parse_dispatch
from src.plugins.ai_chat.adapters.qq_official.approval import prepare_approval_input,control_commitment,apply_control
from src.qq_official.credentials import Credentials
from src.qq_official.http import RestClient
from src.qq_official.token import TokenManager
from src.qq_official.testing.fake_platform import FakePlatform
from tests.qq_official_file_fixture import ExecutionFixture

SECRET=b'TEST_APPROVAL_ONLY_01234567890123456789'
class SourcePort:
 def __init__(self,case): self.case=case
 def require_current(self,cursor,*,source,policy,operation,now):
  c=self.case
  if not c.source_live or source!=c.source or policy!=c.policy or operation not in {'approve','execute'}:
   raise SecurityError('source_not_current')
  assert_execution_current(cursor,token=c.token,handle=c.handle,job=c.job,now=now,admission=c.admission)
  # Root logical RouteService is deliberately a named test port. The real
  # account/endpoint/job/execution fences above are not mocked.
  if not c.route_live: raise SecurityError('route_not_current')

class ApprovalFixture(ExecutionFixture):
 @classmethod
 def setUpClass(cls): cls.password=hash_password('TEST_ONLY_PASSWORD_123456')
 def provision(self,endpoint):
  if not getattr(self,'_private_initialized',False):
   self._private_initialized=True
   self.endpoint=EndpointKey(self.account,'private','TEST_ADMIN_A')
   self.actor=ActorKey(self.account,'c2c_user','','TEST_ADMIN_A')
   endpoint=self.endpoint
  super().provision(endpoint)
 def setUp(self):
  super().setUp()
  self.other=EndpointKey(self.account,'private','TEST_ADMIN_B')
  self.other_actor=ActorKey(self.account,'c2c_user','','TEST_ADMIN_B')
  self.provision(self.other)
  self.owners=OwnerAuthority(clock=lambda:self.now)
  with self.tx() as c:
   owners=self.owners.apply_trusted_config(c,self.account,('TEST_ADMIN_A','TEST_ADMIN_B'),'TEST_CONFIG')
   self.admins=AdminBindingStore(clock=lambda:self.now,control_guard=lambda c,**kw:kw['authorization'] is self)
   self.admin_a=self.admins.create_official_admin(c,username='test_admin_a',password_hash=self.password,
    endpoint=self.endpoint,actor=self.actor,owner_revision=owners[0].grant.revision,authorization=self,bootstrap=True)
   self.admin_b=self.admins.create_official_admin(c,username='test_admin_b',password_hash=self.password,
    endpoint=self.other,actor=self.other_actor,owner_revision=owners[1].grant.revision,authorization=self)
  self.handle=self.claim().handle
  self.begin(self.handle)
  self.arguments={'command_id':'TEST_READ_ONLY_COMMAND'}
  self.policy=TaskPolicy((HostAction('fleet.job.create','TEST_HOST',digest(canonical(self.arguments))),))
  self.source=FrozenSource(self.endpoint,self.actor,self.endpoint.scope,execution_id=self.handle.execution_id,
   revision=self.handle.execution_revision,route_epoch=0,endpoint_route_epoch=self.handle.route_epoch,deadline_at=self.now+180)
  self.source_live=self.route_live=True
  self.transport=OfficialApprovalTransport(token=self.token,secret=SECRET,clock=lambda:self.now,source_port=SourcePort(self))
  self.store=SecurityStore(self.database,SECRET,clock=lambda:self.now,transport=self.transport)
  self.addCleanup(self.store.close)
  self.recipient=Recipient(self.endpoint,self.actor)
  self.recipient_b=Recipient(self.other,self.other_actor)
  self.context=ApprovalContext(self.recipient,self.source,self.policy)
  with self.tx() as c:
   self.account_a=self.transport.account_for_recipient(c,self.recipient)
   self.account_b=self.transport.account_for_recipient(c,self.recipient_b)
   observe_ready(c,self.token,{'op':0,'t':'READY','d':{'session_id':'TEST_SESSION','user':{'id':'TEST_BOT','bot':True}}},self.now)
  self.window_a=self.window()
  self.window_b=self.window(endpoint=self.other)
 def control_command(self,text,recipient=None):
  recipient=recipient or self.recipient
  dispatch=parse_dispatch({'op':0,'t':'C2C_MESSAGE_CREATE','s':10,'id':'TEST_EVENT',
    'd':{'id':'TEST_CONTROL','content':text,'author':{'user_openid':recipient.actor.native_user_id}}})
  return prepare_approval_input(dispatch,recipient.endpoint)
 def source_receipt(self,command,recipient=None,number=10):
  recipient=recipient or self.recipient
  identifier='TEST_CONTROL_'+str(number)
  with self.tx() as c:
   c.execute("""INSERT INTO qq_official_inbound_events(inbox_id,account_key,endpoint_key,endpoint_generation,route_epoch,
     event_type,native_message_id,status,reason_code,echo_state,received_at,created_at,updated_at,
     admin_control_actor_key,admin_control_commitment) VALUES (?,?,?,1,1,'C2C_MESSAGE_CREATE',?,'processed',
      'admin_control','not_echo',?,?,?,?,?)""",(identifier,self.account.key,recipient.endpoint.key,identifier,
      self.now,self.now,self.now,recipient.actor.key,control_commitment(SECRET,inbox_id=identifier,
       endpoint=recipient.endpoint,actor=recipient.actor,command=command)))
   c.execute("""INSERT INTO qq_official_gateway_receipts(account_key,session_id,seq,event_type,disposition,inbox_id,received_at)
      VALUES (?,'TEST_CONTROL_SESSION',?,'C2C_MESSAGE_CREATE','control',?,?)""",(self.account.key,number,identifier,self.now))
  return identifier
 def apply_control(self,command,identifier,number=10):
  with self.tx() as c:return apply_control(c,store=self.store,inbox_id=identifier,session_id='TEST_CONTROL_SESSION',
      seq=number,command=command,now=self.now)
 def propose(self,**changes):
  return self.store.propose(self.account_a,**(dict(bot_id=None,kind='server_task',payload=self.context.payload(),
   summary='TEST exact approved task',transport_context=self.context)|changes))

class ApprovalLoopbackFixture(ApprovalFixture,unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.calls=DatabaseCalls();self.deliveries=DeliveryStore(self.database);self.addCleanup(self.deliveries.close)
  self.outbox=OfficialSendStore(self.deliveries,admission=lambda c,e,a:self.owners.owner_for_actor(c,e,a) is not None,
   pepper=SECRET,clock=lambda:self.now,approval_authority=self.transport)
  self.fake=FakePlatform(clock=lambda:self.now);self.fake.app_id=self.account.app_id;await self.fake.start()
  self.query("UPDATE qq_official_endpoints SET proactive_state='unknown'")
  for endpoint in (self.endpoint,self.other):
   self.fake.add_window('user',endpoint.native_conversation_id,'TEST_SOURCE')
   self.fake.proactive_allowed[('user',endpoint.native_conversation_id)]=True
  self.tokens=TokenManager(Credentials(self.account.app_id,self.fake.app_secret),self.fake.base_url,allow_test_server=True)
  self.rest=RestClient(self.tokens,allow_test_server=True)
  async def ensure():
   def check():
    with self.tx() as c:assert_epoch(c,self.token)
   await self.calls(check)
  self.sender=OfficialSender(self.account,self.token,self.outbox,self.rest,self.ledger,database_calls=self.calls,
   ensure_current=ensure,wall_clock=lambda:self.now,poll_seconds=.01)
  self.otp=ApprovalOtpSender(sender=self.sender,transport=self.transport,store=self.store)
  async def legacy(*args):raise AssertionError('legacy transport called')
  self.mobile=MobileAuthorization(self.store,sender=legacy,bot_selector=lambda:None,official_sender=self.otp)
 async def asyncTearDown(self):
  await self.sender.stop();await self.calls.close();await self.rest.aclose();await self.tokens.aclose();await self.fake.close()
 async def delivered(self,request,code):
  await self.mobile._send_code(request,code)
 async def approved(self):
  request,code=self.propose();await self.delivered(request,code)
  result=self.store.confirm(request['approval_id'],None,None,code,recipient=self.recipient)
  self.assertEqual(result['status'],'awaiting_quorum')
  vote,vcode=self.store.request_review(request['approval_id'],None,None,recipient=self.recipient_b)
  await self.delivered(vote,vcode)
  self.store.confirm(vote['approval_id'],None,None,vcode,recipient=self.recipient_b)
  return request
