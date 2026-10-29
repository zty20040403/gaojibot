from pathlib import Path
from tempfile import TemporaryDirectory
import asyncio
import json
import threading
from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official.migration_preview import Selection, MappingEvidence
from src.plugins.ai_chat.adapters.qq_official.migration_types import MigrationDenied
from src.plugins.ai_chat.adapters.qq_official.project_authority import ProjectAuthority, ProjectRequest, ProjectGrantRef, Operation
from src.plugins.ai_chat.adapters.qq_official.authority import OwnerAuthority
from src.plugins.ai_chat.projects import ProjectStore, ProjectAccess
from tests.qq_official_migration_project_fixture import install_project_owner
from tests.test_qq_official_migration_services import MigrationServicesFixture


class ProjectMigrationGrantTests(MigrationServicesFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        install_project_owner(self)
        temporary=TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.projects=ProjectStore(self.database,Path(temporary.name));self.addCleanup(self.projects.close)
        principal=self.ledger.ensure_principal_identity('onebot-v11',self.source_actor.native_user_id,'TEST old project owner')
        access=ProjectAccess(self.source_scope.with_actor(self.source_actor.native_user_id),principal,member=True)
        self.project=self.projects.create(access,'TEST original project',now=self.now)
        self.control=self.service.project_grants
        self.request=dict(source_endpoint=self.source.key,source_scope=self.source_scope.key,
            source_actor=self.source_actor.key,target_endpoint=self.endpoint.key,target_actor=self.actor.key,
            issuer_actor=self.actor.key,project_id=self.project['project_id'],mapping_id=self.mapping_evidence.mapping_id,
            mapping_revision=self.mapping_evidence.revision,capabilities=['project.read','project.execute','project.deliver'],
            expires_at=self.now+600)

    async def stage(self,**changes):
        return await self.service.console.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,
            action='project_grant_stage',request=self.request|changes)

    def grant_preview(self,*,readonly=False):
        mapping=MappingEvidence(self.mapping_evidence.mapping_id,self.mapping_evidence.revision,
            self.source_actor,self.endpoint,self.actor)
        with self.tx() as cursor:
            if readonly:cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            return self.control.preview(cursor,Selection(self.source,self.source_scope,self.endpoint),None,
                mapping,self.project['project_id'],'deliver')

    async def test_actual_console_stage_is_previewable_but_not_execution_authority(self):
        result=await self.stage()
        self.assertEqual(result['state'],'pending_migration')
        self.assertEqual(self.grant_preview(readonly=True),ProjectGrantRef(result['grant_id'],1))
        row=self.query('SELECT * FROM qq_official_project_grants WHERE grant_id=?',(result['grant_id'],))[0]
        self.assertEqual(row['route_epoch'],2)
        self.assertEqual(self.query('SELECT * FROM migration_project_grants')[0]['migration_id'],None)
        authority=ProjectAuthority(clock=lambda:self.now,route_guard=self.control._route,source_guard=lambda *a,**kw:True)
        request=ProjectRequest(self.endpoint,self.actor,self.source_scope,2,row['endpoint_route_epoch'],
            self.project['project_id'],Operation.EXECUTE)
        with self.tx() as cursor,self.assertRaises((PermissionError,MigrationDenied)):
            authority.require_project(cursor,request=request,grant=ProjectGrantRef(result['grant_id'],1),source=object())
        with self.assertRaises(MigrationDenied):await self.stage()

    async def test_wrong_csrf_wrong_origin_mapping_or_extended_deadline_cannot_stage(self):
        with self.assertRaises(MigrationDenied):
            await self.service.console.mutate(cookie=self.mapping_cookie,csrf='TEST_WRONG',
                action='project_grant_stage',request=self.request)
        for change in ({'mapping_revision':self.mapping_evidence.revision+1},{'project_id':self.project['project_id']+1},
                       {'expires_at':self.now+86401},{'capabilities':['project.execute','project.execute']}):
            with self.subTest(change=change),self.assertRaises((MigrationDenied,PermissionError)):
                await self.stage(**change)
        self.assertEqual(self.query('SELECT * FROM migration_project_grants'),[])

    async def test_exact_revision_revoke_and_mapping_change_invalidate_pending_grant(self):
        result=await self.stage()
        with self.assertRaises(MigrationDenied):
            await self.service.console.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,
                action='project_grant_revoke',request={'grant_id':result['grant_id'],'expected_revision':2})
        revoked=await self.service.console.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,
            action='project_grant_revoke',request={'grant_id':result['grant_id'],'expected_revision':1})
        self.assertEqual(revoked['revision'],2)
        with self.assertRaises(MigrationDenied):self.grant_preview(readonly=True)

    async def test_owner_revocation_and_original_expiry_cannot_be_renewed_by_preview(self):
        await self.stage()
        self.now+=600
        with self.assertRaises(MigrationDenied):self.grant_preview(readonly=True)
        self.now-=600
        with self.tx() as cursor:
            OwnerAuthority(clock=lambda:self.now).apply_trusted_config(cursor,self.account,(),'TEST_EXPLICIT_REVOKE')
        with self.assertRaises(MigrationDenied):self.grant_preview(readonly=True)

    async def revoke(self,result):
        return await self.service.console.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,
            action='project_grant_revoke',request={'grant_id':result['grant_id'],'expected_revision':result['revision']})

    async def test_revoked_reissue_is_explicit_cas_and_keeps_immutable_previous_issuance(self):
        first=await self.stage()
        snapshot=dict(self.query('SELECT * FROM migration_project_grants')[0])
        revoked=await self.revoke(first)
        for expected in (0,1,True,2.0):
            with self.subTest(expected=expected),self.assertRaises(MigrationDenied):
                await self.stage(expected_revision=expected)
        renewed=await self.stage(expected_revision=revoked['revision'],capabilities=['project.deliver'])
        self.assertEqual((renewed['grant_id'],renewed['revision']),(first['grant_id'],3))
        self.assertEqual(dict(self.query('SELECT * FROM migration_project_grants WHERE grant_revision=1')[0]),snapshot)
        self.assertEqual(json.loads(snapshot['grant_snapshot_json'])['capabilities'],sorted(self.request['capabilities']))
        self.assertEqual(self.grant_preview(readonly=True),ProjectGrantRef(first['grant_id'],3))
        with self.tx() as c,self.assertRaises(MigrationDenied):
            self.control._load(c,ProjectGrantRef(first['grant_id'],1),self.now)
        with self.tx() as c,self.assertRaises(Exception):
            c.execute("UPDATE migration_project_grants SET grant_snapshot_json='{}' WHERE grant_revision=1")
        with self.tx() as c,self.assertRaises(Exception):
            c.execute('DELETE FROM migration_project_grants WHERE grant_revision=1')

    async def test_expired_reissue_does_not_extend_old_snapshot_or_accept_concurrent_stale_cas(self):
        first=await self.stage(expires_at=self.now+5)
        with self.assertRaises(MigrationDenied):await self.stage(expected_revision=1)
        self.now+=5
        attempts=await asyncio.gather(self.stage(expected_revision=1),self.stage(expected_revision=1),return_exceptions=True)
        self.assertEqual(sum(isinstance(result,dict) for result in attempts),1)
        self.assertEqual(sum(isinstance(result,MigrationDenied) for result in attempts),1)
        old=self.query('SELECT grant_snapshot_json FROM migration_project_grants WHERE grant_revision=1')[0][0]
        self.assertEqual(json.loads(old)['expires_at'],self.now)
        self.assertEqual(self.grant_preview(readonly=True),ProjectGrantRef(first['grant_id'],2))
        with self.tx() as c,self.assertRaises(MigrationDenied):
            self.control._load(c,ProjectGrantRef(first['grant_id'],1),self.now,readonly=True)

    async def test_reissue_requires_current_owner_binding_and_exact_signed_request(self):
        first=await self.stage()
        await self.revoke(first)
        request=self.request|{'expected_revision':2}
        issued=self.control.control.issue(cookie=self.mapping_cookie,csrf=self.mapping_csrf,
            action='project_grant_stage',request=request)
        with self.assertRaises(MigrationDenied):
            self.control._mutate(issued,request|{'capabilities':['project.manage']})
        self.query("UPDATE qq_official_admin_bindings SET state='revoked',revision=revision+1,revoked_at=?",(self.now,))
        with self.assertRaises(PermissionError):await self.stage(expected_revision=2)
        self.assertEqual(self.query('SELECT count(*) FROM migration_project_grants')[0][0],1)

    def project_file_source(self):
        from src.plugins.ai_chat.message_ir import MediaNode,MessageBody
        blob=self.projects.blobs.put(b'%PDF-1.7\nTEST grant revision source\n%%EOF\n')
        principal=self.ledger.ensure_principal_identity('onebot-v11',self.source_actor.native_user_id,'TEST source')
        with self.tx() as c:
            self.projects._register_blob(c,blob,self.now)
            operation=c.execute('''INSERT INTO project_io
                (project_id,actor_principal_id,request_key,token,tool_name,arguments_sha256,sandbox_id,generation,fence,
                 mutating,reserved_bytes,state,result,evidence,created_at,updated_at)
                VALUES (?,?,?,?,'send_file_from_sandbox',?,'TEST_CAPTURED_GUEST',1,0,FALSE,0,'completed','{}','{}',?,?)
                RETURNING operation_id''',(self.project['project_id'],principal,'b'*64,'c'*32,'d'*64,self.now,self.now)).fetchone()[0]
            delivery,_=self.deliveries.enqueue(idempotency_key='project-file:'+str(operation),
                source_scope_key=self.source_scope.key,target_scope=self.source_scope,
                body=MessageBody((MediaNode(0,'file',name='original.pdf',source_type='project_attachment'),)),
                transaction_cursor=c,now=self.now)
            c.execute('''INSERT INTO project_file_deliveries
                (delivery_id,operation_id,project_id,sha256,blob_sha256,byte_size,filename,path)
                VALUES (?,?,?,?,?,?,'original.pdf','TEST_PATH_NOT_EXPORTED')''',
                (delivery.delivery_id,operation,self.project['project_id'],blob.sha256,blob.sha256,blob.byte_size))
        return {'kind':'file','key':'project:'+str(delivery.delivery_id)}

    async def test_real_plan_refresh_binds_only_new_revision_and_old_review_stays_stale(self):
        first=await self.stage()
        source=self.project_file_source()
        plan=await self.service.console.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,action='plan',
            request=dict(source_endpoint=self.source.key,source_scope=self.source_scope.key,target_endpoint=self.endpoint.key,
                items=[source],deadline=self.now+500))
        original=dict(self.query('SELECT * FROM migration_project_grants')[0])
        self.assertEqual(original['migration_id'],plan['migration_id'])
        await self.revoke(first)
        second=await self.stage(expected_revision=2)
        self.assertIsNone(self.query('SELECT migration_id FROM migration_project_grants WHERE grant_revision=3')[0][0])
        refreshed=await self.service.console.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,action='refresh',
            request={'migration_id':plan['migration_id']},preview_digest=plan['preview_digest'])
        self.assertNotEqual(refreshed['preview_digest'],plan['preview_digest'])
        self.assertEqual(dict(self.query('SELECT * FROM migration_project_grants WHERE grant_revision=1')[0]),original)
        self.assertEqual(self.query('SELECT migration_id FROM migration_project_grants WHERE grant_revision=3')[0][0],plan['migration_id'])
        exported=json.loads(self.query('SELECT export_json FROM migration_items')[0][0])
        self.assertEqual(exported['mapping']['project_grant'],{'grant_id':second['grant_id'],'revision':3})
        with self.assertRaises(MigrationDenied):
            await self.service.console.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,action='freeze',
                request={'migration_id':plan['migration_id']},preview_digest=plan['preview_digest'])
        await self.service.console.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,action='freeze',
            request={'migration_id':plan['migration_id']},preview_digest=refreshed['preview_digest'])
        await self.revoke(second)
        with self.assertRaises(MigrationDenied):await self.stage(expected_revision=4)

    async def test_current_grant_tampering_is_not_trusted_as_frozen_issuance(self):
        result=await self.stage(capabilities=['project.deliver'])
        self.query('UPDATE qq_official_project_grants SET capabilities=? WHERE grant_id=?',
            (['project.deliver','project.execute'],result['grant_id']))
        with self.assertRaises(MigrationDenied):self.grant_preview(readonly=True)

    async def test_console_plan_and_real_security_revoke_have_no_reverse_lock_order(self):
        await self.stage()
        source=self.project_file_source()
        request=dict(source_endpoint=self.source.key,source_scope=self.source_scope.key,target_endpoint=self.endpoint.key,
            items=[source],deadline=self.now+500)
        role=threading.local()
        admin_read,security_attempted,security_entered=(threading.Event() for _ in range(3))
        original_current=self.control.control._current
        original_lock=self.mapping_security._security_lock

        def current(cursor,*args):
            cursor.execute("SET LOCAL lock_timeout='2s'")
            value=original_current(cursor,*args)
            if getattr(role,'value',None)=='console' and not admin_read.is_set():
                admin_read.set()
                self.assertTrue(security_attempted.wait(2))
                # In the old order the other transaction held the global lock
                # here and then waited for our admin SHARE lock. In the fixed
                # order it waits for our global lock until the plan commits.
                security_entered.wait(.2)
            return value

        def security_lock(cursor):
            if getattr(role,'value',None)=='security':
                cursor.execute("SET LOCAL lock_timeout='2s'")
                security_attempted.set()
            original_lock(cursor)
            if getattr(role,'value',None)=='security':security_entered.set()

        def plan():
            role.value='console'
            return self.service.console._mutate(self.mapping_cookie,self.mapping_csrf,'plan',request,'')

        def revoke_session():
            role.value='security'
            self.assertTrue(admin_read.wait(10))
            with self.mapping_security.transaction() as c:
                c.execute('UPDATE admin_accounts SET version=version+1 WHERE account_id=?',('admin',))
            return 'revoked'

        with patch.object(self.control.control,'_current',side_effect=current),\
                patch.object(self.mapping_security,'_security_lock',side_effect=security_lock):
            result=await asyncio.gather(asyncio.to_thread(plan),asyncio.to_thread(revoke_session),return_exceptions=True)
        self.assertIsInstance(result[0],dict,result)
        self.assertEqual(result[1],'revoked',result)
        self.assertEqual(self.query('SELECT count(*) FROM migration_journals')[0][0],1)
        with self.assertRaises(MigrationDenied):self.grant_preview(readonly=True)

    async def test_http_reissue_requires_cookie_csrf_and_strict_expected_revision(self):
        import httpx
        import nonebot
        from fastapi import FastAPI
        from types import SimpleNamespace
        from src.bot_security.service import MobileAuthorization
        from src.plugins.ai_chat.admin_security import SESSION_COOKIE
        nonebot.init()
        from src.plugins.ai_chat.admin import AdminServices,register_admin
        app=FastAPI()
        register_admin(app,AdminServices(version='test',started_at=1,database=self.database,
            settings=SimpleNamespace(admin_origin='https://test'),
            mobile_authorization=MobileAuthorization(self.mapping_security,sender=None,bot_selector=lambda:None),
            official_migration_service=lambda:self.service.console))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://test',
                headers={'Origin':'https://test','x-csrf-token':self.mapping_csrf}) as client:
            client.cookies.set(SESSION_COOKIE,self.mapping_cookie)
            path='/bot-admin/api/v1/qq-official/migrations/project-grants'
            first=await client.post(path,json=self.request)
            self.assertEqual(first.status_code,200,first.text)
            revoked=await client.post(path+'/'+first.json()['grant_id']+'/revoke',json={'expected_revision':1})
            self.assertEqual(revoked.status_code,200,revoked.text)
            request=self.request|{'expected_revision':2}
            self.assertEqual((await client.post(path,json=request,headers={'x-csrf-token':'TEST_BAD'})).status_code,403)
            self.assertEqual((await client.post(path,json=request|{'expected_revision':True})).status_code,422)
            self.assertEqual((await client.post(path,json=request|{'expected_revision':1})).status_code,409)
            renewed=await client.post(path,json=request)
            self.assertEqual(renewed.status_code,200,renewed.text)
            self.assertEqual(renewed.json()['revision'],3)
            self.assertEqual((await client.post(path,json=request)).status_code,409)
