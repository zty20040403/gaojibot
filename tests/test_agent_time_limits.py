"""New-only physical limits and a two-hour question cap independent of job duration."""
import asyncio
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.plugins.ai_chat.background_jobs import BackgroundRunner
from src.plugins.ai_chat.platform_types import AccountKey,EndpointKey
from tests import test_background_jobs as fixture, test_qq_official_outbox_migration as migration


class AgentTimeLimitTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.BackgroundTests.setUp
    submit = fixture.BackgroundTests.submit

    def test_default_and_physical_override_never_change_existing_deadlines(self):
        account = AccountKey('qq-official','TEST_LIMITS','test')
        endpoint = EndpointKey(account,'group','TEST_GROUP')
        other = EndpointKey(account,'group','TEST_OTHER')
        with self.store.transaction() as cursor:
            cursor.execute('INSERT INTO conversation_settings(scope_key,agent_timeout_hours) VALUES (?,1)',(self.source['scope_key'],))
            cursor.execute('INSERT INTO conversation_settings(scope_key,agent_timeout_hours) VALUES (?,4)',(endpoint.scope.key,))
            self.assertEqual(self.store.hours(cursor,other),6)
            hours = self.store.hours(cursor,endpoint)
            self.assertEqual(hours,4)
        first = self.submit(hours=hours)
        self.assertEqual(first['deadline_at'],self.now+14400)
        self.assertEqual(self.submit(hours=1)['deadline_at'],first['deadline_at'])
        default = self.submit('TEST_DEFAULT')
        self.assertEqual(default['deadline_at'],self.now+21600)
        for hours in (0,7,True,1.0,'2'):
            with self.assertRaises(ValueError):
                self.submit('TEST_INVALID',hours=hours)

    async def test_question_is_two_hours_but_clipped_to_remaining_job_time(self):
        for hours,expected,description in ((6,7200,'前台两小时未回答'),(1,3600,'任务已到截止时间')):
            row = self.submit('TEST_WAIT_'+str(hours),hours=hours)
            started = self.now
            held = {}
            async def nothing(*args):
                pass
            async def session(row,job,question):
                held['question'] = question
                return SimpleNamespace(close=nothing)
            async def engine(row,session,**kwargs):
                self.assertIn(description,await held['question']('TEST question'))
                self.assertEqual(self.now-started,expected)
                self.assertEqual(self.store.get(row['job_id'])['deadline_at'],row['deadline_at'])
                return 'TEST report'
            async def sleep(seconds):
                self.assertLessEqual(seconds,2)
                self.now += 900  # Simulate elapsed wall time without waiting or mocking the DB.
            runner = BackgroundRunner(self.store,calls=self.calls,session_factory=session,save_report=nothing,
                ask_main=nothing,record_usage=nothing,logger=logging.getLogger(__name__),engine=engine,clock=lambda:self.now)
            with patch('src.plugins.ai_chat.background_jobs.asyncio',SimpleNamespace(
                    sleep=sleep,timeout=asyncio.timeout,CancelledError=asyncio.CancelledError)):
                await runner.execute(self.queue.get(row['job_id']))
            self.assertEqual(self.store.get(row['job_id'])['state'],'completed')
            self.assertIn(description,self.store.get(row['job_id'])['report'])

    def test_migration_keeps_settings_and_blocks_losing_explicit_limit(self):
        schema = migration.owned_schema(); self.addCleanup(migration.drop,schema)
        migration.migrate(schema,'0087_command_observations')
        with migration.connect(schema) as connection:
            connection.execute("INSERT INTO conversation_settings(scope_key,stopped,stop_generation) VALUES ('TEST_SCOPE',TRUE,8)")
        migration.migrate(schema,'0088_agent_time_limits')
        with migration.connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT stopped,stop_generation,agent_timeout_hours FROM conversation_settings').fetchone(),(True,8,6))
            connection.execute('UPDATE conversation_settings SET agent_timeout_hours=2')
        with self.assertRaisesRegex(RuntimeError,'agent_time_limits_require_export'):
            migration.migrate(schema,'0087_command_observations',downgrade=True)
        with migration.connect(schema) as connection:
            connection.execute('UPDATE conversation_settings SET agent_timeout_hours=6')
        migration.migrate(schema,'0087_command_observations',downgrade=True)
        with migration.connect(schema) as connection:
            self.assertEqual(connection.execute('SELECT stopped,stop_generation FROM conversation_settings').fetchone(),(True,8))
