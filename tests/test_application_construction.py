"""Environment snapshots and transactional application construction."""
import os
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from fastapi import FastAPI
from src.bot_storage import PostgresDatabase
from src.bot_storage.disposable import IsolatedPostgres
from src.bot_security.store import SecurityError
from src.plugins.ai_chat.application_runtime import OfficialApplicationRuntime, create_application
from src.plugins.ai_chat.config import Settings, settings
from src.plugins.ai_chat.application_construction import build_owned_context
from src.plugins.ai_chat.model_catalog import ModelCatalog

MODE = {'AI_ONEBOT_ENABLED':'false', 'AI_QQ_OFFICIAL_ENABLED':'true'}

class ConfigurationTests(unittest.TestCase):
    def test_explicit_application_environment_is_used_by_shared_settings(self):
        with patch.dict(os.environ, {'AI_ADMIN_ENABLED':'false'}, clear=False):
            app = create_application(env={**MODE, 'AI_ADMIN_ENABLED':'true'})
        self.assertTrue(app.state.gaoji_runtime.settings.admin_enabled)

    def test_environment_snapshot_controls_paths_nested_parsers_and_provider_secrets(self):
        supplied = {**MODE, 'AI_STATE_DIR':'/TEST/state', 'AI_CACHE_DIR':'/TEST/cache',
            'AI_POSTGRES_DSN':'TEST_EXPLICIT_DSN', 'AI_POSTGRES_SCHEMA':'test_explicit_schema',
            'AI_ADMIN_SECRET_FILE':'/TEST/explicit-secret', 'AI_ENABLED_GROUPS':'41,42',
            'AI_SANDBOX_CPUS':'3', 'AI_MODEL_DEFAULT_PROFILE':'test',
            'TEST_MODEL_SECRET':'TEST_EXPLICIT_SECRET',
            'AI_MODEL_PROFILES_JSON':json.dumps({'profiles': {'test': {
                'provider':'test', 'protocol':'openai-chat','model':'test-model',
                'base_url':'https://test.invalid/v1','api_key_env':'TEST_MODEL_SECRET'}}})}
        before = dict(os.environ)
        app = create_application(env=supplied)
        host = app.state.gaoji_runtime
        self.assertEqual(host.state_dir,Path('/TEST/state'))
        self.assertEqual(host.cache_dir,Path('/TEST/cache'))
        self.assertEqual(host.settings.postgres_dsn,'TEST_EXPLICIT_DSN')
        self.assertEqual(host.settings.postgres_schema,'test_explicit_schema')
        self.assertEqual(host.settings.admin_secret_file,'/TEST/explicit-secret')
        self.assertEqual(host.settings.enabled_groups,{41,42})
        self.assertEqual(host.settings.sandbox_cpus,3)
        self.assertEqual(ModelCatalog.from_settings(host.settings,environ=host.env).resolve('test').api_key,
                         'TEST_EXPLICIT_SECRET')
        supplied['TEST_MODEL_SECRET']='TEST_CHANGED'
        self.assertEqual(host.env['TEST_MODEL_SECRET'],'TEST_EXPLICIT_SECRET')
        self.assertEqual(dict(os.environ),before)

    def test_parallel_explicit_settings_do_not_change_default_environment(self):
        with patch.dict(os.environ,{'AI_ADMIN_ENABLED':'false','AI_SEARCH_MAX_RESULTS':'211'}):
            original = dict(os.environ)
            def make(index):
                env={**MODE,'AI_ADMIN_ENABLED':str(index%2==1),'AI_SEARCH_MAX_RESULTS':str(1000+index),
                     'AI_STATE_DIR':f'/TEST/state-{index}'}
                host=create_application(env=env).state.gaoji_runtime
                return host.settings.admin_enabled,host.settings.search_max_results,host.state_dir
            with ThreadPoolExecutor(max_workers=4) as pool:
                rows=list(pool.map(make,range(12)))
            for index,row in enumerate(rows):
                self.assertEqual(row,(index%2==1,1000+index,Path(f'/TEST/state-{index}')))
            self.assertEqual(Settings.from_env().search_max_results,211)
            self.assertEqual(dict(os.environ),original)

