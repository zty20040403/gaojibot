from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.plugins.ai_chat.application.commands import CommandServices, ParsedCommand, parse_command, dispatch_command
from src.plugins.ai_chat.application.routine_commands import command_result as routine_result
from src.plugins.ai_chat.application.project_commands import command_result as project_result
from src.plugins.ai_chat.application.project_grant_commands import parse_grant, command_result as grant_result
from src.plugins.ai_chat.application.memory_commands import parse_memory, MemorySyntax
from src.plugins.ai_chat.application.wiki_commands import format_page
from src.plugins.ai_chat.adapters.qq_official.memory_commands import MemoryCommands
from src.plugins.ai_chat.adapters.qq_official.command_fence import CommandEffectDenied
from src.plugins.ai_chat.adapters.qq_official.project_authority import Denied
from src.plugins.ai_chat.project_authorization import ForegroundProjectProvider
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey, InboundMessage
from src.plugins.ai_chat.message_ir import MessageBody
from src.plugins.ai_chat.long_term_memory import LongTermMemoryStore
from src.plugins.ai_chat.tools.context import _official_semantic_payload
from src.plugins.ai_chat.tools.state import ToolContext


def message(app='TEST_APP',environment='test',group='TEST_GROUP',user='TEST_USER'):
    account=AccountKey('qq-official',app,environment)
    endpoint=EndpointKey(account,'group' if group else 'private',group or user)
    actor=ActorKey(account,'group_member' if group else 'c2c_user',group or '',user)
    return InboundMessage(endpoint,actor,'TEST_EVENT','TEST_NATIVE','Tester','owner',MessageBody(),True,None,100,None)


class ParserTests(unittest.TestCase):
    def test_aliases_exact_and_bounded(self):
        for alias,name in [('例行','routine'),('群百科','wiki'),('memory','memory'),('记忆','memory'),('项目','project'),('项目权限','project_grant')]:
            with self.subTest(alias=alias): self.assertEqual(parse_command('/'+alias).name,name)
        self.assertIsNone(parse_command('说明中的 /项目'))
        self.assertIsNone(parse_command('/项目evil'))
        self.assertFalse(parse_command('/memory '+'x'*4097).valid)

    def test_memory_malformed_clear_never_means_personal_clear(self):
        for value in ('清空 group-other','清空 all extra','remove 0','remove -1','remove True','remove 9223372036854775808','list extra'):
            with self.subTest(value=value),self.assertRaises(MemorySyntax):parse_memory(value)

    def test_memory_identity_is_not_parser_argument(self):
        value=parse_memory('添加 喜欢绿色')
        self.assertEqual((value.action,value.scope,value.content),('add','user','喜欢绿色'))
        self.assertNotIn('喜欢绿色',repr(value))
        self.assertEqual(parse_memory('清空').scope,'user')
        self.assertEqual(parse_memory('群：共同约定').scope,'group')

    def test_grant_caps_and_deadline_are_explicit(self):
        parsed=parse_grant('授予 prj#7 msg#42 read,deliver 3600')
        self.assertEqual((parsed.project_id,parsed.message_id,parsed.ttl_seconds),(7,42,3600))
        self.assertEqual(parsed.capabilities,frozenset({'project.read','project.deliver'}))
        for value in ('授予 prj#7 TEST_OPENID read 3600','授予 prj#7 mention#42 read 3600',
                      '授予 prj#7 msg#42 all 3600','授予 prj#7 msg#42 read 86401',
                      '授予 prj#7 msg#42 read,read 60','授予 prj#7 msg#42 read True'):
            with self.subTest(value=value),self.assertRaises(ValueError):parse_grant(value)

    def test_grant_revision_cannot_default_or_coerce(self):
        ref='grant#'+'a'*32
        parsed=parse_grant(f'撤销 prj#7 {ref} v3')
        self.assertEqual(parsed.expected_revision,3)
        for value in (f'撤销 prj#7 {ref}',f'撤销 prj#7 {ref} v0',f'更新 prj#7 {ref} v1 msg#2 read'):
            with self.subTest(value=value),self.assertRaises(ValueError):parse_grant(value)

    def test_wiki_preserves_evidence_and_output_bound(self):
        items=[dict(category='decision',title='Test',content='x'*1500,evidence_ids=[2,3]) for _ in range(12)]
        result=format_page(dict(revision=2,items=items))
        self.assertIn('msg#2, msg#3',result);self.assertIn('显示前 3 条',result);self.assertLess(len(result),5200)


class FormatterTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_business_services_are_unavailable(self):
        for command in ('/例行','/群百科','/memory','/项目','/项目权限'):
            result=await dispatch_command(parse_command(command),message(),services=CommandServices())
            self.assertEqual(result.status,'unavailable')

    async def test_routine_list_and_versioned_cancel(self):
        calls=[]
        async def call(name,args):
            calls.append((name,args))
            return {'ok':True,'routines':[{'handle':'routine#1','version':4,'name':'晨报','enabled':True,'cron':'0 9 * * *','timezone':'Asia/Shanghai','next_due_at':999}]}
        self.assertIn('routine#1 v4',await routine_result('列表',call))
        self.assertEqual(await routine_result('取消 routine#1 v4',call),'已取消 routine#1。')
        self.assertEqual(calls[-1],('routine_cancel',{'routine_handle':'routine#1','expected_version':4}))
        count=len(calls);await routine_result('取消 routine#1',call);self.assertEqual(len(calls),count)

    async def test_project_result_pending_is_not_claimed_saved(self):
        calls=[]
        async def call(name,args):
            calls.append((name,args))
            return {'ok':True,'project':{'handle':'prj#1','title':'A','status':'active','preparing':False,'revision':3},
                    'snapshot':{'saved':False,'status':'pending'}}
        text=await project_result('保存 prj#1 里程碑',call)
        self.assertEqual(calls,[('project_snapshot',{'project_handle':'prj#1','label':'里程碑'})])
        self.assertIn('尚未',text) if '尚未' in text else self.assertIn('排队或执行中',text)
        self.assertNotIn('最近保存已完成',text)

    async def test_project_restore_and_readonly_format(self):
        async def call(name,args):
            self.assertEqual((name,args),('project_open',{'project_handle':'prj#2'}))
            return {'ok':True,'project':{'handle':'prj#2','title':'B','status':'active','preparing':False,'revision':1},'readonly':True}
        self.assertIn('只有查看权限',await project_result('切换 prj#2',call))

    async def test_grant_parser_rejects_before_host_effect(self):
        called=[]
        async def call(request):called.append(request);return {'ok':False,'error':'denied'}
        await grant_result('授予 prj#7 raw-openid read 60',call)
        self.assertEqual(called,[])
        self.assertEqual(await grant_result('授予 prj#7 msg#4 read 60',call),'denied')
        self.assertEqual(len(called),1)


class MemoryServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'memory.json'
        self.store=LongTermMemoryStore(self.path);self.source=message();self.allowed=True;self.group=False;self.effects=0
        @contextmanager
        def scope(inbound):
            self.assertIs(inbound,self.source)
            if not self.allowed:raise CommandEffectDenied('TEST_REVOKED')
            self.effects+=1
            yield object()
        self.service=MemoryCommands(store=self.store,inbound=self.source,effect_scope=scope,database_call=lambda f:f(),
            group_write_authority=lambda c,i:self.group,provenance=lambda c,i:(17,42))

    async def call(self,text):return await self.service(self.source,ParsedCommand('memory',text))

    async def test_real_store_isolates_same_native_id_across_all_namespaces(self):
        self.assertEqual((await self.call('添加 喜欢绿色')).status,'ok')
        scopes=[message('OTHER_APP'),message(environment='sandbox'),message(group='OTHER_GROUP'),message(group=None)]
        for other in scopes:
            self.assertEqual(self.store.list_entries([other.actor.key]),[])
        entry=self.store.list_entries([self.source.actor.key])[0]
        self.assertIsNone(entry.creator_user_id);self.assertEqual(entry.creator_actor_key,self.source.actor.key)
        self.assertEqual(entry.source_message_id,42)

    async def test_platform_owner_is_not_local_group_write_authority(self):
        result=await self.call('群 共同规则')
        self.assertEqual(result.status,'denied');self.assertEqual(self.store.all_entries(),[])
        self.group=1
        self.assertEqual((await self.call('群 共同规则')).status,'denied')
        self.group=True
        self.assertEqual((await self.call('群 共同规则')).status,'ok')

    async def test_canonical_history_does_not_grant_group_write(self):
        source=self.source
        old_scope=message(group='OTHER_GROUP').scope
        self.source=replace(source,canonical_scope=old_scope)
        self.service.inbound=self.source;self.group=True
        self.assertEqual((await self.call('群 secret')).status,'denied')

    async def test_revoke_before_clear_does_not_write(self):
        await self.call('添加 keep');self.allowed=False
        with self.assertRaises(CommandEffectDenied):await self.call('清空')
        self.assertEqual(len(self.store.list_entries([self.source.actor.key])),1)

    async def test_cross_scope_id_cannot_delete(self):
        entry,_=self.store.add(message(group='OTHER_GROUP').actor.key,'user','private',creator_actor_key=message(group='OTHER_GROUP').actor.key)
        self.assertEqual((await self.call(f'删除 {entry.id}')).status,'not_found')
        self.assertEqual(len(self.store.all_entries()),1)

    async def test_clear_all_only_mutates_authorized_addresses_and_keeps_audit(self):
        await self.call('添加 mine')
        self.group=True;await self.call('群 ours');self.group=False
        text=(await self.call('清空 全部')).text
        self.assertIn('1 条',text)
        self.assertEqual([(entry.scope_type,entry.content) for entry in self.store.all_entries()],[('group','ours')])
        audit=self.store.audit([self.source.actor.key])
        self.assertEqual(audit[0].action,'clear');self.assertIsNone(audit[0].actor_user_id)
        self.assertEqual(audit[0].actor_key,self.source.actor.key)
        reopened=LongTermMemoryStore(self.path)
        self.assertEqual(reopened.list_entries([self.source.actor.key]),[])
        self.assertEqual(reopened.audit([self.source.actor.key])[0].action,'clear')

    async def test_clear_invalidates_stale_semantic_memory_hit(self):
        await self.call('添加 indexed')
        entry=self.store.list_entries([self.source.actor.key])[0]
        app=SimpleNamespace(long_term_memory=self.store)
        context=ToolContext(SimpleNamespace(context=app),inbound=self.source,
                            platform=SimpleNamespace(account=self.source.endpoint.account))
        hit=SimpleNamespace(source_handle=f'memory#{entry.id}',source_type='memory',scope_key=entry.scope_key,score=0.9)
        self.assertEqual(len(_official_semantic_payload(context,[hit],set(),10)),1)
        await self.call('清空')
        self.assertEqual(_official_semantic_payload(context,[hit],set(),10),[])

    async def test_secret_rejected_and_empty_clear_has_no_false_count(self):
        self.assertEqual((await self.call('添加 password=TEST_SECRET')).status,'denied')
        self.assertIn('0 条',(await self.call('清空')).text)

    async def test_different_source_object_cannot_reuse_bound_service(self):
        with self.assertRaises(CommandEffectDenied):
            await self.service(replace(self.source),ParsedCommand('memory','添加 invalid'))
        self.assertEqual(self.store.all_entries(),[])


