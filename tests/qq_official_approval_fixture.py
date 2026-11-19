"""Real isolated PG and fake loopback M3, using unapplied source overlay."""

from src.bot_security.passwords import hash_password
from src.bot_security.store import SecurityStore, SecurityError, canonical, digest
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from src.plugins.ai_chat.adapters.qq_official.approval import (
 ApprovalContext, FrozenSource, HostAction, TaskPolicy, Recipient, OfficialApprovalTransport)
from src.plugins.ai_chat.adapters.qq_official.admin_bindings import AdminBindingStore
from src.plugins.ai_chat.adapters.qq_official.authority import OwnerAuthority
from src.plugins.ai_chat.adapters.qq_official.turn_platform import assert_execution_current
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from tests.qq_official_turn_fixture import ExecutionFixture

SECRET=b'TEST_APPROVAL_ONLY_01234567890123456789'
class SourcePort:
 def __init__(self,case): self.case=case
 def require_current(self,cursor,*,source,policy,operation,now):
  c=self.case
  if not c.source_live or source!=c.source or policy!=c.policy or operation not in {'approve','execute'}:
   raise SecurityError('source_not_current')
  assert_execution_current(cursor,token=c.token,handle=c.handle,now=now,admission=c.admission)
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
  self.owners=OwnerAuthority(account=self.account,owner_c2c_ids=('TEST_ADMIN_A','TEST_ADMIN_B'),clock=lambda:self.now)
  with self.tx() as c:
   owners=self.owners.apply_trusted_config(c,self.account,('TEST_ADMIN_A','TEST_ADMIN_B'),'TEST_CONFIG')
   self.admins=AdminBindingStore(owners=self.owners,clock=lambda:self.now,control_guard=lambda c,**kw:kw['authorization'] is self)
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
  self.transport=OfficialApprovalTransport(token=self.token,secret=SECRET,owners=self.owners,
   clock=lambda:self.now,source_port=SourcePort(self))
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
 def propose(self,**changes):
  return self.store.propose(self.account_a,**(dict(bot_id=None,kind='server_task',payload=self.context.payload(),
   summary='TEST exact approved task',transport_context=self.context)|changes))
