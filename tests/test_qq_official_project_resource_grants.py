"""Real PG + actual M5 review, owner grants and M3 receipt commits.

No copied test inheritance: reuse only the non-test service fixture/helpers.
The verification fixture records local synthetic platform receipts, no API.
"""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import secrets
from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official.project_authority import ProjectAuthority,ProjectRequest,ProjectGrantRef,Operation,Denied
from src.plugins.ai_chat.adapters.qq_official.project_commands import CommandProjectProvider
from src.plugins.ai_chat.adapters.qq_official.outbox_types import OfficialAttemptContext
from src.plugins.ai_chat.adapters.qq_official.migration_types import MigrationDenied
from src.plugins.ai_chat.platform_types import ActorKey,EndpointKey
from src.plugins.ai_chat.projects import ProjectStore,ProjectAccess
from src.plugins.ai_chat.message_ir import MediaNode,MessageBody
from tests.test_qq_official_migration_services import MigrationServicesFixture
from tests.qq_official_migration_project_fixture import install_project_owner
from tests import test_qq_official_receipts as receipts


class ProjectResourcePGTests(MigrationServicesFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp();install_project_owner(self)
        temp=TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.projects=ProjectStore(self.database,Path(temp.name));self.addCleanup(self.projects.close)
        principal=self.ledger.ensure_principal_identity('onebot-v11',self.source_actor.native_user_id,'TEST source owner')
        access=ProjectAccess(self.source_scope.with_actor(self.source_actor.native_user_id),principal,member=True)
        self.project=self.projects.create(access,'TEST reviewed resource',now=self.now)
        self.owner=self.actor;self.new_actor=ActorKey(self.account,'group_member',self.endpoint.native_conversation_id,'TEST_NEW_RECIPIENT')
        self.ledger.ensure_actor_identity(self.new_actor,'TEST recipient')
        staged=await self.service.console.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,
            action='project_grant_stage',request=dict(source_endpoint=self.source.key,source_scope=self.source_scope.key,
                source_actor=self.source_actor.key,target_endpoint=self.endpoint.key,target_actor=self.actor.key,
                issuer_actor=self.actor.key,project_id=self.project['project_id'],mapping_id=self.mapping_evidence.mapping_id,
                mapping_revision=self.mapping_evidence.revision,capabilities=['project.read','project.deliver'],expires_at=self.now+10))
        self.original=ProjectGrantRef(staged['grant_id'],staged['revision'])
        blob=self.projects.blobs.put(b'%PDF-1.7\nTEST_RESOURCE\n%%EOF\n')
        with self.tx() as c:
            self.projects._register_blob(c,blob,self.now)
            operation=c.execute('''INSERT INTO project_io
                (project_id,actor_principal_id,request_key,token,tool_name,arguments_sha256,sandbox_id,generation,fence,
                 mutating,reserved_bytes,state,result,evidence,created_at,updated_at)
                VALUES (?,?,?,?,'send_file_from_sandbox',?,'TEST_CAS',1,0,FALSE,0,'completed','{}','{}',?,?) RETURNING operation_id''',
                (self.project['project_id'],principal,'b'*64,'c'*32,'d'*64,self.now,self.now)).fetchone()[0]
            delivery,_=self.deliveries.enqueue(idempotency_key='project-file:'+str(operation),source_scope_key=self.source_scope.key,
                target_scope=self.source_scope,body=MessageBody((MediaNode(0,'file',name='origin.pdf',source_type='project_attachment'),)),
                transaction_cursor=c,now=self.now)
            c.execute('''INSERT INTO project_file_deliveries
                (delivery_id,operation_id,project_id,sha256,blob_sha256,byte_size,filename,path)
                VALUES (?,?,?,?,?,?,'origin.pdf','TEST_NOT_EXPORTED')''',
                (delivery.delivery_id,operation,self.project['project_id'],blob.sha256,blob.sha256,blob.byte_size))
        from src.plugins.ai_chat.adapters.qq_official.migration_services import ExportDeadline
        self.service.ports.exporter.deadlines[(self.source.key,'file','project:'+str(delivery.delivery_id))]=ExportDeadline(self.now+400,self.now+500)
        planned=await self.service.console.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,action='plan',
            request=dict(source_endpoint=self.source.key,source_scope=self.source_scope.key,target_endpoint=self.endpoint.key,
                items=[dict(kind='file',key='project:'+str(delivery.delivery_id))],deadline=self.now+500))
        self.plan=SimpleNamespace(**planned)
        self.ports.export=self.service.ports.export
        self.switch(self.plan)
        # Existing receipt fixture uses a version-1 endpoint. This fixture has
        # a real /bind confirm route increment; use the actual physical tuple.
        physical=self.query('SELECT * FROM qq_official_endpoints WHERE endpoint_key=?',(self.endpoint.key,))[0]
        original_window=self.window
        def window(**kwargs):
            identifier=original_window(**kwargs)
            self.query('UPDATE qq_official_reply_windows SET endpoint_generation=?,route_epoch=? WHERE window_id=?',
                (physical['generation'],physical['route_epoch'],identifier))
            return identifier
        def attempt_context(token,expected_attempt_count,fields):
            values=dict(fields);values.update(endpoint_generation=physical['generation'],route_epoch=physical['route_epoch'])
            return OfficialAttemptContext(token,expected_attempt_count,tuple(values.items()))
        with patch.object(self,'window',side_effect=window),patch.object(receipts,'OfficialAttemptContext',side_effect=attempt_context):
            self.complete(self.plan)
        self.origins=self.service.project_resource_origins
        self.host=object()
        def source_guard(c,*,request,source):
            project=c.execute('SELECT * FROM projects WHERE project_id=?',(request.project_id,)).fetchone()
            return source is self.host and project is not None and self.service.project_grants.project_scope_guard(
                c,request=request,project=project,source=source)
        self.authority=ProjectAuthority(clock=lambda:self.now,source_guard=source_guard,
            route_guard=self.service.project_grants._route,
            recipient_evidence=lambda c,**kw:kw['source'] is self.host and kw['actor'] in (self.owner,self.new_actor),
            fresh_actor_evidence=lambda *a,**kw:False)
        self.issuer=ProjectRequest(self.endpoint,self.owner,self.source_scope,2,physical['route_epoch'],self.project['project_id'],Operation.MANAGE)

    def grant(self,actor,*,prior=None):
        with self.tx() as c:
            def owner(c):self.authority._owner(c,self.issuer);return True
            origin=self.origins.capture(c,request=self.issuer,authorize=owner,now=self.now)
            def current(c,*,request,source):
                if source is not self.host:return False
                self.origins.check_origin(c,request=request,origin_id=origin.origin_id)
                return True
            authority=ProjectAuthority(clock=lambda:self.now,source_guard=current,route_guard=self.authority.route_guard,
                recipient_evidence=self.authority.recipient_evidence)
            result=authority.issue_project(c,issuer=self.issuer,recipient=actor,
                capabilities=frozenset({Operation.READ}),grant_id=prior.grant_id if prior else secrets.token_hex(16),
                expires_at=self.now+100,evidence_ref='message:1',source=self.host,
                expected_revision=prior.revision if prior else 0)
            self.origins.bind_grant(c,request=self.issuer,origin_id=origin.origin_id,grant=result,now=self.now)
            return result

    def require(self,actor,grant,*,request=None):
        request=request or replace(self.issuer,actor=actor,operation=Operation.READ)
        with self.tx() as c:return self.authority.require_project(c,request=request,grant=grant,source=self.host)

    async def test_new_actor_after_expired_stage_can_receive_explicit_current_grant(self):
        self.now+=11
        with self.assertRaises((Denied,MigrationDenied)):self.require(self.owner,self.original)
        issued=self.grant(self.new_actor)
        self.assertEqual(self.require(self.new_actor,issued).grant,issued)
        self.assertEqual(len(self.query('SELECT * FROM qq_official_project_resource_origins')),1)
        with self.assertRaises((Denied,MigrationDenied)):self.require(self.owner,self.original)
        with self.assertRaises((Denied,MigrationDenied)):self.require(self.owner,issued)
        with self.tx() as c,self.assertRaises((Denied,MigrationDenied)):
            self.authority.require_project(c,request=replace(self.issuer,actor=self.new_actor,operation=Operation.WRITE),grant=issued,source=self.host)

    async def test_same_actor_explicit_revision_renewal_does_not_revive_old_refs(self):
        old=dict(self.query('SELECT * FROM migration_project_grants')[0]);self.now+=11
        issued=self.grant(self.owner,prior=self.original)
        self.assertEqual(issued.revision,2);self.assertEqual(self.require(self.owner,issued).grant,issued)
        self.assertEqual(dict(self.query('SELECT * FROM migration_project_grants')[0]),old)
        with self.assertRaises((Denied,MigrationDenied)):self.require(self.owner,self.original)
        with self.assertRaises((Denied,MigrationDenied)):self.grant(self.owner,prior=self.original)
        self.assertEqual(self.query('SELECT revision FROM qq_official_project_grants WHERE grant_id=?',(issued.grant_id,))[0][0],2)

    async def test_expired_mapping_is_not_reused_as_identity_and_origin_is_immutable(self):
        self.now+=1001
        with self.tx() as c,self.assertRaises(MigrationDenied):
            self.service.mappings.lookup(c,self.source,self.source_scope,self.source_actor,self.endpoint,self.now)
        issued=self.grant(self.new_actor);self.assertEqual(self.require(self.new_actor,issued).grant,issued)
        with self.tx() as c,self.assertRaises(Exception):c.execute('DELETE FROM qq_official_project_resource_origins')
        with self.tx() as c,self.assertRaises(Exception):c.execute('UPDATE qq_official_project_resource_origins SET logical_route_epoch=3')

    async def test_cross_project_and_group_alias_do_not_authorize_unreviewed_resource(self):
        issued=self.grant(self.new_actor)
        principal=self.ledger.ensure_principal_identity('onebot-v11',self.source_actor.native_user_id,'TEST owner')
        other=self.projects.create(ProjectAccess(self.source_scope.with_actor(self.source_actor.native_user_id),principal,member=True),
            'TEST not included',now=self.now)
        with self.assertRaises((Denied,MigrationDenied)):
            self.require(self.new_actor,issued,request=replace(self.issuer,actor=self.new_actor,project_id=other['project_id'],operation=Operation.READ))
        endpoint=EndpointKey(self.account,'group','TEST_OTHER_GROUP');self.provision(endpoint)
        actor=ActorKey(self.account,'group_member',endpoint.native_conversation_id,self.new_actor.native_user_id)
        with self.assertRaises((Denied,MigrationDenied)):
            self.require(actor,issued,request=replace(self.issuer,endpoint=endpoint,actor=actor,operation=Operation.READ))

    async def test_rollback_and_project_deletion_revoke_fact_use_without_deleting_audit(self):
        issued=self.grant(self.new_actor)
        self.query("UPDATE projects SET status='purged' WHERE project_id=?",(self.project['project_id'],))
        with self.assertRaises((Denied,MigrationDenied)):self.require(self.new_actor,issued)
        self.query("UPDATE projects SET status='stopped' WHERE project_id=?",(self.project['project_id'],))
        with self.tx() as c:self.migration.rollback(c,migration_id=self.plan.migration_id,authorization=self.authorization,now=self.now)
        with self.assertRaises((Denied,MigrationDenied)):self.require(self.new_actor,issued)
        self.assertEqual(len(self.query('SELECT * FROM qq_official_project_resource_origins')),1)

    async def test_actual_command_source_new_recipient_and_single_m3_loopback_reply(self):
        from datetime import datetime,timezone
        from unittest.mock import AsyncMock
        from src.plugins.ai_chat.adapters.qq_official import execution
        from src.plugins.ai_chat.adapters.qq_official.ingest import process_inbox
        from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
        from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
        from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
        from src.plugins.ai_chat.adapters.qq_official.project_commands import ProjectCommands
        from src.plugins.ai_chat.adapters.qq_official.turn_consumer import OfficialTurnConsumer
        from src.plugins.ai_chat.adapters.qq_official.turn_platform import TransportState
        from src.plugins.ai_chat.adapters.qq_official.sender import OfficialSender
        from src.plugins.ai_chat.application.commands import CommandServices
        from src.plugins.ai_chat.tools.state import ToolContext
        from src.qq_official.testing.fake_platform import FakePlatform
        from src.qq_official.credentials import Credentials
        from src.qq_official.token import TokenManager
        from src.qq_official.http import RestClient
        self.now+=11
        def inbound(actor,text,*,trigger):
            self.sequence+=1;native='TEST_M7_COMMAND_'+str(self.sequence)
            observed=self.store.accept({'op':0,'t':'GROUP_AT_MESSAGE_CREATE' if trigger else 'GROUP_MESSAGE_CREATE',
                's':self.sequence,'d':dict(id=native,group_openid=self.endpoint.native_conversation_id,
                    author=dict(member_openid=actor.native_user_id),content=text,
                    timestamp=datetime.fromtimestamp(self.now,timezone.utc).isoformat())},
                token=self.token,session_id='TEST_ASSEMBLY')
            with self.tx() as c:
                result=process_inbox(c,inbox_id=observed.inbox_id,lease_token=self.token,ledger=self.ledger,
                    jobs=self.jobs,epoch_guard=assert_epoch,admission=self.store.admitted,now=self.now)
            return result,native
        recipient,_=inbound(self.new_actor,'TEST recipient identity evidence',trigger=False)
        command,native=inbound(self.owner,f'/项目权限 授予 prj#{self.project["project_id"]} msg#{recipient.canonical_message_id} read 100',trigger=True)
        with self.tx() as c:
            observe_ready(c,self.token,{'op':0,'t':'READY','d':{'session_id':'TEST_ASSEMBLY',
                'user':{'id':'TEST_ACTUAL_BOT','bot':True}}},self.now)
        jobs=self.jobs.claim_due('TEST_COMMAND_WORKER',kinds=(account_job_kind('turn',self.account.key),),now=self.now)
        self.assertEqual(len(jobs),1);job=jobs[0]
        agent=AsyncMock(side_effect=AssertionError('commands must not invoke model'))
        providers=[]
        def factory(life,actual,platform,base):
            provider=CommandProjectProvider(token=self.token,handle=life.handle,job=life.job,
                admission=self.store.admitted,route_epoch=2,route_guard=self.service.project_grants._route,
                project_scope_guard=self.service.project_grants.project_scope_guard,
                resource_origins=self.origins,clock=lambda:self.now)
            providers.append(provider);platform.project_provider=provider
            context=ToolContext(SimpleNamespace(context=object()),event=None,capabilities=frozenset({'projects'}),
                inbound=actual,platform=platform)
            handler=ProjectCommands(inbound=actual,provider=provider,context=context,
                effect_scope=life.effect_scope,database_call=self.calls)
            return replace(base,project_grant=handler)
        consumer=OfficialTurnConsumer(api=execution,token=self.token,store=self.store,outbox=self.outbox,journal=self.journal,
            database_calls=self.calls,orchestrator=agent,command_services=CommandServices(),business_services=factory,
            transport_state=lambda:TransportState(self.account,True,True),clock=lambda:self.now)
        result=await consumer(job)
        self.assertEqual(result,{'status':'handed_off'});agent.assert_not_awaited()
        self.assertEqual(len(providers),1)
        rows=self.query('SELECT * FROM qq_official_project_grants WHERE actor_key=?',(self.new_actor.key,))
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['evidence_ref'],'message:'+str(recipient.canonical_message_id))
        self.assertEqual(rows[0]['capabilities'],['project.read'])
        self.assertEqual(self.query('SELECT * FROM agent_turns'),[])
        plan=self.query('''SELECT p.* FROM delivery_plans p JOIN qq_official_turn_executions x ON x.plan_id=p.plan_id
            WHERE x.canonical_message_id=?''',(command.canonical_message_id,))[0]
        self.assertEqual(plan['requester_actor_key'],self.owner.key)
        fake=FakePlatform(clock=lambda:self.now);fake.app_id=self.account.app_id;await fake.start()
        tokens=TokenManager(Credentials(self.account.app_id,fake.app_secret),fake.base_url,allow_test_server=True)
        rest=RestClient(tokens,allow_test_server=True)
        async def current():
            def run():
                with self.tx() as c:assert_epoch(c,self.token)
            await self.calls(run)
        sender=OfficialSender(self.account,self.token,self.outbox,rest,self.ledger,database_calls=self.calls,
            ensure_current=current,wall_clock=lambda:self.now)
        try:
            fake.add_window('group',self.endpoint.native_conversation_id,native)
            delivery=self.query('SELECT delivery_id FROM deliveries WHERE plan_id=?',(plan['plan_id'],))[0][0]
            self.assertEqual((await sender.send_delivery(delivery)).status,'committed')
            self.assertEqual((await consumer(job))['status'],'handed_off')
            self.assertEqual((await sender.send_delivery(delivery)).status,'committed')
            self.assertEqual(len(fake.received),1)
            self.assertEqual(self.query('SELECT revision FROM qq_official_project_grants WHERE actor_key=?',(self.new_actor.key,))[0][0],1)
        finally:
            await sender.stop();await rest.aclose();await tokens.aclose();await fake.close()

    async def test_new_origin_never_bypasses_revocation_of_old_mapping_or_old_grant(self):
        issued=self.grant(self.new_actor)
        # Original staged grant has not expired, and originally passed M5.
        self.require(self.owner,self.original)
        await self.service.console.mutate(cookie=self.mapping_cookie,csrf=self.mapping_csrf,action='mapping_revoke',
            request={'mapping_id':self.mapping_evidence.mapping_id,'expected_revision':self.mapping_evidence.revision})
        with self.assertRaises((Denied,MigrationDenied)):self.require(self.owner,self.original)
        self.assertEqual(self.require(self.new_actor,issued).grant,issued)
        # Revoke the new grant through the real authority, keeping origin and
        # old grant-revision association for audit. Neither is permission.
        with self.tx() as c:
            origin=self.query('SELECT origin_id FROM qq_official_project_resource_origins')[0][0]
            authority=ProjectAuthority(clock=lambda:self.now,route_guard=self.authority.route_guard,
                source_guard=lambda cur,**kw:kw['source'] is self.host and bool(self.origins.check_origin(cur,request=kw['request'],origin_id=origin)))
            revoked=authority.revoke_project(c,issuer=self.issuer,grant=issued,source=self.host)
        with self.assertRaises((Denied,MigrationDenied)):self.require(self.new_actor,issued)
        with self.assertRaises((Denied,MigrationDenied)):self.require(self.new_actor,revoked)
        self.assertEqual(self.query('SELECT count(*) FROM qq_official_project_resource_grants')[0][0],1)

    async def test_origin_grant_association_sql_rejects_staged_stale_revision_and_wrong_endpoint(self):
        issued=self.grant(self.new_actor)
        origin=self.query('SELECT origin_id FROM qq_official_project_resource_origins')[0][0]
        for grant,revision in ((self.original.grant_id,self.original.revision),(issued.grant_id,issued.revision+1)):
            with self.subTest(revision=revision),self.tx() as c,self.assertRaises(Exception):
                c.execute('INSERT INTO qq_official_project_resource_grants VALUES (?,?,?,?,?)',
                    (grant,revision,origin,'a'*64,self.now))
        with self.tx() as c,self.assertRaises(Exception):
            c.execute('DELETE FROM qq_official_project_resource_grants')
        with self.tx() as c,self.assertRaises(Exception):
            c.execute('UPDATE qq_official_project_resource_grants SET grant_revision=2')
        # An insert of a current owner-command grant for another actual group
        # cannot bind this origin even with a known origin identifier.
        other=EndpointKey(self.account,'group','TEST_SQL_OTHER');self.provision(other)
        grant_id=secrets.token_hex(16)
        with self.tx() as c:
            row=c.execute('SELECT * FROM qq_official_project_grants WHERE grant_id=?',(issued.grant_id,)).fetchone()
            fields=('project_id','account_key','actor_key','principal_id','project_scope_key','endpoint_generation','route_epoch',
                'endpoint_route_epoch','issuer_actor_key','owner_grant_id','owner_revision','issuer_link_revision','capabilities',
                'evidence_kind','evidence_ref','revision','state','issued_at','expires_at','updated_at')
            c.execute('INSERT INTO qq_official_project_grants (grant_id,endpoint_key,'+','.join(fields)+') VALUES ('+
                ','.join('?' for _ in range(len(fields)+2))+')',(grant_id,other.key,*(row[key] for key in fields)))
        with self.tx() as c,self.assertRaises(Exception):
            c.execute('INSERT INTO qq_official_project_resource_grants VALUES (?,?,?,?,?)',(grant_id,1,origin,'a'*64,self.now))