class ProjectSourceTests(unittest.TestCase):
    def provider(self,kind=None):
        kwargs={} if kind is None else {'expected_execution_kind':kind}
        return ForegroundProjectProvider(token=object(),handle=SimpleNamespace(endpoint=message().endpoint,actor=message().actor),
            job=object(),admission=object(),route_epoch=1,route_guard=lambda *a,**k:True,clock=lambda:100,**kwargs)

    def test_default_chat_and_command_source_do_not_cross(self):
        for configured in ('chat','command'):
            provider=self.provider(configured)
            for actual in ('chat','command'):
                with self.subTest(configured=configured,actual=actual),patch('src.plugins.ai_chat.project_authorization.execution.assert_execution_current',
                    return_value={'state':'executing','execution_kind':actual,'deadline_at':101}):
                    if configured==actual:self.assertEqual(provider._current(object())['execution_kind'],actual)
                    else:
                        with self.assertRaises(Denied):provider._current(object())

    def test_unknown_kind_and_expiry_remain_denied(self):
        with self.assertRaises(Denied):self.provider('migration')
        with patch('src.plugins.ai_chat.project_authorization.execution.assert_execution_current',return_value={'state':'executing','execution_kind':'command','deadline_at':100}):
            with self.assertRaises(Denied):self.provider('command')._current(object())


class BusinessFactoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_router_builds_only_after_durable_begin(self):
        from src.plugins.ai_chat.adapters.qq_official.commands import route_command
        from src.plugins.ai_chat.application.commands import CommandResult
        order=[]
        async def begin(name,inbound):order.append('begin')
        async def memory(inbound,request):order.append('memory');return CommandResult('ok','TEST')
        def factory():
            self.assertEqual(order,['begin']);order.append('factory')
            return CommandServices(memory=memory)
        result=await route_command('/memory',message(),services=CommandServices(),admission=lambda _:True,
            before_effect=begin,services_factory=factory)
        self.assertEqual(result.result.text,'TEST');self.assertEqual(order,['begin','factory','memory'])

    async def test_begin_failure_and_control_never_build_business_authority(self):
        from src.plugins.ai_chat.adapters.qq_official.commands import route_command
        def factory():self.fail('factory before durable source')
        async def begin(name,inbound):raise RuntimeError('TEST_UNAVAILABLE')
        with self.assertRaises(RuntimeError):
            await route_command('/项目',message(),services=CommandServices(),admission=lambda _:True,
                before_effect=begin,services_factory=factory)
        self.assertEqual((await route_command('/bind TEST',message(),services=CommandServices(),
            admission=lambda _:True,before_effect=begin,services_factory=factory)).kind,'control')


class RecipientTests(unittest.TestCase):
    def setUp(self):
        from src.plugins.ai_chat.adapters.qq_official.project_commands import CommandProjectProvider
        self.source=message();self.now=400
        self.provider=CommandProjectProvider(token=object(),handle=SimpleNamespace(endpoint=self.source.endpoint,
            actor=self.source.actor,endpoint_generation=2,route_epoch=3,execution_id='TEST_EXEC'),job=object(),
            admission=object(),route_epoch=4,route_guard=lambda *a,**k:True,clock=lambda:self.now)
        self.current=patch.object(self.provider,'_current',return_value={'state':'executing'})
        self.current.start();self.addCleanup(self.current.stop)
        self.actor=message(user='RECIPIENT').actor
        self.row=dict(sender_actor_key=self.actor.key,actor_key=self.actor.key,occurred_at=100,
            source_endpoint_key=self.source.endpoint.key,endpoint_key=self.source.endpoint.key,
            account_key=self.source.endpoint.account.key,scope_key=self.source.endpoint.scope.key,
            echo_state='not_echo',status='processed',endpoint_generation=2,route_epoch=3)
        owner=self
        class Cursor:
            def execute(self,query,args):
                owner.assertIn('conversation_visibility',query)
                owner.assertIn("direction='inbound'",query)
                owner.assertIn('i.native_message_id=m.native_message_id',query)
                owner.assertEqual(args,(42,owner.source.endpoint.key,owner.source.endpoint.scope.key,
                                       owner.source.endpoint.account.key,owner.source.endpoint.key,2,3))
                return self
            def fetchone(self):return owner.row
        self.cursor=Cursor()

    def test_exact_recent_source_evidence_is_not_issuer_or_membership(self):
        self.assertEqual(self.provider.recipient(self.cursor,42),self.actor)
        self.assertNotEqual(self.actor,self.source.actor)
        self.assertTrue(self.provider._recipient(self.cursor,endpoint=self.source.endpoint,actor=self.actor,
            evidence_ref='message:42',source=self.provider))

    def test_same_native_other_account_endpoint_and_c2c_are_denied(self):
        for actor in (message(app='OTHER').actor,message(environment='sandbox').actor,
                      message(group='OTHER').actor,message(group=None).actor):
            with self.subTest(actor=actor.identity_kind):
                self.row['actor_key']=self.row['sender_actor_key']=actor.key
                with self.assertRaises(Denied):self.provider.recipient(self.cursor,42)

    def test_stale_future_echo_old_generation_and_route_denied(self):
        original=self.row.copy()
        for key,value in [('occurred_at',99),('occurred_at',401),('endpoint_generation',1),
                          ('route_epoch',2),('status','pending'),('echo_state','unknown')]:
            self.row=original|{key:value}
            with self.subTest(key=key),self.assertRaises(Denied):self.provider.recipient(self.cursor,42)

    def test_wrong_host_source_never_authorizes_recipient(self):
        self.assertFalse(self.provider._recipient(self.cursor,endpoint=self.source.endpoint,actor=self.actor,
            evidence_ref='message:42',source=object()))


class RoutineDomainAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from src.plugins.ai_chat.adapters.qq_official.routine_commands import RoutineCommands
        from src.plugins.ai_chat.adapters.qq_official.native_schedules import NativeCommandResult
        self.message=message();self.calls=[]
        self.life=SimpleNamespace(handle=SimpleNamespace(endpoint=self.message.endpoint,actor=self.message.actor,
            source_native_message_id=self.message.native_message_id,execution_id='TEST_EXEC'),job=object())
        owner=self
        class Domain:
            def list_owned(self,source):
                owner.assertIs(source.handle,owner.life.handle);owner.assertIs(source.job,owner.life.job)
                return (dict(routine_id=7,version=3,name='晨报',cron='0 9 * * *',timezone='Asia/Shanghai',
                    max_tokens=2048,daily_tokens=8192,daily_runs=3,enabled=True,next_due_at=1234),)
            def set_routine(self,source,**kwargs):
                owner.calls.append((source,kwargs));return NativeCommandResult('updated',7,4,2345)
            def cancel(self,source,**kwargs):
                owner.calls.append((source,kwargs));return NativeCommandResult('cancelled',7)
        self.handler=RoutineCommands(domain=Domain(),life=self.life,inbound=self.message,database_calls=lambda f,*a,**k:f(*a,**k))

    async def test_edit_passes_exact_id_version_and_preserves_existing_policy_parameters(self):
        result=await self.handler(self.message,ParsedCommand('routine','修改 routine#7 v3 | 0 10 * * * | 新摘要'))
        self.assertIn('routine#7 v4',result.text)
        source,args=self.calls[0]
        self.assertEqual((args['routine_id'],args['expected_version'],args['name']),(7,3,'晨报'))
        self.assertEqual((args['max_tokens'],args['daily_tokens'],args['daily_runs']),(2048,8192,3))
        self.assertNotIn('authorization_until',args)
        self.assertEqual(len(args['request_key']),64)
        self.assertIs(source.inbound,self.message)

    async def test_unknown_handle_never_upserts_by_name(self):
        result=await self.handler(self.message,ParsedCommand('routine','修改 routine#999 v3 | 0 10 * * * | 新摘要'))
        self.assertIn('没有这个由你创建',result.text);self.assertEqual(self.calls,[])

    async def test_request_key_stable_for_exact_execution_and_payload(self):
        request=ParsedCommand('routine','添加 晨报 | 0 9 * * * | 整理')
        await self.handler(self.message,request);await self.handler(self.message,request)
        self.assertEqual(self.calls[0][1]['request_key'],self.calls[1][1]['request_key'])
        await self.handler(self.message,ParsedCommand('routine','添加 晨报 | 0 10 * * * | 整理'))
        self.assertNotEqual(self.calls[0][1]['request_key'],self.calls[2][1]['request_key'])


