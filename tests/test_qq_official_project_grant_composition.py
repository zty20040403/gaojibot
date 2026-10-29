"""Call-local grant composition boundaries; SQL authorization has separate PG coverage."""
from types import SimpleNamespace
import unittest
from dataclasses import replace
from unittest.mock import patch
from src.plugins.ai_chat.adapters.qq_official import migration_project_grants as module

from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey, ActorKey
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.adapters.qq_official.project_authority import Operation, ProjectGrantRef
from src.plugins.ai_chat.adapters.qq_official.migration_types import MigrationDenied


class Cursor:
    def __init__(self,item):self.item=item
    def execute(self,*args):return self
    def fetchone(self):return self.item


class CompositionTests(unittest.TestCase):
    def setUp(self):
        self.now=100
        endpoint=EndpointKey(AccountKey('qq-official','TEST_STAGE_CACHE','test'),'group','TEST_GROUP')
        actor=ActorKey(endpoint.account,'group_member','TEST_GROUP','TEST_ACTOR')
        self.scope=ConversationScope('onebot-v11','group','TEST_OLD')
        self.proof=SimpleNamespace(item_id='a'*32,migration_id='b'*32,export_digest='c'*64,
            target_endpoint=endpoint,target_actor=actor,mapping_id='d'*32,mapping_revision=1,
            route_epoch=2,endpoint_route_epoch=1)
        self.stage=dict(migration_id=self.proof.migration_id,project_id=7,
            target_endpoint_key=endpoint.key,target_actor_key=actor.key,
            mapping_id=self.proof.mapping_id,mapping_revision=1,expected_route_epoch=2,
            source_scope_key=self.scope.key)
        self.current=dict(capabilities=['project.execute','project.deliver'],issued_at=90,
            expires_at=101,principal_id=9)
        self.route=dict(phase='completed',epoch=2,active_endpoint_key=endpoint.key)
        self.cursor=Cursor(dict(state='active',kind='project_task',export_digest=self.proof.export_digest))
        self.grant=ProjectGrantRef('e'*32,1)
        self.loads=self.public_calls=0
        self.guards=[]
        self.callback=None
        self.public_result=True
        self.host=object.__new__(module.ProjectMigrationGrants)
        self.host.services=SimpleNamespace(clock=lambda:self.now,
            journal=SimpleNamespace(_load=lambda c,i:({},self.route)))
        def load(c,g,n):
            self.loads+=1
            return self.stage,self.current
        self.host._load=load
        def public(c,**kwargs):
            self.public_calls+=1
            return self.public_result
        self.host.project_scope_guard=public
        self.authority_calls=0
        case=self
        class Authority:
            def __init__(self,**kwargs):self.options=kwargs
            def require_project(self,c,*,request,grant,source):
                case.authority_calls+=1
                guard=self.options['source_guard']
                case.guards.append((guard,c,request,source))
                if case.callback:return case.callback(self,c,request,source)
                if not guard(c,request=request,source=source):raise PermissionError()
                if not guard(c,request=request,source=source):raise PermissionError()
                return 'checked'
        self.authority=Authority

    def call(self,*,now=100,phase='execute'):
        with patch.object(module,'ProjectAuthority',self.authority):
            return self.host._require(self.cursor,proof=self.proof,project_id=7,grant=self.grant,
                phase=phase,now=now,operation=Operation.EXECUTE)

    def test_same_call_runs_authority_and_reuses_only_source_load(self):
        self.assertEqual(self.call(),'checked')
        self.assertEqual((self.loads,self.public_calls,self.authority_calls),(1,0,1))
        self.assertEqual(self.call(),'checked')
        self.assertEqual((self.loads,self.public_calls,self.authority_calls),(2,0,2))

    def test_clock_boundary_reverts_to_full_public_guard(self):
        def check(authority,c,request,source):
            guard=authority.options['source_guard']
            self.assertTrue(guard(c,request=request,source=source))
            self.now=101
            self.public_result=False
            self.assertFalse(guard(c,request=request,source=source))
            self.assertEqual(authority.options['clock'](),101)
            return 'checked'
        self.callback=check
        self.call()
        self.assertEqual(self.public_calls,1)

    def test_slow_initial_validation_cannot_stamp_after_expiry(self):
        original=self.host._load
        def load(*args):
            result=original(*args)
            self.now=101
            self.public_result=False
            return result
        self.host._load=load
        with self.assertRaises(PermissionError):self.call()
        self.assertEqual(self.public_calls,1)

    def test_stale_caller_now_cannot_enable_local_reuse(self):
        self.now=101
        self.public_result=False
        with self.assertRaises(PermissionError):self.call(now=100)
        self.assertEqual(self.public_calls,1)

    def test_exact_cursor_proof_and_full_request_are_required(self):
        def check(authority,c,request,source):
            guard=authority.options['source_guard']
            self.assertFalse(guard(Cursor(c.item),request=request,source=source))
            self.assertFalse(guard(c,request=request,source=SimpleNamespace(**source.__dict__)))
            self.assertFalse(guard(c,request=replace(request,operation=Operation.DELIVER),source=source))
            self.assertFalse(guard(c,request=replace(request,route_epoch=3),source=source))
            self.assertFalse(guard(c,request=replace(request,canonical_scope=request.endpoint.scope),source=source))
            self.assertTrue(guard(c,request=request,source=source))
            return 'checked'
        self.callback=check
        self.call()
        self.assertEqual(self.public_calls,0)

    def test_closure_loses_authority_when_success_returns(self):
        self.call()
        guard,c,request,source=self.guards[-1]
        self.assertFalse(guard(c,request=request,source=source))

    def test_closure_loses_authority_after_exception(self):
        def fail(*args):raise RuntimeError('TEST_FAILURE')
        self.callback=fail
        with self.assertRaises(RuntimeError):self.call()
        guard,c,request,source=self.guards[-1]
        self.assertFalse(guard(c,request=request,source=source))

    def test_route_tuple_not_only_namespace_is_checked(self):
        self.route['epoch']=3
        with self.assertRaises(PermissionError):self.call()

    def test_invalid_clock_inputs_fail_closed(self):
        for value in (True,float('nan'),float('inf'),-1,2**63):
            with self.subTest(clock=value),self.assertRaises(MigrationDenied):
                self.now=value
                self.call()
        self.now=100
        for value in (True,float('nan'),100.0,-1,2**63):
            with self.subTest(now=value),self.assertRaises(MigrationDenied):self.call(now=value)

if __name__=='__main__':unittest.main()
