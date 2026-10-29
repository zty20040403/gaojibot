"""Shared project authority fixture: actual current source/lease/owner/grants.

No test case inheritance, ad-hoc DDL, credentials, model or transport calls.
"""
from pathlib import Path
import tempfile
import uuid

from tests.qq_official_file_fixture import ExecutionFixture
from src.plugins.ai_chat import project_authorization as project_host
from src.plugins.ai_chat.adapters.qq_official.authority import OwnerAuthority
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.adapters.qq_official.binding import BindingContext, BindingReducer, parse_binding_command
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.platform_types import EndpointKey, ActorKey
from src.plugins.ai_chat.projects import ProjectStore


class ProjectFixture(ExecutionFixture):
    def setUp(self):
        super().setUp()
        self.routes_live = True
        self.aliases = set()
        self.private = EndpointKey(self.account, 'private', 'TEST_PROJECT_OWNER')
        self.owner_actor = ActorKey(self.account, 'c2c_user', '', 'TEST_PROJECT_OWNER')
        self.provision(self.private)
        owners = OwnerAuthority(clock=lambda:self.now)
        with self.tx() as cursor:
            if cursor.execute("SELECT to_regclass('qq_official_project_grants') AS n").fetchone()['n'] is None:
                raise RuntimeError('official_project_schema_not_migrated')
            owners.apply_trusted_config(cursor, self.account, ('TEST_PROJECT_OWNER',), 'TEST_PROJECT_CONFIG')
            observed=observe_ready(cursor, self.token, {'op':0,'t':'READY','d':{'session_id':'TEST_PROJECT_SESSION','user': {'id':'TEST_REAL_BOT', 'bot':True}}}, self.now)
            self.assertEqual(observed.status,'verified')
        reducer = BindingReducer(b'TEST_ONLY_PROJECT_PEPPER_123456789012345', epoch_guard=assert_epoch, authority=owners, clock=lambda:self.now)
        def context(endpoint,actor):
            return BindingContext(endpoint,actor,uuid.uuid4().hex,self.now)
        with self.tx() as cursor:
            cursor.execute("UPDATE qq_official_endpoints SET binding_state='unbound' WHERE endpoint_key=?",(self.endpoint.key,))
            issued = reducer.apply(cursor, context=context(self.private,self.owner_actor),
                command=parse_binding_command('/bind create'), lease_token=self.token)
            reducer.mark_issued(cursor, account=self.account, challenge_id=issued.challenge_id,
                outcome='success', lease_token=self.token)
            submitted = reducer.apply(cursor, context=context(self.endpoint,self.actor),
                command=parse_binding_command(f'/bind submit {issued.challenge_id} {issued.effect.code}'), lease_token=self.token)
            self.assertEqual(submitted.status,'pending')
            confirmed = reducer.apply(cursor, context=context(self.private,self.owner_actor),
                command=parse_binding_command(f'/bind confirm {submitted.candidate_id} 1 --grant-owner'), lease_token=self.token)
            self.assertEqual(confirmed.status,'confirmed')
            # This synthetic inbound fixture is now admitted under the bound
            # endpoint, exactly as production TxA snapshots its current route.
            cursor.execute('''UPDATE qq_official_inbound_events SET endpoint_generation=e.generation,route_epoch=e.route_epoch
                FROM qq_official_endpoints e WHERE e.endpoint_key=qq_official_inbound_events.endpoint_key''')
        self.handle = self.claim().handle
        self.begin(self.handle)
        self.provider = self.provider_for()
        with self.tx() as cursor:
            self.access = self.provider.issue(cursor)
        temp = tempfile.TemporaryDirectory(prefix='TEST_project_host_')
        self.addCleanup(temp.cleanup)
        self.store = ProjectStore(self.database,Path(temp.name))
        self.addCleanup(self.store.close)

    def route_guard(self,cursor,*,canonical_scope,endpoint,route_epoch,operation):
        return (self.routes_live and endpoint==self.endpoint and route_epoch==0
                and (canonical_scope.key==self.endpoint.scope.key or canonical_scope.key in self.aliases)
                and operation in {'read','execute','send'})

    def provider_for(self,**changes):
        return project_host.ForegroundProjectProvider(**(dict(token=self.token,handle=self.handle,
            job=self.job,admission=self.admission,route_epoch=0,route_guard=self.route_guard,
            project_scope_guard=lambda cursor,**kw: kw['project']['project_id']==getattr(self,'alias_id',None)
                and kw['project']['bot_native_user_id']=='301',clock=lambda:self.now)|changes))

    def create(self):
        return self.store.create(self.access,'TEST project',now=self.now,creation_key='c'*64)