class WikiBoundaryTests(unittest.TestCase):
    def setUp(self):
        from src.plugins.ai_chat.adapters.qq_official.wiki_commands import OfficialWikiStore
        from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
        self.store=OfficialWikiStore.__new__(OfficialWikiStore)
        self.source=message();self.store.account_key=self.source.endpoint.account.key
        self.store.job_kind=account_job_kind('wiki',self.store.account_key)

    def test_official_full_scope_never_truncates_into_legacy_lane(self):
        source=message(app='a'*100,group='g'*200)
        self.store.account_key=source.endpoint.account.key
        from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
        self.store.job_kind=account_job_kind('wiki',self.store.account_key)
        lane=self.store._job_scope_key(source.scope)
        self.assertLess(len(lane),100)
        job=SimpleNamespace(kind=self.store.job_kind,payload={'scope_key':source.scope.key})
        self.assertEqual(self.store._job_scope(job),source.scope)
        key=self.store._job_key(source.scope,2,'2026-10-29',1)
        self.assertLess(len(key),100)
        other=message(app='a'*100,group='g'*199+'h')
        self.assertNotEqual(self.store._job_scope_key(other.scope),lane)
        self.assertNotEqual(self.store._job_key(other.scope,2,'2026-10-29',1),key)

    def test_legacy_store_does_not_enable_official_group(self):
        from src.plugins.ai_chat.group_wiki import GroupWikiStore, WIKI_JOB_KIND, WikiLeaseLost
        with self.assertRaises(ValueError):GroupWikiStore._group(self.source.scope)
        with self.assertRaises(WikiLeaseLost):
            self.store._job_scope(SimpleNamespace(kind=WIKI_JOB_KIND,payload={'scope_key':self.source.scope.key}))

    def test_other_account_and_private_scope_rejected(self):
        from src.plugins.ai_chat.group_wiki import WikiLeaseLost
        for source in (message(app='OTHER'),message(environment='sandbox'),message(group=None)):
            with self.subTest(kind=source.endpoint.kind),self.assertRaises(WikiLeaseLost):self.store._group(source.scope)

    def test_direct_configure_and_newcomer_never_bypass_source(self):
        from src.plugins.ai_chat.group_wiki import WikiLeaseLost
        with self.assertRaises(WikiLeaseLost):self.store.configure(self.source.scope,enabled=True)
        with self.assertRaises(WikiLeaseLost):self.store.enqueue_newcomer(self.source.scope,user_id='TEST')

    def test_clock_rejects_nan_infinity_bool(self):
        from src.plugins.ai_chat.group_wiki import WikiLeaseLost
        for value in (True,float('nan'),float('inf'),-1):
            self.store.clock=lambda:value
            with self.subTest(value=repr(value)),self.assertRaises(WikiLeaseLost):self.store._now()


class RoutineContractPresentationTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_contract_is_visible_and_legacy_output_unchanged(self):
        base=dict(handle='routine#7',version=2,name='晨报',enabled=True,cron='0 9 * * *',timezone='UTC',next_due_at=100)
        async def legacy(name,args):return {'ok':True,'routines':[base],'routine':base}
        old=await routine_result('列表',legacy)
        self.assertEqual(old,'routine#7 v2 · 晨报 · 启用\n0 9 * * * (UTC) · 下次 100')
        frozen=base|dict(authorization_until=1800000000,execution_seconds=60,delivery_seconds=300)
        async def official(name,args):return {'ok':True,'routines':[frozen],'routine':frozen}
        listed=await routine_result('列表',official,explicit_targets=True)
        created=await routine_result('添加 晨报 | 0 9 * * * | 摘要',official,explicit_targets=True)
        for value in (listed,created):
            self.assertIn('授权至 2027-01-15 08:00 UTC',value)
            self.assertIn('单次执行 60 秒，结果期限 300 秒',value)
