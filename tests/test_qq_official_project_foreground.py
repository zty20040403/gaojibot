"""Actual shared foreground loop -> project RPC/storage -> existing M6/M3."""
from dataclasses import replace
from pathlib import Path
import json
import time
from types import SimpleNamespace
from unittest.mock import patch
from src.sandbox_runtime.docker import SandboxResult

from src.plugins.ai_chat.adapters.qq_official.delivery_configuration import DeliveryConfiguration
from src.plugins.ai_chat.adapters.qq_official.file_artifacts import file_job_kind
from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
from src.plugins.ai_chat.adapters.qq_official.ingest import process_inbox
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.adapters.qq_official.native_media import CONTRACT
from src.plugins.ai_chat.adapters.qq_official.turn_runtime import build_turn_consumer
from tests.qq_official_file_fixture import PDF
from tests.test_qq_official_host_composition import HostCompositionFixture
from tests import test_project_runtime as project_fixture
from tests import test_qq_official_turn_endtoend as turn_fixture


class ProjectForegroundTests(HostCompositionFixture):
    def setUp(self):
        super().setUp()
        # The existing real project worker owns its wall clock; align newly
        # admitted source timestamps before constructing any execution/grant.
        self.now=int(time.time())

    async def _exercise(self, *, revoke_context=False):
        official=self.runtime
        # Real ProjectRuntime plus private local runner. Only the Docker/kernel
        # driver below that RPC boundary is a fixture.
        await project_fixture.ProjectRuntimeTests.asyncSetUp(self)
        projects=self.runtime;self.runtime=official
        # The existing Docker fixture validates workspace identity but does
        # not implement bytes. Keep its checks and simulate only the final
        # kernel-level tee/wc/cat calls behind the real private runner RPC.
        contents={};driver_errors=[]
        original_run=self.driver._run
        async def docker_run(*args,**kwargs):
            if args[:2]==('docker','exec') and 'tee' in args:
                contents[(args[3],args[-1])]=kwargs['input_bytes']
                try:
                    container=next(row for key,row in self.docker.containers.items()
                        if key==args[3] or row['Id']==args[3])
                    mount=next(item['Source'] for item in container['Mounts']
                        if item['Destination']=='/workspace')
                    target=Path(mount)/args[-1].removeprefix('/workspace/')
                    target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(kwargs['input_bytes'])
                except Exception as exc:
                    driver_errors.append(repr(exc));raise
            if args[:2]==('docker','exec') and args[-1].startswith('wc -c < '):
                body=contents.get((args[2],args[-1].removeprefix('wc -c < ')))
                return SandboxResult(str(len(body)),'',0) if body is not None else SandboxResult('','TEST missing file',1)
            return await original_run(*args,**kwargs)
        async def docker_bytes(*args,**kwargs):
            self.assertEqual(args[:2],('docker','exec'))
            self.assertEqual(args[3:5],('cat','--'))
            return contents[(args[2],args[5])],b'',0
        self.driver._run=docker_run
        self.driver._run_bytes=docker_bytes
        project_settings=self.app.settings
        old=self.context
        self.app.__dict__.update(turn_fixture.OfficialTurnEndToEndTests.application(self).__dict__)
        self.app.settings=replace(project_settings,search_enabled=False,tool_intent_enabled=False,
            subagent_entry_enabled=False,tool_simple_max_rounds=8)
        self.app.mobile_authorization=old.mobile_authorization
        self.app.subagent_store=old.subagent_store
        self.app.subagent_coordinator=old.subagent_coordinator
        self.app.fleet_client=None
        self.app.delivery_store=self.deliveries
        self.app.project_runtime=projects
        self.context=self.app;official.application=self.app
        self.raw['files']['native_routes']=[dict(endpoint=self.endpoint.key,endpoint_generation=2,
            route_epoch=2,capability_revision=1,max_bytes=1024*1024,
            verified_at=self.now,expires_at=self.now+600,contract=CONTRACT,file_type=4)]
        endpoint=self.query('SELECT generation,route_epoch,capability_revision FROM qq_official_endpoints WHERE endpoint_key=?',
            (self.endpoint.key,))[0]
        self.raw['files']['native_routes'][0].update(endpoint_generation=endpoint['generation'],
            route_epoch=endpoint['route_epoch'],capability_revision=endpoint['capability_revision'])
        await self.build()
        await projects.start()
        await self.service.configure_project_runtime(projects)
        official.loaded=SimpleNamespace(account=self.account,credentials=object(),delivery=DeliveryConfiguration())
        official._gateway=SimpleNamespace(state='ready');official.subagent_service=None
        official.journal=self.app.turn_journal
        consumer=build_turn_consumer(official,self.token,self.app)
        job=self.jobs.claim_due('TEST_PROJECT_FOREGROUND',kinds=(account_job_kind('turn',self.account.key),),now=self.now)[0]
        count=0;tool_results=[]
        async def completion(**kwargs):
            nonlocal count
            self.now=int(time.time())
            count+=1
            definitions={tool['function']['name']:tool for tool in kwargs['tools']}
            self.assertIn('project_create',definitions)
            self.assertIn('sandbox_write_file',definitions)
            self.assertNotIn('sandbox_id',definitions['sandbox_write_file']['function']['parameters']['required'])
            if count==3:
                self.assertTrue(any('[当前项目资料]' in str(message.get('content',''))
                                    for message in kwargs['messages']))
                if revoke_context:
                    self.query("UPDATE qq_official_project_grants SET state='revoked',revision=revision+1,revoked_at=issued_at")
            messages=[message['content'] for message in kwargs['messages'] if message['role']=='tool']
            if messages:
                content=messages[-1]
                result=json.loads(content[content.index('{'):content.rindex('}')+1])
                tool_results.append(result)
                self.assertTrue(result.get('ok'),result)
            if count==1:name,args='project_create',{'title':'TEST foreground project'}
            elif count==2:name,args=None,None
            elif count==3:
                name,args=('use_skill',{'name':'TEST_MISSING_SKILL'}) if revoke_context else (
                    'sandbox_write_file',{'path':'report.pdf','content':PDF.decode()})
            elif count==4:name,args='sandbox_read_file',{'path':'report.pdf'}
            elif count==5:name,args='send_file_from_sandbox',{'path':'report.pdf','filename':'report.pdf'}
            else:name,args=None,None
            calls=[SimpleNamespace(id='TEST_PROJECT_TOOL_'+str(count),type='function',
                function=SimpleNamespace(name=name,arguments=json.dumps(args)))] if name else []
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='' if name else 'TEST project file queued',tool_calls=calls))],usage=None)
        with patch('src.plugins.ai_chat.agent_loop._create_completion',new=completion):
            result=await consumer(job)
            self.assertEqual(result['status'],'handed_off')
            self.jobs.mark_succeeded(job.job_id,job.lease_owner,lease_generation=job.lease_generation,now=self.now)
            # Wake after the creating foreground yields its scope. The actual
            # durable project worker publishes the verified workspace receipt.
            await self.worker.run_once()
            self.now=int(time.time())
            self.assertEqual(self.query("SELECT status FROM projects WHERE title='TEST foreground project'")[0]['status'],
                'active',[dict(row) for row in self.query('SELECT kind,status,last_error FROM durable_jobs')])
            accepted=self.accept()
            with self.tx() as cursor:
                process_inbox(cursor,inbox_id=accepted.inbox_id,lease_token=self.token,ledger=self.app.message_ledger,
                    jobs=self.jobs,epoch_guard=assert_epoch,admission=official.store.admitted,now=self.now)
            self.fake.add_window('group',self.endpoint.native_conversation_id,'TEST_ASSEMBLY_2')
            job=self.jobs.claim_due('TEST_PROJECT_FOREGROUND_2',kinds=(account_job_kind('turn',self.account.key),),now=self.now)[0]
            if revoke_context:
                from src.plugins.ai_chat.agent.control import RouteAuthorityLost
                with self.assertRaises(RouteAuthorityLost):await consumer(job)
                self.assertEqual(count,3)  # no next model call can reuse the cache
                self.assertEqual(self.query('SELECT * FROM project_io'),[])
                self.assertFalse(self.fake.received)
                return
            result=await consumer(job)
        self.assertEqual(result['status'],'handed_off')
        self.assertEqual(count,6,(tool_results,driver_errors))
        self.jobs.mark_succeeded(job.job_id,job.lease_owner,lease_generation=job.lease_generation,now=self.now)
        self.assertEqual(await projects.autosave.run_once(now=self.now),1)
        tracker=self.query('SELECT * FROM project_turn_saves')[0]
        self.assertEqual(tracker['state'],'queued',dict(tracker))
        await self.worker.run_once()
        saved=self.jobs.get(tracker['job_id'])
        self.assertEqual(saved.status,'succeeded',self.logger.errors)
        version=self.query('SELECT * FROM project_versions WHERE source_turn_id=?',(tracker['turn_id'],))
        self.assertEqual(len(version),1)
        self.assertEqual(self.query('SELECT count(*) FROM qq_official_project_io_authority')[0][0],1)
        self.assertTrue(any('TEST controlled fixture' in item.get('content','') for item in tool_results))
        artifact_result=next((item for item in tool_results if item.get('artifact_handle')),None)
        self.assertIsNotNone(artifact_result,tool_results)
        self.assertEqual(artifact_result['status'],'stored')
        self.assertFalse(artifact_result['delivery_confirmed'])
        self.assertEqual(len(self.query('SELECT * FROM qq_official_project_artifacts')),1)
        self.assertEqual(self.query('SELECT * FROM project_file_deliveries'),[])
        file_job=self.jobs.claim_due('TEST_PROJECT_FILE',kinds=(file_job_kind(self.account),),now=self.now)[0]
        self.outbox.protected_payloads=self.files.payloads
        self.assertEqual((await self.files.dispatcher(file_job))['status'],'handed_off')
        delivery=self.query('''SELECT delivery_id FROM qq_official_file_message_plans''')[0]['delivery_id']
        self.assertEqual((await self.sender.send_delivery(delivery)).status,'committed')
        self.assertEqual([item['payload']['msg_type'] for item in self.fake.received],[7])
        self.assertEqual(len(self.query('SELECT * FROM delivery_receipts WHERE applied_at IS NOT NULL')),1)
        self.assertFalse(any(name in self.app.__dict__ for name in ('bot','event')))
        self.assertIs(projects.official_provider_factory,self.service.project_router)

    async def test_real_config_shared_loop_creates_writes_reads_and_delivers_project_file(self):
        await self._exercise()

    async def test_revoked_cached_project_context_never_reaches_next_model_call(self):
        await self._exercise(revoke_context=True)
