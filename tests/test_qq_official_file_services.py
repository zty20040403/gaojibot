"""Host composition against real PG/source authority, with no sandbox/network."""
import asyncio
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import socket
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.agent.control import LeaseLost
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.platform_types import AccountKey
from src.plugins.ai_chat.project_blobs import ProjectBlobStore
from src.plugins.ai_chat.adapters.qq_official.file_artifacts import ArtifactError, ArtifactStore, file_job_kind
from src.plugins.ai_chat.adapters.qq_official.file_generation import GenerationBindingAdapter
from src.plugins.ai_chat.adapters.qq_official.file_links import ArtifactRef, LinkError, create_download_runner
from src.plugins.ai_chat.adapters.qq_official.file_services import (
    FileServiceConfig, FileServices, NativeRouteEvidence, _DownloadStore, file_services_factory,
)
from src.plugins.ai_chat.adapters.qq_official.outbox import OfficialOutbox
from tests.qq_official_file_fixture import FileFixture, PDF, KEY

ORIGIN='https://download.example'


class FakeManager:
    def __init__(self):
        self.items=[]; self.calls=[]; self.failure=None; self.after_list=None
    async def list(self,owner):
        self.calls.append(('list',owner))
        if self.failure: raise self.failure
        if self.after_list: self.after_list()
        return self.items.copy()
    async def destroy(self,owner,sandbox_id):
        self.calls.append(('destroy',owner,sandbox_id))
        self.items=[item for item in self.items if item['sandbox_id']!=sandbox_id]
        return {'destroyed':True}


