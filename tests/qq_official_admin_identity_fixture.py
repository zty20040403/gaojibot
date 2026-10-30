from contextlib import contextmanager
import hashlib,secrets
from types import SimpleNamespace
from src.bot_security.store import SecurityStore
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from src.plugins.ai_chat.platform_types import AccountKey
from src.plugins.ai_chat.adapters.qq_official.authority import ConsoleAuthorization,OwnerAuthority
from src.plugins.ai_chat.adapters.qq_official.binding import BindingReducer
from src.plugins.ai_chat.adapters.qq_official.inbox import OfficialInboxStore
from src.plugins.ai_chat.adapters.qq_official.lease import DedicatedAccountLease,assert_epoch
from src.plugins.ai_chat.adapters.qq_official.admin_identity import AdminIdentityBinding

PASSWORD='TEST-bind-password-only-123456'
class IdentityFixture:
 def setUp(self):
  from tests.postgres_fixture import postgres_for_test
  self.database=postgres_for_test(self)
  self.schema=self.database.schema;self.dsn=self.database.dsn
  self.now=1800000000;self.account=AccountKey('qq-official','TEST_OBS_'+secrets.token_hex(4),'test');self.seq=0;self.session='TEST_SESSION'
  self.owner='TEST_C2C_OWNER';self.pepper=b'TEST_OBSERVABILITY_BINDING_PEPPER_123456789'
  self.owners=OwnerAuthority(clock=lambda:self.now)
  self.jobs=DurableJobStore(self.database);self.addCleanup(self.jobs.close)
  self.store=OfficialInboxStore(self.database,jobs=self.jobs,binding=BindingReducer(self.pepper,clock=lambda:self.now,epoch_guard=assert_epoch),clock=lambda:self.now)
  self.addCleanup(self.store.close);self.store.ensure_account(self.account)
  self.lease=DedicatedAccountLease(self.dsn,self.account,schema=self.schema,clock=lambda:self.now);self.addCleanup(self.lease.close)
  self.token=self.lease.acquire();self.store.apply_configuration(self.token,(self.owner,))
  self.store.accept({'op':0,'s':0,'t':'READY','d':{'session_id':self.session}},token=self.token,session_id=self.session)
  self.security=SecurityStore(self.database,b'TEST_SECURITY_PEPPER_12345678901234567',clock=lambda:self.now);self.addCleanup(self.security.close)
  self.admin=self.security.bootstrap_local_admin('test_admin',PASSWORD)
  self.login=self.security.login('test_admin',PASSWORD,'127.0.0.1')
  self.auth=ConsoleAuthorization(self.admin['account_id'],self.admin['version'],hashlib.sha256(self.login['session'].encode()).hexdigest())
  self.runtime=SimpleNamespace(_token=self.token,_stopping=False,store=self.store,loaded=SimpleNamespace(account=self.account,binding_pepper=self.pepper))
  self.service=AdminIdentityBinding(self.runtime,self.token);self.store.admin_identity_service=self.service
 @contextmanager
 def tx(self):
  with self.store.transaction() as c:yield c
 def rows(self,query,args=()):
  with self.tx() as c:return [dict(r) for r in c.execute(query,args).fetchall()]
 def receive(self,command,*,actor=None,group=False,timestamp='2027-01-15T08:00:00Z'):
  self.seq+=1;d={'id':f'TEST_NATIVE_{self.seq}','content':command,'author':{'member_openid' if group else 'user_openid':actor or self.owner},'timestamp':timestamp}
  if group:d['group_openid']='TEST_GROUP'
  return self.store.accept({'op':0,'s':self.seq,'id':f'TEST_EVENT_{self.seq}','t':'GROUP_MESSAGE_CREATE' if group else 'C2C_MESSAGE_CREATE','d':d},token=self.token,session_id=self.session)
 def challenge(self):return self.service.create(self.auth)
