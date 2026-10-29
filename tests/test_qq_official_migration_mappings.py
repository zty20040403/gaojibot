"""Actual admin sessions + trusted gateway/ledger evidence + migration mappings."""
from dataclasses import replace
import unittest

from src.plugins.ai_chat.adapters.qq_official.migration_console import MigrationControlAuthority
from src.plugins.ai_chat.adapters.qq_official.migration_mappings import mapping_request
from src.plugins.ai_chat.adapters.qq_official.migration_preview import Selection
from src.plugins.ai_chat.adapters.qq_official.migration_types import MigrationDenied
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.platform_types import AccountKey,ActorKey,EndpointKey
from tests.qq_official_migration_journal_fixture import JournalFixture
from tests.qq_official_migration_mapping_fixture import record_evidence,rotate_mapping


class MigrationMappingTests(JournalFixture,unittest.TestCase):
    def request(self,**changes):
        value=mapping_request(selection=Selection(self.source,self.source_scope,self.endpoint),
            source_actor=self.source_actor,target_actor=self.actor,evidence_inbox_id=self.mapping_inbox,
            expected_revision=self.mapping_evidence.revision,expires_at=self.now+1000)
        return {**value,**changes}

    def grant(self,request=None,**changes):
        args=dict(cookie=self.mapping_cookie,csrf=self.mapping_csrf,action='mapping_put',request=request or self.request())
        args.update(changes)
        return self.mapping_authority.issue(**args)

    def current(self,**changes):
        args=dict(source_endpoint=self.source,source_scope=self.source_scope,
            source_actor=self.source_actor,target_endpoint=self.endpoint,now=self.now)
        args.update(changes)
        with self.tx() as cursor:
            return self.mapping_store.lookup(cursor,**args)

    def test_mapping_is_real_versioned_and_never_principal_merge(self):
        before=self.query('SELECT * FROM principals ORDER BY principal_id')
        first=self.current()
        second=self.rotate_mapping()
        self.assertEqual(first.mapping_id,second.mapping_id)
        self.assertEqual((1,2),(first.revision,second.revision))
        self.assertEqual(before,self.query('SELECT * FROM principals ORDER BY principal_id'))
        self.assertEqual([1,2],[r[0] for r in self.query('SELECT revision FROM migration_actor_mapping_revisions ORDER BY revision')])
        self.assertEqual(2,self.query('SELECT count(*) FROM migration_control_audit')[0][0])

    def test_source_account_group_scope_and_target_account_are_independent(self):
        other_group=EndpointKey(self.source.account,'group','OTHER_GROUP')
        other_source=EndpointKey(AccountKey('onebot-v11','OTHER_BOT','test'),'group',self.source.native_conversation_id)
        other_account=EndpointKey(AccountKey('qq-official','TEST_OTHER_MAPPING_ACCOUNT','test'),'group','TEST_GROUP')
        self.provision(other_account)
        for changes in (
            dict(source_endpoint=other_group,source_scope=ConversationScope('onebot-v11','group','OTHER_GROUP')),
            dict(source_endpoint=other_source,source_actor=ActorKey(other_source.account,'user','',self.source_actor.native_user_id)),
            dict(target_endpoint=other_account),
            dict(source_scope=ConversationScope('onebot-v11','group','ANOTHER')),
        ):
            with self.subTest(changes=tuple(changes)):
                with self.assertRaises(ValueError):self.current(**changes)

    def test_recent_processed_gateway_inbound_and_exact_actor_required(self):
        other=ActorKey(self.account,'group_member',self.endpoint.native_conversation_id,'TEST_OTHER_ACTOR')
        bad=[record_evidence(self,received_at=self.now-601),record_evidence(self,received_at=self.now+1),
             record_evidence(self,status='quarantined'),record_evidence(self,echo='possible_self'),
             record_evidence(self,gateway=False),record_evidence(self,actor=other)]
        for inbox in bad:
            with self.subTest(inbox=inbox):
                with self.assertRaises(MigrationDenied):rotate_mapping(self,inbox=inbox)
        self.assertEqual(1,self.current().revision)

    def test_route_revision_membership_and_expiry_rechecked_on_lookup(self):
        for sql in ("UPDATE qq_official_endpoints SET generation=2", "UPDATE qq_official_endpoints SET route_epoch=2",
                    "UPDATE qq_official_endpoints SET membership_state='absent'", "UPDATE qq_official_endpoints SET binding_state='revoked'"):
            with self.subTest(sql=sql):
                self.query(sql)
                with self.assertRaises(MigrationDenied):self.current()
                self.query("UPDATE qq_official_endpoints SET generation=1,route_epoch=1,membership_state='present',binding_state='active'")
        with self.assertRaises(MigrationDenied):self.current(now=self.now+1000)

    def test_revoke_preserves_old_revision_and_blocks_future_lookup(self):
        request=dict(mapping_id=self.mapping_evidence.mapping_id,expected_revision=1)
        grant=self.grant(request,action='mapping_revoke')
        with self.tx() as cursor:
            self.assertEqual(2,self.mapping_store.revoke(cursor,grant,**request,now=self.now))
        with self.assertRaises(MigrationDenied):self.current()
        self.assertEqual(['active','revoked'],[r[0] for r in self.query('SELECT state FROM migration_actor_mapping_revisions ORDER BY revision')])

    def test_stale_revision_and_non_integer_lifetime_cannot_mutate(self):
        for changes in (dict(expected=0),dict(expected=True),dict(expires=True),dict(expires=self.now),dict(expires=self.now+86401)):
            with self.subTest(changes=changes):
                with self.assertRaises(MigrationDenied):rotate_mapping(self,**changes)
        self.assertEqual(1,self.current().revision)

    def test_control_grant_requires_issue_and_exact_request_digest(self):
        request=self.request();grant=self.grant(request)
        with self.tx() as cursor:
            with self.assertRaises(MigrationDenied):
                self.mapping_authority.begin(cursor,replace(grant),action='mapping_put',request=request,now=self.now)
        for change in (dict(target_actor='CHANGED'),dict(expires_at=self.now+999),dict(expected_revision=7)):
            with self.tx() as cursor:
                with self.assertRaises(MigrationDenied):
                    self.mapping_authority.begin(cursor,grant,action='mapping_put',request={**request,**change},now=self.now)
        with self.tx() as cursor:
            with self.assertRaises(MigrationDenied):
                self.mapping_authority.control(cursor,grant,action='mapping_put',preview_digest='',now=self.now)

    def test_control_grant_is_transaction_bound_and_new_authority_cannot_replay(self):
        request=self.request();grant=self.grant(request)
        with self.tx() as cursor:
            self.mapping_authority.begin(cursor,grant,action='mapping_put',request=request,now=self.now)
            self.mapping_authority.control(cursor,grant,action='mapping_put',preview_digest='',now=self.now)
        with self.tx() as cursor:
            with self.assertRaises(MigrationDenied):
                self.mapping_authority.begin(cursor,grant,action='mapping_put',request=request,now=self.now)
            with self.assertRaises(MigrationDenied):
                MigrationControlAuthority(self.mapping_security).control(cursor,grant,action='mapping_put',preview_digest='',now=self.now)

    def test_control_rechecks_csrf_session_role_version_and_time(self):
        with self.assertRaises(MigrationDenied):self.grant(csrf='WRONG')
        grant=self.grant()
        self.query('UPDATE admin_accounts SET version=2')
        with self.tx() as cursor:
            with self.assertRaises(MigrationDenied):self.mapping_authority.begin(cursor,grant,action='mapping_put',request=self.request(),now=self.now)
        self.query('UPDATE admin_accounts SET version=1')
        self.query("UPDATE admin_accounts SET role='member'")
        with self.assertRaises(MigrationDenied):self.grant()
        self.query("UPDATE admin_accounts SET role='admin'")
        with self.tx() as cursor:
            with self.assertRaises(MigrationDenied):self.mapping_authority.begin(cursor,grant,action='mapping_put',request=self.request(),now=self.now+30)
        self.mapping_security.logout(self.mapping_cookie)
        with self.tx() as cursor:
            with self.assertRaises(MigrationDenied):self.mapping_authority.begin(cursor,grant,action='mapping_put',request=self.request(),now=self.now)

    def test_preview_digest_and_action_are_exact_for_stage_effects(self):
        request={'migration_id':'a'*32};preview='b'*64
        grant=self.grant(request,action='freeze',preview_digest=preview)
        with self.tx() as cursor:
            self.mapping_authority.begin(cursor,grant,action='freeze',request=request,preview_digest=preview,now=self.now)
            for action,value in (('switch',preview),('freeze','c'*64)):
                with self.assertRaises(MigrationDenied):self.mapping_authority.control(cursor,grant,action=action,preview_digest=value,now=self.now)
            self.mapping_authority.control(cursor,grant,action='freeze',preview_digest=preview,now=self.now)