class FileServiceTests(FileFixture):
    def setUp(self):
        super().setUp()
        temp=tempfile.TemporaryDirectory(prefix='qq-services-test-')
        self.addCleanup(temp.cleanup)
        self.private=Path(temp.name)
        self.calls=DatabaseCalls()
        self.addCleanup(lambda:asyncio.run(self.calls.close()))
        self.deliveries=DeliveryStore(self.database,recover_interrupted=False)
        self.addCleanup(self.deliveries.close)
        self.outbox=OfficialOutbox(self.deliveries,admission=self.admission,
            pepper=b'TEST_SERVICES_PEPPER'*2,clock=lambda:self.now)
        self.manager=FakeManager()
        self.runtime=SimpleNamespace(_calls=self.calls,deliveries=self.deliveries,jobs=self.jobs,
            outbox=self.outbox,_stopping=False,_token=self.token,
            store=SimpleNamespace(admitted=self.admission,clock=lambda:self.now))
        self.config=FileServiceConfig(self.private,self.manager,route_authority=lambda *_:True,
            link_origin=ORIGIN,link_reachable=lambda:True,generation_enabled=True,min_free_bytes=0)
        self.service=None
        self.addCleanup(self.cleanup_service)

    def cleanup_service(self):
        if self.service is not None:asyncio.run(self.service.close())

    def build(self,**changes):
        self.config=replace(self.config,**changes)
        self.service=FileServices(self.runtime,self.token,None,self.config)
        self.outbox.protected_payloads=self.service.payloads
        self.binding=GenerationBindingAdapter(self.source,self.service.artifacts,self.calls,
            account_limit=self.config.account_capacity,actor_limit=self.config.actor_capacity)
        return self.service

    def item(self,content=PDF):
        return self.service.artifacts.register(self.source,content=content,filename='report.pdf',logical_request_key=KEY)

    async def publish(self,content=PDF):
        item=self.item(content)
        job=self.jobs.claim_due('TEST_SERVICE_FILE',kinds=(file_job_kind(self.account),),now=self.now)[0]
        self.window(source=self.source.source_native_message_id)
        with self.tx() as cursor:
            grant=self.service.payloads.issue(cursor,token=self.token,job=job,artifact_id=item.artifact_id,now=self.now)
            _,intent,_=self.service.dispatcher._link_intent(cursor,grant)
        def authority(cursor,ref,expiry):
            row,_=self.service.payloads.assert_grant(cursor,grant,self.now)
            return (ref.artifact_id==row['artifact_id'] and ref.sha256==row['sha256']
                and expiry==intent['publication_expires_at'])
        publisher=self.service.publisher_factory(authority)
        ref=ArtifactRef(item.artifact_id,item.sha256,item.byte_size,item.mime,item.filename)
        link=await publisher.publish(ref,request_key=intent['publication_request_key'],
            expires_at=intent['publication_expires_at'])
        publication=await publisher.store.get(link.publication_ref)
        with self.tx() as cursor:
            credential=self.service.payloads.register_link(cursor,grant,link.publication_ref,self.now)
            self.service.payloads.handoff(cursor,grant,credential,self.outbox,self.now)
        return publication

    def test_unconfigured_factory_is_zero_io(self):
        class NoAccess:
            def __getattribute__(self,name):raise AssertionError(name)
        self.assertIsNone(asyncio.run(file_services_factory()(NoAccess(),NoAccess(),NoAccess())))
        self.assertEqual(list(self.private.iterdir()),[])

    def test_account_directory_is_private_and_no_route_means_unavailable(self):
        self.build(route_authority=None)
        self.assertEqual(self.service.directory.name,hashlib.sha256(self.account.key.encode()).hexdigest())
        self.assertEqual(self.service.directory.stat().st_mode&0o777,0o700)
        self.assertIsNotNone(self.service.download_app)  # app only; no listener is created.
        self.assertFalse(asyncio.run(self.service.readiness(self.binding)).available)
        self.assertEqual(self.manager.calls,[])
        self.assertEqual(self.query('SELECT * FROM delivery_attempts'),[])

    def test_generation_can_wait_for_window_and_readiness_never_reserves(self):
        self.build()
        self.assertTrue(asyncio.run(self.service.readiness(self.binding)).available)
        item=self.item()
        with self.tx() as cursor:
            row=dict(cursor.execute('SELECT * FROM qq_official_file_artifacts WHERE artifact_id=?',(item.artifact_id,)).fetchone())
            self.assertFalse(self.service.check_delivery_ready(cursor,row,self.now))
        self.window(source=self.source.source_native_message_id)
        before=self.query('SELECT * FROM qq_official_reply_windows')
        with self.tx() as cursor:self.assertTrue(self.service.check_delivery_ready(cursor,row,self.now))
        self.assertEqual(before,self.query('SELECT * FROM qq_official_reply_windows'))
        self.assertEqual(self.query('SELECT * FROM delivery_attempts'),[])
        self.assertEqual(self.query('SELECT * FROM qq_official_generation_capacity'),[])

    def test_existing_owned_workspace_remains_ready_at_capacity(self):
        self.build(account_capacity=1,actor_capacity=1)
        async def run():
            await self.binding.reserve_capacity(self.binding.owner)
            self.assertTrue((await self.service.readiness(self.binding)).available)
            with self.assertRaisesRegex(ValueError,'exhausted'):
                await self.binding.reserve_capacity(self.binding.owner)
        asyncio.run(run())

    def test_full_physical_quota_allows_owned_capture_but_blocks_new_bytes(self):
        self.build(quota_bytes=len(PDF),max_file_bytes=len(PDF),max_artifacts=1)
        async def run():
            cap=await self.binding.reserve_capacity(self.binding.owner)
            self.service.artifacts.capture_verified(self.source,content=PDF,filename='report.pdf')
            self.assertEqual((await self.service.readiness(self.binding)).max_bytes,len(PDF))
            with self.assertRaisesRegex(ArtifactError,'quota'):
                self.service.artifacts.capture_verified(self.source,content=PDF.replace(b'TEST',b'NEW!'),filename='report.pdf')
            await self.binding.release_capacity(cap)
            self.assertFalse((await self.service.readiness(self.binding)).available)
        asyncio.run(run())

    def test_full_quota_restores_exact_task_capture_without_a_workspace(self):
        from unittest.mock import Mock
        from src.plugins.ai_chat.adapters.qq_official import execution
        from src.plugins.ai_chat.adapters.qq_official.file_artifacts import task_source
        from src.plugins.ai_chat.adapters.qq_official.subagent_runtime import submit, job_kind
        from src.plugins.ai_chat.agent.contracts import ContextPacket
        from src.plugins.ai_chat.model_catalog import ModelCatalog, ModelProfile
        from src.plugins.ai_chat.subagents import SubAgentCoordinator, SubAgentStore, TaskStep
        self.build(quota_bytes=len(PDF),max_file_bytes=len(PDF),max_artifacts=1)
        tasks=SubAgentStore(self.database); self.addCleanup(tasks.close)
        profile=ModelProfile(name='test',provider='test',protocol='openai-chat',model='test',
            base_url='http://127.0.0.1',api_key_required=False)
        coordinator=SubAgentCoordinator(tasks,ModelCatalog({'test':profile},default_profile='test'),logger=Mock())
        packet=ContextPacket(self.endpoint.scope.key,'TEST_RESTORED_CAPTURE',None,
            self.message.canonical_message_id,'生成文件',endpoint=self.endpoint,requester_actor=self.actor)
        with self.tx() as cursor:
            task=submit(cursor,token=self.token,source_api=execution,source_handle=self.handle,source_job=self.job,
                tool_call_id='TEST_CAPTURE',tool_name='run_subagents',packet=packet,decision=None,profile='test',
                now=self.now,execution_deadline=self.now+1200,result_wait_seconds=600,admission=self.admission,
                coordinator=coordinator,jobs=self.jobs)
        job=self.jobs.claim_due('TEST_CAPTURE_WORKER',kinds=(job_kind(self.account),),now=self.now)[0]
        tasks.set_task_state(task.task_id,'running')
        with self.tx() as cursor:
            source=task_source(cursor,token=self.token,task_id=task.task_id,job=job,
                admission=self.admission,now=self.now,pending_deadline_at=self.now+86400)
        binding=GenerationBindingAdapter(source,self.service.artifacts,self.calls)
        blob=self.service.artifacts.capture_verified(source,content=PDF,filename='report.pdf')
        self.assertFalse(asyncio.run(self.service.readiness(binding)).available)
        run=tasks.create_run(task.task_id,TaskStep('doc','doc','TEST','PDF'),allowed_tools=(),model_profile='test',now=self.now)
        metadata={'handle':'s123abc:/workspace/report.pdf','name':'report.pdf','snapshot':blob.sha256,
            'size':blob.byte_size,'capture_version':1,'kind':'file','source_kind':'file'}
        tasks.add_artifacts(task.task_id,run.run_id,[metadata],now=self.now)
        tasks.finish_run(run.run_id,'succeeded',result={'artifacts':[metadata]},now=self.now)
        self.assertTrue(asyncio.run(self.service.readiness(binding)).available)
        self.assertFalse(asyncio.run(self.service.readiness(self.binding)).available)
        self.query('UPDATE subagent_tasks SET cancel_requested=TRUE WHERE task_id=?',(task.task_id,))
        with self.assertRaises(LeaseLost):asyncio.run(self.service.readiness(binding))

    def test_native_evidence_is_exact_and_not_inferred_from_platform_role(self):
        proof=NativeRouteEvidence(self.endpoint,1,1,0,1024,self.now,self.now+60)
        revision=self.query('SELECT capability_revision FROM qq_official_endpoints')[0]['capability_revision']
        proof=replace(proof,capability_revision=revision)
        self.build(link_origin=None,native_evidence=lambda *_:proof)
        self.assertTrue(asyncio.run(self.service.readiness(self.binding)).available)
        for changed in (replace(proof,endpoint_generation=2),replace(proof,route_epoch=2),
                replace(proof,capability_revision=revision+1),replace(proof,file_type=1),
                replace(proof,verified_at=self.now+1),replace(proof,expires_at=self.now),
                replace(proof,contract='TEST_UNKNOWN'),replace(proof,max_bytes=True)):
            self.service.config=replace(self.config,native_evidence=lambda *_,value=changed:value)
            self.assertFalse(asyncio.run(self.service.readiness(self.binding)).available)

    def test_native_only_route_never_promises_unsupported_large_uploads(self):
        revision=self.query('SELECT capability_revision FROM qq_official_endpoints')[0]['capability_revision']
        proof=NativeRouteEvidence(self.endpoint,1,1,revision,20_000_000,self.now,self.now+60)
        self.build(link_origin=None,native_evidence=lambda *_:proof)
        ready=asyncio.run(self.service.readiness(self.binding))
        self.assertTrue(ready.available)
        self.assertEqual(ready.max_bytes,3_500_000)
        artifact=dict(endpoint_key=self.endpoint.key,requester_actor_key=self.actor.key,
            endpoint_generation=1,route_epoch=1,byte_size=ready.max_bytes+1,draft=False)
        with self.tx() as cursor:self.assertFalse(self.service._route(cursor,artifact,'native'))

    def test_download_rechecks_cancel_and_each_chunk_without_parent_running(self):
        self.build()
        async def run():
            pub=await self.publish(b'%PDF-1.4\n'+b'x'*70000+b'\n%%EOF\n')
            turn=self.query('SELECT journal_turn_id FROM qq_official_turn_executions')[0]['journal_turn_id']
            self.result(self.handle,turn_id=turn)
            self.jobs.mark_succeeded(self.job.job_id,self.job.lease_owner,now=self.now,
                lease_generation=self.job.lease_generation)
            store=_DownloadStore(self.service,self.service._publication_store)
            self.assertIsNotNone(await store.by_digest(pub.token_digest))
            async with self.service.open_artifact(pub.artifact) as stream:
                self.assertEqual(len(await anext(stream.chunks)),65536)
                self.query("UPDATE qq_official_turn_executions SET state='cancelled' WHERE execution_id=?",(self.handle.execution_id,))
                with self.assertRaises(LinkError):await anext(stream.chunks)
            self.assertIsNone(await store.by_digest(pub.token_digest))
        asyncio.run(run())

    def test_download_denies_endpoint_rebind_expiry_and_host_route_revocation(self):
        self.build()
        async def run():
            pub=await self.publish()
            store=_DownloadStore(self.service,self.service._publication_store)
            self.assertIsNotNone(await store.get(pub.publication_ref))
            self.service.config=replace(self.config,route_authority=lambda *_:False)
            self.assertIsNone(await store.get(pub.publication_ref))
            self.service.config=self.config
            self.query('UPDATE qq_official_endpoints SET route_epoch=route_epoch+1 WHERE endpoint_key=?',(self.endpoint.key,))
            self.assertIsNone(await store.get(pub.publication_ref))
            self.query('UPDATE qq_official_endpoints SET route_epoch=route_epoch-1 WHERE endpoint_key=?',(self.endpoint.key,))
            self.now=pub.expires_at
            self.assertIsNone(await store.get(pub.publication_ref))
        asyncio.run(run())

    def test_actual_download_head_and_get_are_revoked_with_source(self):
        self.build()
        async def run():
            pub=await self.publish()
            link=await self.service._maintenance_publisher.restore(pub.credential_ref)
            sock=socket.socket(); sock.bind(('127.0.0.1',0))
            url='http://127.0.0.1:'+str(sock.getsockname()[1])+urlsplit(link.url).path
            runner=create_download_runner(self.service.download_app)
            await runner.setup(); await web.SockSite(runner,sock).start()
            try:
                async with aiohttp.ClientSession() as client:
                    async with client.head(url) as response:self.assertEqual(response.status,200)
                    async with client.get(url) as response:
                        self.assertEqual(response.status,200); self.assertEqual(await response.read(),PDF)
                    self.query("UPDATE qq_official_turn_executions SET state='needs_attention' WHERE execution_id=?",(self.handle.execution_id,))
                    async with client.head(url) as response:self.assertEqual(response.status,404)
                    async with client.get(url) as response:self.assertEqual(response.status,404)
            finally:await runner.cleanup()
        asyncio.run(run())

    def test_expired_capacity_requires_exact_owner_absence(self):
        self.build()
        async def run():
            await self.binding.reserve_capacity(self.binding.owner)
            self.now=self.deadline+1
            self.manager.items=[{'sandbox_id':'s123abc'}]
            await self.service._capacity()
            self.assertEqual(self.query('SELECT state FROM qq_official_generation_capacity')[0]['state'],'reserved')
            self.assertEqual(self.manager.calls,[('list',self.binding.owner)])
            self.manager.items=[]
            await self.service._capacity()
            row=self.query('SELECT state,release_evidence FROM qq_official_generation_capacity')[0]
            self.assertEqual((row['state'],row['release_evidence']),('released','host_verified_absent'))
        asyncio.run(run())

    def test_expired_capacity_lost_lease_after_lookup_does_not_refund(self):
        self.build()
        async def run():
            await self.binding.reserve_capacity(self.binding.owner)
            self.now=self.deadline+1
            self.manager.after_list=lambda:setattr(self.runtime,'_token',None)
            with self.assertRaises(LeaseLost):await self.service._capacity()
            self.assertEqual(self.query('SELECT state FROM qq_official_generation_capacity')[0]['state'],'reserved')
        asyncio.run(run())

    def test_unknown_host_inventory_retains_expired_capacity(self):
        self.build()
        async def run():
            await self.binding.reserve_capacity(self.binding.owner)
            self.now=self.deadline+1; self.manager.failure=OSError('TEST_PRIVATE_HOST_DETAILS')
            await self.service._capacity()
            self.assertEqual(self.query('SELECT state FROM qq_official_generation_capacity')[0]['state'],'reserved')
        asyncio.run(run())

    def test_explicit_host_cleanup_destroys_only_exact_owner_then_rechecks(self):
        self.build(destroy_expired_resources=True)
        async def run():
            await self.binding.reserve_capacity(self.binding.owner)
            self.now=self.deadline+1; self.manager.items=[{'sandbox_id':'s123abc'}]
            await self.service._capacity()
            self.assertEqual(self.manager.calls,[('list',self.binding.owner),
                ('destroy',self.binding.owner,'s123abc'),('list',self.binding.owner)])
            self.assertEqual(self.query('SELECT state FROM qq_official_generation_capacity')[0]['state'],'released')
        asyncio.run(run())

    def test_account_bound_store_never_reads_or_tombstones_foreign_rows(self):
        foreign=self.put()
        other=AccountKey('qq-official','TEST_OTHER_FILES','test')
        other_blobs=ProjectBlobStore(self.private/'other-objects')
        store=ArtifactStore(transaction=self.tx,jobs=self.jobs,blobs=other_blobs,clock=lambda:self.now,account_key=other.key)
        self.assertIsNone(store.get(foreign.artifact_id))
        with self.assertRaisesRegex(ArtifactError,'unavailable'):
            store.read_verified(foreign.artifact_id,sha256=foreign.sha256,byte_size=foreign.byte_size)
        with self.assertRaisesRegex(LeaseLost,'account_mismatch'):
            store.register(self.source,content=PDF,filename='report.pdf',logical_request_key=KEY)
        blob=other_blobs.put(PDF)
        os.utime(other_blobs.path(blob.sha256),(self.now-90000,self.now-90000))
        self.assertEqual(store.collect_orphans(limit=50)[0],1)
        self.assertTrue(self.blobs.path(foreign.sha256).exists())
        self.now+=86401
        store.collect_expired()
        self.assertEqual(self.query('SELECT bytes_state FROM qq_official_file_artifacts')[0]['bytes_state'],'available')

    def test_stale_maintenance_cannot_tombstone_or_remove_bytes(self):
        self.build(); item=self.item()
        self.now+=86401; self.runtime._token=None
        with self.assertRaises(LeaseLost):asyncio.run(self.service.maintain())
        self.assertEqual(self.query('SELECT bytes_state FROM qq_official_file_artifacts')[0]['bytes_state'],'available')
        self.assertTrue(self.service.artifacts.blobs.path(item.sha256).exists())

    def test_maintenance_expires_bytes_but_does_not_refund_unknown_workspace(self):
        self.build(); item=self.item()
        async def run():
            await self.binding.reserve_capacity(self.binding.owner)
            self.now+=86401; self.manager.items=[{'sandbox_id':'s123abc'}]
            await self.service.maintain()
            row=self.query('SELECT bytes_state,cleanup_pending FROM qq_official_file_artifacts')[0]
            self.assertEqual((row['bytes_state'],row['cleanup_pending']),('deleted',False))
            self.assertFalse(self.service.artifacts.blobs.path(item.sha256).exists())
            self.assertEqual(self.query('SELECT state FROM qq_official_generation_capacity')[0]['state'],'reserved')
        asyncio.run(run())

    def test_credential_cleanup_rechecks_lease_after_async_publication_lookup(self):
        self.build()
        async def run():
            pub=await self.publish(); self.now=pub.expires_at+1
            store=self.service._publication_store.store
            original=store.by_credential
            async def lose_after(ref):
                row=await original(ref); self.runtime._token=None; return row
            with patch.object(store,'by_credential',lose_after):
                with self.assertRaises(LeaseLost):
                    await self.service._maintenance_publisher.collect_credentials(limit=50)
            self.assertIn(pub.credential_ref,self.service.link_vault.page(None,50))
            self.assertEqual(self.query('SELECT state FROM qq_official_file_publications')[0]['state'],'active')
        asyncio.run(run())

    def test_partial_constructor_failure_closes_private_native_vault(self):
        from src.plugins.ai_chat.adapters.qq_official.native_vault import NativeUploadVault
        made=[]
        def capture(*args,**kwargs):
            value=NativeUploadVault(*args,**kwargs); made.append(value); return value
        with patch('src.plugins.ai_chat.adapters.qq_official.file_services.NativeUploadVault',capture), \
                patch('src.plugins.ai_chat.adapters.qq_official.file_services.FileCredentialVault',
                      side_effect=ValueError('TEST_SETUP_FAILURE')):
            with self.assertRaisesRegex(ValueError,'TEST_SETUP_FAILURE'):
                FileServices(self.runtime,self.token,None,self.config)
        self.assertEqual(made[0]._fd,-1)

    def test_account_environments_use_distinct_private_roots_and_views(self):
        self.build(); item=self.item()
        account=replace(self.account,environment='sandbox')
        self.provision(replace(self.endpoint,account=account))
        token=self.lease(account).acquire()
        other_runtime=SimpleNamespace(**vars(self.runtime)); other_runtime._token=token
        other=FileServices(other_runtime,token,None,self.config)
        try:
            self.assertNotEqual(other.directory,self.service.directory)
            self.assertIsNone(other.artifacts.get(item.artifact_id))
            self.assertEqual(other.artifacts.physical_usage(),(0,0))
        finally:asyncio.run(other.close())

    def test_factory_cancel_waits_for_constructor_and_closes_result(self):
        started,release=threading.Event(),threading.Event()
        original=FileServices
        made=[]
        def blocked(*args):
            result=original(*args); made.append(result); started.set(); release.wait(10); return result
        async def run():
            with patch('src.plugins.ai_chat.adapters.qq_official.file_services.FileServices',blocked):
                task=asyncio.create_task(file_services_factory(self.config)(self.runtime,self.token,None))
                await asyncio.to_thread(started.wait,10)
                task.cancel(); release.set()
                with self.assertRaises(asyncio.CancelledError):await task
            self.assertTrue(made[0].closed)
            self.assertEqual(made[0].native_vault._fd,-1)
            self.assertEqual(made[0].link_vault._fd,-1)
        asyncio.run(run())

    def test_close_is_idempotent_and_denies_future_work(self):
        self.build()
        async def run():
            await self.service.close(); await self.service.close()
            with self.assertRaises(LeaseLost):await self.service.maintain()
            with self.assertRaises(LeaseLost):await self.service.readiness(self.binding)
        asyncio.run(run())

    def test_cancelled_close_drains_both_private_vaults(self):
        self.build(); started,release=threading.Event(),threading.Event()
        original=self.service.native_vault.close
        def blocked():
            started.set(); release.wait(10); original()
        async def run():
            with patch.object(self.service.native_vault,'close',blocked):
                task=asyncio.create_task(self.service.close())
                await asyncio.to_thread(started.wait,10)
                task.cancel(); release.set()
                with self.assertRaises(asyncio.CancelledError):await task
            await self.service.close()
            self.assertEqual(self.service.native_vault._fd,-1)
            self.assertEqual(self.service.link_vault._fd,-1)
        asyncio.run(run())

    def test_console_reads_without_transport_route_or_file_worker_grant(self):
        from src.plugins.ai_chat.adapters.qq_official.file_authority import assert_file_current
        self.build(route_authority=None,link_origin=None)
        item=self.item()
        turn=self.query('SELECT journal_turn_id FROM qq_official_turn_executions')[0]['journal_turn_id']
        self.result(self.handle,turn_id=turn)
        self.jobs.mark_succeeded(self.job.job_id,self.job.lease_owner,now=self.now,
            lease_generation=self.job.lease_generation)
        async def run():
            for state in ('stored','failed','upload_unknown','publication_unknown'):
                with self.subTest(state=state):
                    self.query('UPDATE qq_official_file_deliveries SET state=? WHERE artifact_id=?',(state,item.artifact_id))
                    async with self.service.console_artifact(item.artifact_id) as (ref,stream):
                        self.assertEqual(ref.artifact_id,item.artifact_id)
                        self.assertEqual(b''.join([block async for block in stream.chunks]),PDF)
                    with self.assertRaises(LeaseLost),self.tx() as cursor:
                        assert_file_current(cursor,token=self.token,artifact_id=item.artifact_id,
                            admission=self.admission,now=self.now)
                    self.assertEqual(self.query('SELECT state FROM qq_official_file_deliveries')[0]['state'],state)
            self.assertEqual(self.query('SELECT * FROM delivery_plans'),[])
            self.assertEqual(self.query('SELECT * FROM qq_official_file_publications'),[])
            self.assertEqual(self.query('SELECT * FROM qq_official_file_credentials'),[])
        asyncio.run(run())

    def test_console_unknown_handoff_works_without_transport_vault_or_route(self):
        from src.plugins.ai_chat.adapters.qq_official.file_authority import assert_file_current
        self.build()
        async def run():
            pub=await self.publish()
            self.query("UPDATE qq_official_file_deliveries SET state='delivery_unknown' WHERE artifact_id=?",(pub.artifact.artifact_id,))
            self.service.link_vault.delete(pub.credential_ref)
            self.service.config=replace(self.config,route_authority=None,link_origin=None)
            async with self.service.console_artifact(pub.artifact.artifact_id) as (ref,stream):
                self.assertEqual(b''.join([block async for block in stream.chunks]),PDF)
                self.assertEqual(ref,pub.artifact)
            row=self.query('SELECT state,active_plan_id FROM qq_official_file_deliveries')[0]
            self.assertEqual(row['state'],'delivery_unknown')
            with self.assertRaises(LeaseLost),self.tx() as cursor:
                assert_file_current(cursor,token=self.token,artifact_id=pub.artifact.artifact_id,
                    admission=self.admission,now=self.now,plan_id=row['active_plan_id'])
        asyncio.run(run())

    def test_console_rechecks_source_and_lease_for_every_chunk(self):
        self.build(route_authority=None,link_origin=None)
        item=self.item(b'%PDF-1.4\n'+b'x'*70000+b'\n%%EOF\n')
        async def run():
            async with self.service.console_artifact(item.artifact_id) as (_,stream):
                self.assertEqual(len(await anext(stream.chunks)),65536)
                self.query("UPDATE qq_official_turn_executions SET state='needs_attention' WHERE execution_id=?",(self.handle.execution_id,))
                with self.assertRaisesRegex(LinkError,'console_artifact_unavailable'):await anext(stream.chunks)
            self.query("UPDATE qq_official_turn_executions SET state='executing' WHERE execution_id=?",(self.handle.execution_id,))
            async with self.service.console_artifact(item.artifact_id) as (_,stream):
                await anext(stream.chunks); self.runtime._token=None
                with self.assertRaises(LinkError):await anext(stream.chunks)
        asyncio.run(run())

    def test_console_denies_revoked_route_cancelled_artifact_and_expired_bytes(self):
        self.build(route_authority=None,link_origin=None); item=self.item()
        async def rejected():
            with self.assertRaisesRegex(LinkError,'console_artifact_unavailable'):
                async with self.service.console_artifact(item.artifact_id):self.fail('unauthorized console bytes')
        for state in ('cancelled','expired'):
            self.query('UPDATE qq_official_file_deliveries SET state=? WHERE artifact_id=?',(state,item.artifact_id))
            asyncio.run(rejected())
        self.query("UPDATE qq_official_file_deliveries SET state='stored' WHERE artifact_id=?",(item.artifact_id,))
        self.admitted=False; asyncio.run(rejected()); self.admitted=True
        self.query('UPDATE qq_official_endpoints SET route_epoch=route_epoch+1 WHERE endpoint_key=?',(self.endpoint.key,))
        asyncio.run(rejected())
        self.query('UPDATE qq_official_endpoints SET route_epoch=route_epoch-1 WHERE endpoint_key=?',(self.endpoint.key,))
        self.now+=86401; asyncio.run(rejected())

    def test_console_rejects_other_account_and_detects_changed_bytes(self):
        self.build(route_authority=None,link_origin=None); item=self.item()
        other=AccountKey('qq-official','TEST_CONSOLE_OTHER','test')
        self.provision(replace(self.endpoint,account=other)); token=self.lease(other).acquire()
        runtime=SimpleNamespace(**vars(self.runtime)); runtime._token=token
        other_service=FileServices(runtime,token,None,self.config)
        async def run():
            try:
                with self.assertRaises(LinkError):
                    async with other_service.console_artifact(item.artifact_id):self.fail('cross account')
            finally:await other_service.close()
            path=self.service.artifacts.blobs.path(item.sha256)
            path.chmod(0o600); path.write_bytes(b'%PDF-corrupt')
            with self.assertRaisesRegex(LinkError,'console_artifact_unavailable'):
                async with self.service.console_artifact(item.artifact_id):self.fail('corrupt bytes')
        asyncio.run(run())

    def test_console_failed_producer_still_requires_explicit_safe_draft(self):
        from src.plugins.ai_chat.adapters.qq_official.execution import capture_result
        from src.plugins.ai_chat.message_ir import MessageBody, TextNode
        self.build(route_authority=None,link_origin=None)
        complete=self.item()
        draft=self.service.artifacts.register(self.source,content=PDF,filename='draft.pdf',
            logical_request_key=hashlib.sha256(b'TEST_CONSOLE_DRAFT').hexdigest(),
            draft=True,draft_reason='TEST independent validation incomplete')
        turn=self.query('SELECT journal_turn_id FROM qq_official_turn_executions')[0]['journal_turn_id']
        with self.tx() as cursor:
            capture_result(cursor,**self.args(self.handle),body=MessageBody((TextNode(0,'TEST failure'),)),
                turn_id=turn,status='crashed')
        async def run():
            with self.assertRaises(LinkError):
                async with self.service.console_artifact(complete.artifact_id):self.fail('non-draft failed producer')
            async with self.service.console_artifact(draft.artifact_id) as (ref,stream):
                self.assertTrue(ref.filename.startswith('草稿-'))
                self.assertEqual(b''.join([block async for block in stream.chunks]),PDF)
            self.query("UPDATE qq_official_turn_executions SET result_status='aborted' WHERE execution_id=?",(self.handle.execution_id,))
            with self.assertRaises(LinkError):
                async with self.service.console_artifact(draft.artifact_id):self.fail('aborted producer')
        asyncio.run(run())