class OwnedConstructionTests(unittest.IsolatedAsyncioTestCase):
    async def test_rollback_closes_on_same_loop_and_preserves_original_error(self):
        loop=asyncio.get_running_loop();seen=[];failure=ValueError('PRIVATE_FAILURE')
        class Resource:
            async def close(self):
                self_outer.assertIs(asyncio.get_running_loop(),loop)
                seen.append('async')
                raise RuntimeError('PRIVATE_CLEANUP')
        class Store:
            def close(self):seen.append('store')
        self_outer=self;logger=Mock()
        def build(*args,_construction,**kwargs):
            _construction.own(Store());_construction.own(Resource());raise failure
        with self.assertRaises(ValueError) as result:
            await build_owned_context(build,logger=logger)
        self.assertIs(result.exception,failure)
        self.assertEqual(seen,['async','store'])
        self.assertNotIn('PRIVATE_',str(logger.mock_calls))

    async def test_second_cancellation_waits_for_original_loop_cleanup(self):
        entered,release=asyncio.Event(),asyncio.Event();seen=[]
        original=asyncio.CancelledError()
        class Resource:
            async def close(self):
                entered.set();await release.wait();seen.append('closed')
        def build(*args,_construction,**kwargs):
            _construction.own(Resource());raise original
        task=asyncio.create_task(build_owned_context(build,logger=Mock()))
        await entered.wait();task.cancel();await asyncio.sleep(0)
        self.assertFalse(task.done());release.set()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(seen,['closed'])

    async def test_direct_sync_failure_closes_pool_and_preserves_original_control(self):
        from src.plugins.ai_chat.runtime import build_app_context
        for failure in (ValueError('original'),asyncio.CancelledError()):
            with self.subTest(kind=type(failure).__name__), TemporaryDirectory() as directory:
                database=Mock()
                database.require_revision.side_effect=failure
                database.close.side_effect=RuntimeError('PRIVATE_CLEANUP')
                tasks=asyncio.all_tasks()
                with patch('src.plugins.ai_chat.runtime.PostgresDatabase',return_value=database):
                    with self.assertRaises(type(failure)) as raised:
                        build_app_context(replace(settings,postgres_dsn='TEST_NOT_CONNECTED'),
                            state_dir=Path(directory)/'state',project_root=Path(directory),logger=Mock(),
                            historian_generator=Mock(),dream_generator=Mock(),evidence_provider=Mock())
                self.assertIs(raised.exception,failure)
                database.close.assert_called_once()
                self.assertEqual(asyncio.all_tasks(),tasks)

    async def test_success_transfers_without_closing_or_new_background_tasks(self):
        seen=[];context=object()
        class Resource:
            async def close(self):seen.append('unexpected')
        def build(*args,_construction,**kwargs):
            _construction.own(Resource());return context
        tasks=asyncio.all_tasks()
        self.assertIs(await build_owned_context(build,logger=Mock()),context)
        self.assertEqual(seen,[])
        self.assertEqual(asyncio.all_tasks(),tasks)

class FailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_console_secret_closes_actual_application_pool(self):
        fixture = IsolatedPostgres(os.environ['TEST_POSTGRES_DSN'])
        self.addCleanup(fixture.close)
        captured = []
        def build_database(*args, **kwargs):
            database = PostgresDatabase(*args, **kwargs)
            captured.append(database)
            self.addCleanup(database.close)
            return database
        with TemporaryDirectory() as directory:
            root = Path(directory)
            chosen = replace(settings, postgres_dsn=fixture.dsn, postgres_schema=fixture.schema,
                admin_secret_file=str(root/'missing-secret'), admin_enabled=True,
                model_default_profile='test',local_model_profile='test',
                model_profiles_json=json.dumps({'test': {'provider':'test','protocol':'openai-chat',
                    'model':'test-model','base_url':'https://test.invalid/v1','api_key_env':'TEST_MODEL_SECRET'}}),
                ledger_enabled=True, semantic_enabled=False, subagents_enabled=False,
                sandbox_enabled=False, media_enabled=False, archive_enabled=False, observability_enabled=False)
            host = OfficialApplicationRuntime(chosen,state_dir=root/'state',cache_dir=root/'cache',
                project_root=root,env={**MODE,'TEST_MODEL_SECRET':'TEST_EXPLICIT_SECRET'},logger=Mock())
            closed=[];loop=asyncio.get_running_loop();local_clients=[];catalogs=[]
            from src.plugins.ai_chat.local_model import LocalModelRuntime
            def local_factory(*args,**kwargs):
                client=LocalModelRuntime(*args,**kwargs);local_clients.append(client);return client
            class Client:
                async def close(self):
                    self_outer.assertIs(asyncio.get_running_loop(),loop)
                    closed.append('llm')
            self_outer=self
            def gateway_factory(*args,**kwargs):
                catalogs.append(kwargs['catalog']);return Client()
            from src.plugins.ai_chat.ledger import MessageLedger
            original_close=MessageLedger.close
            def close_ledger(instance):
                closed.append('ledger');return original_close(instance)
            with patch('src.plugins.ai_chat.runtime.PostgresDatabase', side_effect=build_database), \
                 patch('src.plugins.ai_chat.runtime.LLMGateway',side_effect=gateway_factory), \
                 patch('src.plugins.ai_chat.runtime.LocalModelRuntime',side_effect=local_factory), \
                 patch.object(MessageLedger,'close',close_ledger):
                with self.assertRaisesRegex(SecurityError, 'console_secret_unavailable'):
                    await host.start(FastAPI())
            self.assertEqual(closed,['ledger','llm'])
            self.assertEqual(catalogs[0].resolve('test').api_key,'TEST_EXPLICIT_SECRET')
            self.assertEqual(len(local_clients),1)
            self.assertTrue(local_clients[0]._client.is_closed)
            await host.close()
            self.assertEqual(len(captured),1)
            self.assertTrue(captured[0]._pool.closed)

if __name__ == '__main__': unittest.main(verbosity=2)
