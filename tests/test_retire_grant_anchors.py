from contextlib import closing
import unittest

import psycopg
from sqlalchemy.exc import InternalError

from src.bot_storage.database import PostgresDatabase
from src.plugins.ai_chat.adapters.qq_official.authority import OwnerAuthority
from src.plugins.ai_chat.platform_types import ActorKey
from tests import test_qq_official_outbox_migration as migration


class RetireGrantTests(unittest.TestCase):
    def test_only_previously_resolvable_links_survive_and_still_require_current_config(self):
        schema = migration.owned_schema()
        self.addCleanup(migration.drop, schema)
        migration.migrate(schema, '0134_retire_reply_windows')
        rows = migration.OfficialOutboxMigrationTests()
        with migration.connect(schema) as rows.connection:
            db = rows.connection
            private = rows.endpoint(native='TEST_OWNER')
            group = rows.endpoint(kind='group', native='TEST_GROUP')
            challenge = rows.challenge(private)
            plan = rows.plan(private, ephemeral=True, challenge_id=challenge)
            grant, = db.execute('SELECT owner_grant_id FROM qq_official_binding_challenges').fetchone()
            migration.insert(db, 'qq_official_binding_candidates', dict(candidate_id='TEST_CANDIDATE',
                account_key=group.account.key, challenge_id=challenge, endpoint_key=group.key,
                actor_key=rows.actor(group).key, endpoint_generation=1, state='confirmed', expires_at=300,
                created_at=100, updated_at=100))
            def link(name, **anchor):
                member = ActorKey(group.account, 'group_member', group.native_conversation_id, name)
                migration.insert(db, 'qq_official_owner_actor_links', dict(endpoint_key=group.key,
                    account_key=group.account.key, actor_key=member.key, state='active', endpoint_generation=1,
                    created_at=100, updated_at=100, **anchor))
                return member
            old = link('TEST_OLD', owner_grant_id=grant, owner_revision=1, candidate_id='TEST_CANDIDATE')
            stale = link('TEST_STALE', owner_grant_id=grant, owner_revision=2, candidate_id='TEST_CANDIDATE')
            current = link('TEST_CURRENT', configured_owner_actor_key=rows.actor(private).key)
            db.execute('CREATE TABLE test_external_fk (origin text REFERENCES qq_official_owner_grants(grant_id))')
            before = db.execute('SELECT * FROM delivery_plans WHERE plan_id=%s', (plan['plan_id'],)).fetchone()
        with self.assertRaises(InternalError) as rejected:
            migration.migrate(schema, '0135_retire_grant_anchors')
        self.assertIsInstance(rejected.exception.orig, psycopg.errors.DependentObjectsStillExist)
        with migration.connect(schema) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM qq_official_owner_actor_links WHERE owner_grant_id IS NOT NULL').fetchone(), (2,))
            db.execute('DROP TABLE test_external_fk')
        migration.migrate(schema, '0135_retire_grant_anchors')
        with migration.connect(schema) as db:
            self.assertEqual(db.execute('SELECT * FROM delivery_plans').fetchone(), before)
        database = PostgresDatabase(migration.checked_test_dsn(), schema=schema)
        self.addCleanup(database.close)
        configured = OwnerAuthority(account=group.account, owner_c2c_ids=('TEST_OWNER',))
        removed = OwnerAuthority(account=group.account)
        with closing(database.store_connection()) as conn:
            c = conn.cursor()
            for actor in (old, current):
                self.assertEqual(configured.owner_for_actor(c, group, actor).c2c_actor_key, rows.actor(private).key)
                self.assertIsNone(removed.owner_for_actor(c, group, actor))
            self.assertIsNone(configured.owner_for_actor(c, group, stale))
            self.assertIsNone(configured.owner_for_actor(c, group, rows.actor(group)))
            removed.apply_trusted_config(c)
            configured.apply_trusted_config(c)
            self.assertIsNone(configured.owner_for_actor(c, group, old))
            self.assertIsNone(configured.owner_for_actor(c, group, current))
            self.assertIsNotNone(configured.owner_for_actor(c, private, rows.actor(private)))
