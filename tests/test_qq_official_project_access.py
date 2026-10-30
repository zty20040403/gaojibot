"""Actual project store and tools with source-bound official authorization."""
from dataclasses import replace

from tests.qq_official_project_fixture import ProjectFixture
from src.plugins.ai_chat.adapters.qq_official.project_authority import Operation
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.projects import ProjectAccess, ProjectStore


class ProjectAccessTests(ProjectFixture):
    def test_create_list_and_update_use_real_execution_and_atomic_explicit_grant(self):
        row = self.create()
        self.assertFalse(self.access.member or self.access.admin)
        self.assertEqual(self.store.list(self.access)[0]['project_id'],row['project_id'])
        changed = self.store.update(self.access,row['project_id'],title='NEW',expected_version=row['version'],now=self.now)
        self.assertEqual(changed['title'],'NEW')
        grants = self.query('SELECT * FROM qq_official_project_grants')
        self.assertEqual(len(grants),1)
        self.assertEqual(grants[0]['actor_key'],self.actor.key)
        self.assertEqual(set(grants[0]['capabilities']),{op.value for op in Operation})
        self.assertEqual(self.create()['project_id'],row['project_id'])
        self.assertEqual(len(self.query('SELECT * FROM qq_official_project_grants')),1)

    def test_old_membership_never_replaces_removed_official_grant(self):
        row=self.create()
        self.query("UPDATE qq_official_project_grants SET state='revoked',revision=revision+1,revoked_at=issued_at")
        self.assertTrue(self.query('SELECT * FROM project_members'))
        with self.assertRaises(PermissionError):self.store.get(self.access,row['project_id'])
        self.assertEqual(self.store.list(self.access),[])

    def test_regrant_does_not_replace_pinned_revision(self):
        row=self.create()
        self.query('UPDATE qq_official_project_grants SET revision=revision+1')
        with self.assertRaises(PermissionError):self.store.get(self.access,row['project_id'])
        with self.tx() as cursor:fresh=self.provider_for().issue(cursor)
        self.assertEqual(self.store.get(fresh,row['project_id'])['project_id'],row['project_id'])

    def test_read_grant_cannot_write_despite_legacy_creator_and_owner(self):
        row=self.create()
        self.query("UPDATE qq_official_project_grants SET capabilities=ARRAY['project.read']")
        self.assertEqual(self.store.get(self.access,row['project_id'])['project_id'],row['project_id'])
        with self.assertRaises(PermissionError):
            self.store.update(self.access,row['project_id'],title='BAD',expected_version=row['version'])

    def test_dataclass_copy_or_mixed_legacy_flags_cannot_issue_authority(self):
        row=self.create()
        for forged in (replace(self.access,official_proof=replace(self.access.official_proof)),
                       replace(self.access,member=True),replace(self.access,admin=True),
                       replace(self.access,scope=replace(self.access.scope,bot_native_user_id='OTHER')),
                       ProjectAccess(self.access.scope,self.access.principal_id,True,True)):
            with self.subTest(forged=type(forged)):
                with self.assertRaises(PermissionError):self.store.get(forged,row['project_id'])

    def test_dedicated_account_lease_loss_stops_existing_access(self):
        row=self.create()
        self.account_lease.close()
        with self.assertRaises(PermissionError):self.store.get(self.access,row['project_id'])

    def test_route_freeze_blocks_read_and_write_and_create(self):
        row=self.create();self.routes_live=False
        with self.assertRaises(PermissionError):self.store.get(self.access,row['project_id'])
        with self.assertRaises(PermissionError):self.create()

    def test_owner_revocation_blocks_creation_and_existing_project_grant(self):
        row=self.create()
        self.query("UPDATE qq_official_owner_actor_links SET state='revoked',revision=revision+1")
        with self.assertRaises(PermissionError):self.store.get(self.access,row['project_id'])
        with self.assertRaises(PermissionError):self.store.create(self.access,'NEW',now=self.now)

    def test_create_and_grant_rollback_together(self):
        from unittest.mock import patch
        with patch.object(ProjectStore,'_event',side_effect=RuntimeError('TEST rollback')):
            with self.assertRaises(RuntimeError):self.create()
        self.assertEqual(self.query('SELECT * FROM projects'),[])
        self.assertEqual(self.query('SELECT * FROM qq_official_project_grants'),[])
        self.assertTrue(self.create()['project_id'])

    def test_bound_owner_gets_old_project_without_manual_grant(self):
        scope=ConversationScope('onebot-v11','group','101','201','301')
        principal=self.ledger.ensure_principal_identity('onebot-v11','201','old')
        old=self.store.create(ProjectAccess(scope,principal,True),'OLD',now=self.now)
        self.aliases.add(scope.key);self.alias_id=old['project_id']
        self.query('UPDATE qq_official_endpoints SET canonical_scope_key=? WHERE endpoint_key=?', (scope.key,self.endpoint.key))
        got=self.store.get(self.access,old['project_id'])
        self.assertEqual((got['scope_key'],got['bot_native_user_id']),(scope.key,'301'))
        self.assertEqual(self.store.list(self.access)[0]['project_id'],old['project_id'])
        self.store.update(self.access,old['project_id'],title='UPDATED',expected_version=old['version'],now=self.now)
        self.query("UPDATE projects SET bot_native_user_id='302' WHERE project_id=?",(old['project_id'],))
        with self.assertRaises(PermissionError):self.store.get(self.access,old['project_id'])


    def tool_context(self):
        from types import SimpleNamespace
        from src.plugins.ai_chat.platform_types import InboundMessage
        from src.plugins.ai_chat.message_ir import MessageBody, TextNode
        async def db_call(function,*args,**kwargs):
            return function(*args,**kwargs)
        def forbidden_native_policy(*args):
            raise AssertionError("official actor must not use numeric OneBot policy")
        inbound=InboundMessage(self.endpoint,self.actor,None,'TEST_INPUT','test','',
            MessageBody((TextNode(0,'TEST project request'),)),True,None,self.now,None)
        runtime=SimpleNamespace(store=self.store,ready=True,closed=False)
        app=SimpleNamespace(project_runtime=runtime,db_call=db_call,
            settings=SimpleNamespace(projects_enabled=True,sandbox_backend='vm',
                is_sandbox_user_allowed=forbidden_native_policy))
        return SimpleNamespace(app=app,inbound=inbound,event=None,routine_run_id=None,
            platform=SimpleNamespace(project_provider=self.provider),capabilities={'projects'})

    def test_official_tool_and_prompt_context_need_no_onebot_event_or_numeric_actor(self):
        import asyncio
        import json
        from types import SimpleNamespace
        from src.plugins.ai_chat.tools.projects import execute
        from src.plugins.ai_chat.project_context import current_project_payload
        row=self.create()
        context=self.tool_context()
        answer=json.loads(asyncio.run(execute(SimpleNamespace(context=context),{},name='project_list')))
        self.assertTrue(answer['ok'])
        self.assertEqual(answer['projects'][0]['handle'],f"prj#{row['project_id']}")
        payload=asyncio.run(current_project_payload(context))
        self.assertEqual(payload['handle'],f"prj#{row['project_id']}")
        self.assertTrue(payload['sandbox_admitted'])
        self.query("UPDATE qq_official_project_grants SET capabilities=ARRAY['project.read']")
        payload=asyncio.run(current_project_payload(context))
        self.assertFalse(payload['sandbox_admitted'])

    def test_official_tool_source_is_rechecked_after_provider_configuration(self):
        import asyncio
        import json
        from types import SimpleNamespace
        from src.plugins.ai_chat.tools.projects import execute
        self.create();context=self.tool_context();self.routes_live=False
        answer=json.loads(asyncio.run(execute(SimpleNamespace(context=context),{},name='project_list')))
        self.assertFalse(answer['ok'])
        self.assertEqual(answer['code'],'project_forbidden')

    def test_unknown_host_route_callback_is_denial(self):
        with self.tx() as cursor:
            with self.assertRaises(PermissionError):self.provider_for(route_guard=lambda *a,**kw:1).issue(cursor)

    def test_foreground_provider_cannot_authorize_synthetic_background_source(self):
        import asyncio
        from src.plugins.ai_chat.platform_types import BackgroundToolSource
        from src.plugins.ai_chat.project_access import project_access
        context = self.tool_context()
        context.inbound = None
        context.background_source = BackgroundToolSource(self.endpoint, self.actor, 'project', 'task:1', 1)
        with self.assertRaises(PermissionError):
            asyncio.run(project_access(context))

    def test_configured_provider_must_match_actual_tool_actor(self):
        import asyncio
        from src.plugins.ai_chat.platform_types import ActorKey
        from src.plugins.ai_chat.project_access import project_access
        context = self.tool_context()
        actor = ActorKey(self.account, 'group_member', self.endpoint.native_conversation_id, 'TEST_OTHER_ACTOR')
        context.inbound = replace(context.inbound, actor=actor)
        with self.assertRaises(PermissionError):
            asyncio.run(project_access(context))
