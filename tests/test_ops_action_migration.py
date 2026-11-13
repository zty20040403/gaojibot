"""Only owned migration-test schemas are upgraded or downgraded."""
import unittest
from unittest.mock import patch
from src.bot_storage import PostgresDatabase
from tests import test_ops_actions as fixture,test_qq_official_outbox_migration as migration


class OpsActionMigrationTests(unittest.TestCase):
    def test_empty_downgrade_but_real_audit_evidence_requires_export(self):
        schema = migration.owned_schema(); self.addCleanup(migration.drop,schema)
        migration.migrate(schema,'0088_agent_time_limits')
        migration.migrate(schema,'0091_ops_replies')
        migration.migrate(schema,'0090_ops_notices',downgrade=True)
        migration.migrate(schema,'0089_ops_actions',downgrade=True)
        migration.migrate(schema,'0088_agent_time_limits',downgrade=True)
        migration.migrate(schema,'0089_ops_actions')
        database = PostgresDatabase(migration.checked_test_dsn(),schema=schema)
        self.addCleanup(database.close)
        case = fixture.OpsActionTests('runTest'); self.addCleanup(case.doCleanups)
        with patch('tests.qq_official_outbox_fixture.postgres_for_test',return_value=database):
            case.setUp()
        original = case.request().action
        with case.tx() as cursor:
            case.ops.close(cursor,original['action_id'],state='denied',error='owner_declined')
        with self.assertRaisesRegex(RuntimeError,'ops_actions_require_export'):
            migration.migrate(schema,'0088_agent_time_limits',downgrade=True)
        with migration.connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT action_id,state FROM ops_actions').fetchone(),(original['action_id'],'denied'))
            self.assertEqual(connection.execute('SELECT version_num FROM alembic_version').fetchone(),('0089_ops_actions',))

    def test_notice_source_and_ephemeral_evidence_cannot_be_dropped(self):
        from src.plugins.ai_chat.adapters.qq_official.ops_notices import OpsNotices
        from src.plugins.ai_chat.adapters.qq_official.send_store import OfficialSendStore
        from src.plugins.ai_chat.delivery import DeliveryStore
        schema = migration.owned_schema(); self.addCleanup(migration.drop,schema)
        migration.migrate(schema,'0090_ops_notices')
        database = PostgresDatabase(migration.checked_test_dsn(),schema=schema); self.addCleanup(database.close)
        case = fixture.OpsActionTests('runTest'); self.addCleanup(case.doCleanups)
        with patch('tests.qq_official_outbox_fixture.postgres_for_test',return_value=database):
            case.setUp()
        deliveries = DeliveryStore(database); self.addCleanup(deliveries.close)
        outbox = OfficialSendStore(deliveries,admission=lambda *args:True,pepper=case.ops.pepper,clock=lambda:case.now)
        notices = OpsNotices(case.ops,outbox,configuration=lambda:case.config); outbox.ops_authority = notices
        proposal = case.request()
        with patch.object(type(case.config),'secret',return_value='TEST_SECRET_'*4),case.tx() as cursor:
            ref = notices.prepare(cursor,proposal)
        with self.assertRaisesRegex(RuntimeError,'ops_notices_require_export'):
            migration.migrate(schema,'0089_ops_actions',downgrade=True)
        with migration.connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT notice_plan_id FROM ops_actions').fetchone(),(ref.plan_id,))
        # Upgrade an existing pending intent, then bind a real authenticated
        # gateway receipt. No HTTP or operations command is executed here.
        from datetime import datetime,timezone
        from src.plugins.ai_chat.adapters.qq_official.binding import BindingReducer
        from src.plugins.ai_chat.adapters.qq_official.inbox import OfficialInboxStore
        from src.plugins.ai_chat.adapters.qq_official.ops_replies import OpsReplies
        from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
        from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
        migration.migrate(schema,'0091_ops_replies')
        with case.tx() as cursor:
            observe_ready(cursor,case.token,{'op':0,'t':'READY','d':{'session_id':'TEST_OPS_SESSION','user':{'id':'TEST_REAL_BOT','bot':True}}},case.now)
        binding = BindingReducer(case.ops.pepper,epoch_guard=assert_epoch,authority=case.authority,clock=lambda:case.now)
        store = OfficialInboxStore(database,jobs=case.queue,binding=binding,clock=lambda:case.now); self.addCleanup(store.close)
        store.ops_replies = OpsReplies(notices)
        store.accept({'op':0,'s':0,'t':'READY','d':{'session_id':'TEST_OPS_SESSION'}},token=case.token,session_id='TEST_OPS_SESSION')
        with patch.object(type(case.config),'secret',return_value='TEST_SECRET_'*4):
            outbox.prepare(case.token,ref.delivery_ids[0],send_mode='passive',msg_id='TEST_SOURCE',msg_seq=1)
            accepted = store.accept({'op':0,'s':1,'t':'C2C_MESSAGE_CREATE','d':{'id':'TEST_REPLY','content':'批准 '+proposal.code,
                'author':{'user_openid':case.actor.native_user_id},'timestamp':datetime.fromtimestamp(case.now,timezone.utc).isoformat()}},
                token=case.token,session_id='TEST_OPS_SESSION')
        with self.assertRaisesRegex(RuntimeError,'ops_replies_require_export'):
            migration.migrate(schema,'0090_ops_notices',downgrade=True)
        with migration.connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT approved_inbox_id,state FROM ops_actions').fetchone(),(accepted.inbox_id,'approved'))
