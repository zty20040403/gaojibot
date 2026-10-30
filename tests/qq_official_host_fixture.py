"""Actual shared authorization + real PG Fleet store/HTTP contract, no host execution."""
from pathlib import Path
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import httpx

from tests import qq_official_approval_fixture as approval_fixtures
from src.bot_security.store import SecurityError
from src.plugins.ai_chat.adapters.qq_official.approval import TaskPolicy
from src.plugins.ai_chat.adapters.qq_official.turn_source import assert_execution_current
from src.plugins.ai_chat.adapters.qq_official.host_authority import AllowedService,OfficialHostAuthority
from src.plugins.ai_chat.server_task_authorization import ServerTaskAuthorization
from src.plugins.ai_chat.qq_action_authorization import bind_official_host_source
from src.plugins.ai_chat.fleet_authorization import FleetAuthorization
from src.plugins.ai_chat.fleet_client import FleetControlClient
from src.cluster_control.execution_storage import ClusterExecutionStore
from src.cluster_control.execution_service import ClusterExecutionService,WriteBackendBinding
from src.cluster_control.api import create_app
from src.cluster_control.service import FleetControlService

class CurrentHostSource:
 def __init__(self,c):self.c=c
 def require_current(self,cursor,*,source,policy,operation,now):
  c=self.c
  if source!=c.source or not c.source_live or not c.route_live or not isinstance(policy,TaskPolicy):
   raise SecurityError('source_not_current')
  if operation not in {'prepare','read','approve','execute'}:raise SecurityError('source_not_current')
  assert_execution_current(cursor,token=c.token,handle=c.handle,job=c.job,now=now,admission=c.admission)

class HostFixture(approval_fixtures.ApprovalFixture,unittest.IsolatedAsyncioTestCase):
 asyncSetUpBase=approval_fixtures.ApprovalLoopbackFixture.asyncSetUp
 asyncTearDownBase=approval_fixtures.ApprovalLoopbackFixture.asyncTearDown
 delivered=approval_fixtures.ApprovalLoopbackFixture.delivered
 async def asyncSetUp(self):
  await self.asyncSetUpBase()
  self.transport.source_port=CurrentHostSource(self)
  self.tmp=tempfile.TemporaryDirectory(prefix='TEST_OFFICIAL_HOST_');self.addCleanup(self.tmp.cleanup)
  root=Path(self.tmp.name);self.fleet_token=root/'TEST_TOKEN';self.fleet_token.write_text('TEST_ONLY_FLEET_'+'x'*48);self.fleet_token.chmod(0o600)
  self.remote_store=ClusterExecutionStore(self.database,root/'artifacts')
  self.execution=ClusterExecutionService(self.remote_store,inventory=({'host_id':'testhost','operate':True,
    'operable_units':['test.service']},),diagnostic_targets=(),worker_hosts={},
    write_backend=WriteBackendBinding('TEST_BACKEND',1,True))
  for module in ('execution_service','execution_storage'):
   p=patch('src.cluster_control.'+module+'.time',SimpleNamespace(time=lambda:self.now));p.start();self.addCleanup(p.stop)
  self.fleet_service=FleetControlService(None)
  self.app=create_app(self.fleet_service,api_token_file=self.fleet_token,execution=self.execution)
  self.http_calls=[]
  class ObservedASGI(httpx.ASGITransport):
   async def handle_async_request(inner,request):
    self.http_calls.append((request.method,request.url.path,json.loads(request.content) if request.content else None))
    return await super().handle_async_request(request)
  self.client=FleetControlClient('http://test-control.invalid',self.fleet_token,transport=ObservedASGI(app=self.app))
  self.host=OfficialHostAuthority(transport=self.transport,mobile=self.mobile,
    services=(AllowedService('testhost','test.service','TEST_BACKEND'),),clock=lambda:self.now)
  self.tasks=ServerTaskAuthorization(SimpleNamespace(mobile_authorization=self.mobile),official_authority=self.host)
  self.client.authorization=FleetAuthorization(self.client,self.mobile,official_authority=self.host)
  self.body={'host_id':'testhost','resource_ref':'test.service','operation':'service.restart',
             'idempotency_key':'TEST_OFFICIAL_SERVICE_1','deadline_at':self.now+120}
 async def asyncTearDown(self):
  await self.client.close();await self.fleet_service.close();await self.asyncTearDownBase()
 async def prepare(self,body=None):
  with bind_official_host_source(self.host,self.source):
   return await self.client.prepare_operation(body or self.body,actor='official',origin=self.source.scope.key)
 async def approve(self,record):
  with self.tasks.bind_inbound(self.source):
   return await self.client.approve_operation(record['operation_id'],record['contract_hash'],record['resource_version'],
      actor='official',origin=self.source.scope.key)
 async def quorum(self):
  row=self.query("SELECT * FROM admin_approvals WHERE kind='server_task'")[0]
  # Codes exist only in the fake physical sender's RAM capture.
  import re
  code=re.search(r'一次性口令：([0-9]{6})',self.fake.received[-1]['payload']['content'])[1]
  self.store.confirm(row['approval_id'],None,None,code,recipient=self.recipient)
  vote,code=self.store.request_review(row['approval_id'],None,None,recipient=self.recipient_b)
  await self.delivered(vote,code)
  self.store.confirm(vote['approval_id'],None,None,code,recipient=self.recipient_b)
  self.assertTrue(await self.mobile.run_once())
