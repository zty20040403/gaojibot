"""Real PostgreSQL authority tests; no platform network or production grants.

Run with repository on PYTHONPATH and the existing loopback test database.
The root route/source ports are explicit test doubles: this tests project grant
transactions, not the separate root migration/source assembly.
"""
from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from psycopg.errors import CheckViolation, ForeignKeyViolation, UniqueViolation

from src.plugins.ai_chat.adapters.qq_official.project_authority import Denied, Operation, ProjectAuthority, ProjectGrantRef
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey
from src.plugins.ai_chat.subagents import SubAgentStore


from tests.qq_official_authority_fixture import AuthorityFixture

class ProjectAuthorityTests(AuthorityFixture):
    def new_project_task(self):
        store = SubAgentStore(self.database)
        self.addCleanup(store.close)
        trigger = self.ledger.record_message(self.endpoint.scope, native_message_id='TEST_TASK_INPUT',
            sender_native_user_id=self.actor.native_user_id, sender_display='Test member',
            body=MessageBody((TextNode(0, 'TEST project request'),)), occurred_at=self.now,
            actor=self.actor, source_endpoint=self.endpoint)
        with self.tx() as cursor:
            task = store.create_task(scope_key=(self.endpoint.scope.key), conversation_id='TEST_TASK',
                requester_user_id=None, trigger_message_id=trigger.canonical_message_id, objective='TEST project task',
                max_parallelism=1, max_steps=1, endpoint=self.endpoint, requester_actor=self.actor,
                now=self.now, transaction_cursor=cursor)
            cursor.execute('''INSERT INTO project_blobs(sha256,byte_size,storage_path,created_at)
                VALUES (?,0,'TEST_FAKE_NOT_READ',?)''', ('1'*64, self.now))
            cursor.execute('''INSERT INTO project_versions(project_id,version_no,manifest_sha256,file_count,
                total_bytes,created_by_principal_id,created_at) VALUES (?,1,?,0,0,?,?)''',
                (self.project_id, '1'*64, self.principal, self.now))
            cursor.execute('''INSERT INTO project_task_bindings(task_id,project_id,actor_principal_id,
                request_key,intent_sha256,base_version,manifest_sha256,generation,fence,created_at)
                VALUES (?,?,?,?,?,1,?,1,1,?)''',
                (task.task_id, self.project_id, self.principal, '2'*64, '3'*64, '1'*64, self.now))
        return task

    def test_exact_non_numeric_actor_and_resource_alias_work_without_merging_principal(self):
        ref = self.issue()
        got = self.require(self.request(Operation.EXECUTE))
        self.assertEqual((got.grant, got.principal_id, got.project_id), (ref, self.principal, self.project_id))
        with self.tx() as cursor:
            row = cursor.execute('SELECT canonical_scope_key FROM qq_official_endpoints WHERE endpoint_key=?',
                                 (self.endpoint.key,)).fetchone()
        self.assertEqual(row['canonical_scope_key'], self.endpoint.scope.key)
        self.assertNotEqual(row['canonical_scope_key'], self.scope.key)
        self.assertNotIn(self.actor.native_user_id, repr(got) + repr(self.request()))

    def test_legacy_project_creator_or_editor_does_not_supply_official_grant(self):
        self.update('''INSERT INTO project_members(project_id,principal_id,role,added_by,created_at)
            VALUES (?,?,'editor',?,?)''', (self.project_id, self.principal, self.principal, self.now))
        with self.assertRaises(Denied):
            self.require(ref=ProjectGrantRef('a'*32, 1))

    def test_default_authority_ports_are_fail_closed(self):
        self.issue()
        self.authority = ProjectAuthority(clock=lambda: self.now)
        with self.assertRaises(Denied):
            self.require()

    def test_truthy_callback_is_not_trusted_true(self):
        for name in ('route_guard', 'source_guard', 'recipient_evidence'):
            with self.subTest(name=name):
                self.authority = self.new_authority(**{name: lambda *args, **kwargs: 1})
                with self.assertRaises(Denied):
                    self.issue()

    def test_permissions_are_specific_not_owner_or_all_tools(self):
        self.issue(capabilities=frozenset({Operation.READ}))
        self.require()
        for operation in (Operation.WRITE, Operation.EXECUTE, Operation.MANAGE, Operation.DELIVER):
            with self.subTest(operation=operation), self.assertRaises(Denied):
                self.require(self.request(operation))

    def test_actor_and_endpoint_namespaces_do_not_alias_native_strings(self):
        self.issue()
        variants = [
            (AccountKey('qq-official', 'OTHER_APP', 'test'), 'group', 'TEST_GROUP/a:b'),
            (AccountKey('qq-official', 'TEST_APP', 'production'), 'group', 'TEST_GROUP/a:b'),
            (self.account, 'group', 'OTHER_GROUP'),
            (self.account, 'private', self.actor.native_user_id),
        ]
        for account, kind, native in variants:
            endpoint = EndpointKey(account, kind, native)
            actor = ActorKey(account, 'group_member' if kind == 'group' else 'c2c_user',
                             native if kind == 'group' else '', self.actor.native_user_id)
            with self.subTest(kind=kind, account=account.environment), self.assertRaises(Denied):
                self.require(self.request(endpoint=endpoint, actor=actor))

    def test_old_route_epoch_or_endpoint_generation_invalidates_grant(self):
        self.issue()
        with self.assertRaises(Denied):
            self.require(self.request(route_epoch=1))
        self.update('UPDATE qq_official_endpoints SET generation=generation+1 WHERE endpoint_key=?', (self.endpoint.key,))
        with self.assertRaises(Denied):
            self.require()

    def test_logical_route_and_endpoint_route_epochs_are_independent(self):
        self.issue()
        with self.tx() as cursor:
            row = cursor.execute('SELECT route_epoch,endpoint_route_epoch FROM qq_official_project_grants').fetchone()
        self.assertEqual((row['route_epoch'], row['endpoint_route_epoch']), (7, 2))
        with self.assertRaises(Denied):
            self.require(self.request(route_epoch=2))
        self.update('UPDATE qq_official_endpoints SET route_epoch=3 WHERE endpoint_key=?', (self.endpoint.key,))
        with self.assertRaises(Denied):
            self.require(self.request(endpoint_route_epoch=3))

    def test_waiting_for_project_lock_cannot_authorize_an_expired_grant(self):
        self.issue(expires_at=self.now+5)
        original = self.authority._project

        def later(cursor, request):
            original(cursor, request)
            self.now += 6

        self.authority._project = later
        with self.assertRaises(Denied):
            self.require()

    def test_source_lease_loss_while_waiting_for_project_is_rechecked(self):
        self.issue()
        original = self.authority._project

        def lost(cursor, request):
            original(cursor, request)
            self.source_live = False

        self.authority._project = lost
        with self.assertRaises(Denied):
            self.require()

    def test_revoked_route_endpoint_or_source_stops_before_effect(self):
        self.issue()
        for flag in ('route_live', 'source_live'):
            setattr(self, flag, False)
            with self.subTest(flag=flag), self.assertRaises(Denied):
                self.require()
            setattr(self, flag, True)
        self.update("UPDATE qq_official_endpoints SET membership_state='absent' WHERE endpoint_key=?", (self.endpoint.key,))
        with self.assertRaises(Denied):
            self.require()

    def test_time_boundary_is_exact_and_clock_rollback_does_not_make_unissued_grant_current(self):
        self.issue(expires_at=self.now+10)
        self.now += 9
        self.require()
        self.now += 1
        with self.assertRaises(Denied):
            self.require()
        self.now -= 11
        with self.assertRaises(Denied):
            self.require()

    def test_owner_revocation_invalidates_grant_without_cleanup(self):
        self.issue()
        with self.tx() as cursor:
            self.owners.apply_trusted_config(cursor, self.account, (), 'TEST_REMOVED')
        with self.assertRaises(Denied):
            self.require()
        with self.tx() as cursor:
            state = cursor.execute('SELECT state FROM qq_official_project_grants').fetchone()['state']
        self.assertEqual(state, 'active')

    def test_owner_link_revision_change_does_not_resurrect_grant(self):
        self.issue()
        self.update('UPDATE qq_official_owner_actor_links SET revision=revision+1 WHERE endpoint_key=?', (self.endpoint.key,))
        with self.assertRaises(Denied):
            self.require()

    def test_principal_mapping_swap_is_rejected(self):
        self.issue()
        other = self.ledger.ensure_principal_identity('onebot-v11', '202', 'Test other')
        self.update('UPDATE qq_official_project_grants SET principal_id=?', (other,))
        with self.assertRaises(Denied):
            self.require()

    def test_old_task_ref_stays_invalid_after_explicit_regrant(self):
        old = self.issue()
        with self.tx() as cursor:
            revoked = self.authority.revoke_project(cursor,
                issuer=self.request(Operation.MANAGE, actor=self.issuer), grant=old, source=self.source)
        self.assertEqual(revoked.revision, old.revision+1)
        self.issue(grant_id=old.grant_id, expected_revision=revoked.revision)
        self.require()
        with self.assertRaises(Denied):
            self.require(ref=old)

    def test_reissue_cannot_move_grant_to_a_new_actor(self):
        old = self.issue()
        with self.assertRaises(Denied):
            self.issue(grant_id=old.grant_id, expected_revision=old.revision, recipient=self.issuer)

    def test_fresh_evidence_required_for_delivery_but_does_not_grant_read(self):
        self.fresh = True
        with self.assertRaises(Denied):
            self.require(ref=ProjectGrantRef('b'*32, 1))
        self.issue()
        self.fresh = False
        self.require()
        with self.assertRaises(Denied):
            self.require(self.request(Operation.DELIVER))
        self.fresh = True
        self.require(self.request(Operation.DELIVER))

    def test_no_permission_from_unseen_recipient_or_unlinked_owner(self):
        self.recipient_seen = False
        with self.assertRaises(Denied):
            self.issue()
        self.recipient_seen = True
        with self.assertRaises(Denied):
            self.issue(issuer=self.request(Operation.MANAGE))

    def test_invalid_ids_capabilities_and_expiry_are_fixed_safe_errors(self):
        invalid = [dict(expires_at=True), dict(expires_at=self.now), dict(expires_at=self.now+86401),
                   dict(capabilities=frozenset({'project.read'})), dict(capabilities=frozenset()),
                   dict(evidence_ref='RAW SECRET \n'), dict(expected_revision=True), dict(grant_id='secret')]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaisesRegex(Denied, '^project_authority_denied$'):
                self.issue(**changes)

    def test_invalid_key_types_and_bot_principals_fail_before_sql(self):
        for changes in (dict(project_id=True), dict(route_epoch=True), dict(operation='project.read'),
                        dict(actor=ActorKey(self.account, 'bot', '', 'TEST_BOT')),
                        dict(actor=ActorKey(self.account, 'group_member', self.endpoint.native_conversation_id, 'x\0y'))):
            with self.subTest(changes=changes), self.assertRaises(Denied):
                self.request(**changes)

    def test_grant_schema_rejects_mixed_account_unknown_ops_and_unbounded_lifetime(self):
        self.issue()
        for sql, values, exception in [
            ('UPDATE qq_official_project_grants SET account_key=?', ('fake-account',), ForeignKeyViolation),
            ('UPDATE qq_official_project_grants SET capabilities=?', (['project.read', 'everything'],), CheckViolation),
            ('UPDATE qq_official_project_grants SET capabilities=?', ([None],), CheckViolation),
            ('UPDATE qq_official_project_grants SET expires_at=issued_at+86401', (), CheckViolation),
        ]:
            with self.subTest(sql=sql), self.sql_rejects(exception):
                self.update(sql, values)

    def test_caller_transaction_rollback_removes_grant_and_has_no_independent_commit(self):
        try:
            with self.tx() as cursor:
                self.authority.issue_project(cursor,
                    issuer=self.request(Operation.MANAGE, actor=self.issuer), recipient=self.actor,
                    capabilities=frozenset({Operation.READ}), grant_id='c'*32,
                    expires_at=self.now+60, evidence_ref='TEST_COMMAND', source=self.source)
                raise RuntimeError('test_rollback')
        except RuntimeError:
            pass
        with self.tx() as cursor:
            self.assertIsNone(cursor.execute('SELECT grant_id FROM qq_official_project_grants').fetchone())

    def test_current_effect_holds_fence_until_commit_and_revocation_blocks_next_effect(self):
        self.issue()
        checked, revoke_started, revoke_done = threading.Event(), threading.Event(), threading.Event()
        release = threading.Event()

        def effect():
            with self.tx() as cursor:
                self.authority.require_project(cursor, request=self.request(Operation.EXECUTE),
                                               grant=self.ref, source=self.source)
                checked.set()
                if not release.wait(3):
                    raise RuntimeError('test_timeout')
                cursor.execute("UPDATE projects SET title='Effect under current fence' WHERE project_id=?", (self.project_id,))

        def revoke():
            revoke_started.set()
            with self.tx() as cursor:
                self.owners.apply_trusted_config(cursor, self.account, (), 'TEST_REMOVED')
            revoke_done.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(effect)
            self.assertTrue(checked.wait(3))
            second = pool.submit(revoke)
            self.assertTrue(revoke_started.wait(3))
            self.assertFalse(revoke_done.wait(0.1))
            release.set()
            first.result(3)
            second.result(3)
        with self.assertRaises(Denied):
            self.require(self.request(Operation.EXECUTE))

    def test_admin_binding_schema_uses_exact_owner_actor_and_never_fake_qq(self):
        with self.tx() as cursor:
            cursor.execute('''INSERT INTO admin_accounts(account_id,username,password_hash,role,qq_id,created_at)
                VALUES ('TEST_ADMIN','TEST_ADMIN','TEST_NOT_A_REAL_HASH','admin','12345678',?)''', (self.now,))
            cursor.execute('''INSERT INTO qq_official_admin_bindings(binding_id,admin_account_id,admin_account_version,
                account_key,c2c_endpoint_key,c2c_actor_key,owner_grant_id,owner_revision,revision,state,created_at,updated_at)
                VALUES (?,'TEST_ADMIN',1,?,?,?,?,1,1,'active',?,?)''',
                ('d'*32, self.account.key, self.private.key, self.owner_actor.key, self.owner.grant_id, self.now, self.now))
        with self.sql_rejects(ForeignKeyViolation):
            self.update('UPDATE qq_official_admin_bindings SET c2c_actor_key=?', (self.actor.key,))
        # Version snapshots do not prevent account/owner revocation in place.
        self.update("UPDATE admin_accounts SET version=version+1 WHERE account_id='TEST_ADMIN'")
        with self.tx() as cursor:
            self.owners.apply_trusted_config(cursor, self.account, (), 'TEST_REMOVED')
        with self.sql_rejects(CheckViolation):
            self.update("UPDATE admin_accounts SET qq_id=NULL WHERE account_id='TEST_ADMIN'")

    def test_same_actor_project_cannot_have_competing_grant_rows(self):
        self.issue()
        with self.tx() as cursor:
            row = dict(cursor.execute('SELECT * FROM qq_official_project_grants').fetchone())
        row['grant_id'] = 'e'*32
        columns = ','.join(row)
        marks = ','.join('?' for _ in row)
        with self.sql_rejects(UniqueViolation):
            self.update(f'INSERT INTO qq_official_project_grants({columns}) VALUES ({marks})', tuple(row.values()))

    def test_actual_task_binding_freezes_original_grant_revision(self):
        old = self.issue()
        task = self.new_project_task()
        with self.tx() as cursor:
            frozen = self.authority.freeze_task_grant(cursor, request=self.request(Operation.EXECUTE),
                grant=old, task_id=task.task_id, source=self.source)
            cursor.execute('UPDATE subagent_tasks SET project_id=?,project_base_version=1 WHERE task_id=?',
                           (self.project_id, task.task_id))
        self.assertEqual(frozen, old)
        current = self.issue(grant_id=old.grant_id, expected_revision=old.revision)
        self.assertEqual(current.revision, old.revision+1)
        with self.assertRaises(Denied), self.tx() as cursor:
            self.authority.freeze_task_grant(cursor, request=self.request(Operation.EXECUTE),
                grant=current, task_id=task.task_id, source=self.source)
        with self.tx() as cursor:
            row = cursor.execute('SELECT grant_revision FROM qq_official_project_task_authority').fetchone()
        self.assertEqual(row['grant_revision'], old.revision)

    def test_task_grant_fk_does_not_allow_other_actor_or_account(self):
        self.issue()
        task = self.new_project_task()
        with self.tx() as cursor:
            self.authority.freeze_task_grant(cursor, request=self.request(Operation.EXECUTE),
                grant=self.ref, task_id=task.task_id, source=self.source)
        for column, value in [('actor_key', self.issuer.key), ('endpoint_key', self.private.key),
                              ('account_key', AccountKey('qq-official', 'OTHER', 'test').key)]:
            with self.subTest(column=column), self.sql_rejects(ForeignKeyViolation):
                self.update(f'UPDATE qq_official_project_task_authority SET {column}=?', (value,))

    def test_official_project_task_cannot_commit_without_exact_authority_binding(self):
        task = self.new_project_task()
        with self.sql_rejects(ForeignKeyViolation):
            self.update('UPDATE subagent_tasks SET project_id=?,project_base_version=1 WHERE task_id=?',
                        (self.project_id, task.task_id))
        with self.tx() as cursor:
            self.assertIsNone(cursor.execute('SELECT project_id FROM subagent_tasks WHERE task_id=?',
                                             (task.task_id,)).fetchone()['project_id'])

    def test_legacy_project_task_does_not_require_official_grant(self):
        self.new_project_task()  # Existing real project/version metadata.
        store = SubAgentStore(self.database)
        self.addCleanup(store.close)
        with self.tx() as cursor:
            task = store.create_task(scope_key=(self.scope.key), conversation_id='TEST_LEGACY_TASK',
                requester_user_id=201, trigger_message_id=None, objective='TEST legacy project task',
                max_parallelism=1, max_steps=1, now=self.now, transaction_cursor=cursor)
            cursor.execute('UPDATE subagent_tasks SET project_id=?,project_base_version=1 WHERE task_id=?',
                           (self.project_id, task.task_id))
        with self.tx() as cursor:
            row = cursor.execute('SELECT project_id,official_project_id FROM subagent_tasks WHERE task_id=?',
                                 (task.task_id,)).fetchone()
        self.assertEqual(row['project_id'], self.project_id)
        self.assertIsNone(row['official_project_id'])


if __name__ == '__main__':
    unittest.main()


class ProjectOriginAndTrashTests(AuthorityFixture):
    def test_trash_allows_only_explicit_manage_not_data_or_execution(self):
        self.issue(capabilities=frozenset(Operation))
        self.update("UPDATE projects SET status='trash' WHERE project_id=?",(self.project_id,))
        self.assertEqual(self.require(self.request(Operation.MANAGE)).project_id,self.project_id)
        for operation in (Operation.READ,Operation.WRITE,Operation.EXECUTE,Operation.DELIVER):
            with self.subTest(operation=operation),self.assertRaises(Denied):
                self.require(self.request(operation))
        self.update("UPDATE projects SET status='purged' WHERE project_id=?",(self.project_id,))
        with self.assertRaises(Denied):
            self.require(self.request(Operation.MANAGE))
