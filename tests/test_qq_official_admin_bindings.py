"""Official-only admin creation in real PG with strict legacy defaults."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from psycopg.errors import CheckViolation

from src.plugins.ai_chat.adapters.qq_official.admin_bindings import AdminBindingStore, two_distinct_official_admins
from src.plugins.ai_chat.adapters.qq_official.project_authority import Denied
from src.bot_security.passwords import hash_password
from src.bot_security.store import SecurityError, SecurityStore
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from tests.qq_official_authority_fixture import AuthorityFixture


class AdminBindingsTests(AuthorityFixture):
    @classmethod
    def setUpClass(cls):
        cls.encoded_password = hash_password('TEST_ONLY_PASSWORD_123456')

    def setUp(self):
        super().setUp()
        self.authorization = object()
        self.control_live = True
        self.admins = AdminBindingStore(control_guard=self.control, clock=lambda: self.now)

    def control(self, cursor, *, authorization, **kwargs):
        return authorization is self.authorization and self.control_live

    def create(self, **changes):
        with self.tx() as cursor:
            return self.admins.create_official_admin(cursor, **(dict(username='test_admin',
                password_hash=self.encoded_password, endpoint=self.private, actor=self.owner_actor,
                owner_revision=self.owner.revision, authorization=self.authorization, bootstrap=True) | changes))

    def resolve(self, **changes):
        with self.tx() as cursor:
            return self.admins.resolve_admin(cursor, **(dict(endpoint=self.private, actor=self.owner_actor) | changes))

    def test_official_only_bootstrap_creates_bound_admin_without_qq(self):
        created = self.create()
        self.assertEqual(self.resolve(), created)
        with self.tx() as cursor:
            row = cursor.execute('SELECT qq_id,identity_mode,enabled FROM admin_accounts').fetchone()
        self.assertEqual((row['qq_id'], row['identity_mode'], row['enabled']), (None, 'official_c2c', 1))
        self.assertNotIn(self.owner_actor.native_user_id, repr(created))
        self.assertFalse(two_distinct_official_admins(created, created))

    def test_existing_legacy_validator_and_database_default_stay_strict(self):
        with self.assertRaises(SecurityError):
            SecurityStore.validate_account('legacy_admin', 'admin', None)
        with self.sql_rejects(CheckViolation):
            self.update('''INSERT INTO admin_accounts(account_id,username,password_hash,role,qq_id,created_at)
                VALUES ('TEST_NO_QQ','test_no_qq','TEST_HASH','admin',NULL,?)''', (self.now,))
        self.update('''INSERT INTO admin_accounts(account_id,username,password_hash,role,qq_id,created_at)
            VALUES ('TEST_LEGACY','test_legacy','TEST_HASH','admin','12345678',?)''', (self.now,))
        with self.tx() as cursor:
            row = cursor.execute("SELECT identity_mode FROM admin_accounts WHERE account_id='TEST_LEGACY'").fetchone()
        self.assertEqual(row['identity_mode'], 'legacy_qq')

    def test_mode_does_not_allow_mixed_or_unknown_identity(self):
        self.create()
        for sql in ("UPDATE admin_accounts SET qq_id='12345678'", "UPDATE admin_accounts SET identity_mode='any'",
                    "UPDATE admin_accounts SET role='member'", "UPDATE admin_accounts SET identity_mode='legacy_qq'"):
            with self.subTest(sql=sql), self.sql_rejects(CheckViolation):
                self.update(sql)

    def test_current_owner_does_not_imply_local_admin(self):
        with self.assertRaises(Denied):
            self.resolve()

    def test_default_false_unknown_or_truthy_control_cannot_create(self):
        for admin in (AdminBindingStore(clock=lambda:self.now),
                      AdminBindingStore(clock=lambda:self.now, control_guard=lambda *a, **k: 1)):
            self.admins = admin
            with self.assertRaises(Denied):
                self.create()
        with self.tx() as cursor:
            self.assertIsNone(cursor.execute('SELECT 1 FROM admin_accounts').fetchone())

    def test_bootstrap_cannot_overwrite_existing_admin(self):
        self.create()
        with self.assertRaises(Denied):
            self.create(username='test_second')

    def test_creation_and_binding_roll_back_together(self):
        try:
            with self.tx() as cursor:
                self.admins.create_official_admin(cursor, username='test_admin', password_hash=self.encoded_password,
                    endpoint=self.private, actor=self.owner_actor, owner_revision=1,
                    authorization=self.authorization, bootstrap=True)
                raise RuntimeError('TEST_ROLLBACK')
        except RuntimeError:
            pass
        with self.tx() as cursor:
            self.assertIsNone(cursor.execute('SELECT 1 FROM admin_accounts').fetchone())
            self.assertIsNone(cursor.execute('SELECT 1 FROM qq_official_admin_bindings').fetchone())

    def test_group_link_resolves_same_admin_but_never_other_speaker(self):
        made = self.create()
        self.assertEqual(self.resolve(endpoint=self.endpoint, actor=self.issuer), made)
        with self.assertRaises(Denied):
            self.resolve(endpoint=self.endpoint, actor=self.actor)

    def test_admin_revision_disable_binding_or_owner_revoke_fail_current_resolution(self):
        self.create()
        self.update('UPDATE admin_accounts SET version=version+1')
        with self.assertRaises(Denied):
            self.resolve()
        self.update('UPDATE admin_accounts SET version=1,enabled=0')
        with self.assertRaises(Denied):
            self.resolve()
        self.update('UPDATE admin_accounts SET enabled=1')
        self.update("UPDATE qq_official_admin_bindings SET state='revoked',revoked_at=updated_at,revision=revision+1")
        with self.assertRaises(Denied):
            self.resolve()
        self.update("UPDATE qq_official_admin_bindings SET state='active',revoked_at=NULL")
        with self.tx() as cursor:
            self.owners.apply_trusted_config(cursor, self.account, (), 'TEST_REVOKE')
        with self.assertRaises(Denied):
            self.resolve()

    def test_missing_private_endpoint_reachability_blocks_group_high_risk_identity(self):
        self.create()
        self.update("UPDATE qq_official_endpoints SET membership_state='absent' WHERE endpoint_key=?", (self.private.key,))
        with self.assertRaises(Denied):
            self.resolve(endpoint=self.endpoint, actor=self.issuer)

    def test_new_private_actor_needs_own_grant_and_distinct_admin_account_for_quorum(self):
        first = self.create()
        private = EndpointKey(self.account, 'private', 'TEST_OWNER_2')
        actor = ActorKey(self.account, 'c2c_user', '', 'TEST_OWNER_2')
        with self.tx() as cursor:
            cursor.execute('''INSERT INTO qq_official_endpoints(endpoint_key,account_key,kind,native_conversation_id,
                canonical_scope_key,binding_state,membership_state,created_at,updated_at)
                VALUES (?,?,'private',?,?,'unbound','present',?,?)''',
                (private.key, self.account.key, private.native_conversation_id, private.scope.key, self.now, self.now))
            grants = self.owners.apply_trusted_config(cursor, self.account, ('TEST_OWNER', 'TEST_OWNER_2'), 'TEST_CONFIG_2')
        second = self.create(username='test_second', endpoint=private, actor=actor,
                             owner_revision=grants[1].grant.revision, bootstrap=False)
        self.assertTrue(two_distinct_official_admins(first, second))
        self.assertFalse(two_distinct_official_admins(first, replace(second, admin_account_id=first.admin_account_id)))
        self.assertFalse(two_distinct_official_admins(first, replace(second,
            c2c_actor=first.c2c_actor, c2c_endpoint=first.c2c_endpoint)))

    def test_concurrent_bootstrap_creates_exactly_one_account_and_binding(self):
        start = threading.Barrier(2)

        def create(index):
            start.wait(3)
            try:
                return self.create(username=f'test_admin_{index}')
            except Denied:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = tuple(pool.map(create, (1, 2)))
        self.assertEqual(sum(value is not None for value in outcomes), 1)
        with self.tx() as cursor:
            self.assertEqual(cursor.execute('SELECT COUNT(*) AS n FROM admin_accounts').fetchone()['n'], 1)
            self.assertEqual(cursor.execute('SELECT COUNT(*) AS n FROM qq_official_admin_bindings').fetchone()['n'], 1)

    def migrate(self, revision):
        import os
        from pathlib import Path
        from unittest.mock import patch
        from alembic import command
        from alembic.config import Config
        root=Path(__file__).resolve().parents[1]
        config=Config(str(root/'alembic.ini'))
        config.set_main_option('script_location',str(root/'migrations'))
        with patch.dict(os.environ,{'AI_POSTGRES_SCHEMA':self.database.schema,'AI_POSTGRES_DSN':self.database.dsn}):
            if revision=='head':command.upgrade(config,revision)
            else:command.downgrade(config,revision)
