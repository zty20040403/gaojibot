"""Real isolated PG application startup; only network and sandbox are local fakes."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

import httpx

from fastapi import FastAPI
from src.bot_security.store import SecurityStore, SecurityError
from src.plugins.ai_chat.application_runtime import OfficialApplicationRuntime
from src.plugins.ai_chat.application_security import ConsoleAuthorization
from src.plugins.ai_chat.config import settings
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey, ActorKey
from src.plugins.ai_chat.adapters.qq_official.admin_bindings import AdminBindingStore
from src.plugins.ai_chat.adapters.qq_official.validation import Denied
from src.plugins.ai_chat.adapters.qq_official.runtime import OfficialRuntime, open_official_protocol
from src.qq_official.testing.fake_platform import FakePlatform
from tests.postgres_fixture import isolated_postgres_for_test


def private(path,value):
    path.touch(mode=0o600);path.write_bytes(value);return str(path)


class NativeConsoleTests(unittest.TestCase):
    def setUp(self):
        self.database=isolated_postgres_for_test(self)
        self.store=SecurityStore(self.database,b'TEST_LOCAL_CONSOLE_SECRET_32_BYTES!')
        self.addCleanup(self.store.close)

    def test_local_bootstrap_login_never_creates_transport_or_owner(self):
        admin=self.store.bootstrap_local_admin('localadmin','TEST safe password 12345')
        self.assertIsNone(admin['qq_id'])
        login=self.store.login('localadmin','TEST safe password 12345','127.0.0.1')
        self.assertEqual(self.store.session(login['session'],login['csrf'])['account_id'],admin['account_id'])
        account=AccountKey('qq-official','TEST_APP','test');endpoint=EndpointKey(account,'private','TEST_USER')
        actor=ActorKey(account,'c2c_user','','TEST_USER')
        with self.store.transaction() as cursor:
            self.assertEqual(cursor.execute('SELECT identity_mode FROM admin_accounts').fetchone()[0],'local_admin')
            self.assertEqual(cursor.execute('SELECT count(*) FROM qq_official_admin_bindings').fetchone()[0],0)
            self.assertEqual(cursor.execute('SELECT count(*) FROM qq_official_owner_grants').fetchone()[0],0)
            with self.assertRaises(Denied):AdminBindingStore().resolve_admin(cursor,endpoint=endpoint,actor=actor)
        with self.assertRaises(SecurityError):self.store.bootstrap_local_admin('overwrite','TEST safe password 12345')
        with self.assertRaises(SecurityError):self.store.bootstrap('overwrite','TEST safe password 12345','100001')

    def test_add_local_admin_requires_existing_admin_and_never_overwrites(self):
        with self.assertRaises(SecurityError):self.store.add_local_admin('second','TEST safe password 12345')
        self.store.bootstrap_local_admin('first','TEST safe password 12345')
        second=self.store.add_local_admin('second','TEST other password 12345')
        self.assertIsNone(second['qq_id'])
        with self.assertRaises(SecurityError):self.store.add_local_admin('second','TEST replacing password 12345')
        with self.store.transaction() as cursor:
            self.assertEqual(cursor.execute('SELECT count(*) FROM qq_official_admin_bindings').fetchone()[0],0)
            cursor.execute('UPDATE admin_accounts SET enabled=0')
        with self.assertRaises(SecurityError):self.store.add_local_admin('third','TEST safe password 12345')

    def test_legacy_bootstrap_keeps_required_numeric_identity(self):
        with self.assertRaises(SecurityError):self.store.bootstrap('legacyadmin','TEST safe password 12345',None)
        self.store.bootstrap('legacyadmin','TEST safe password 12345','100001')
        with self.store.transaction() as cursor:
            self.assertEqual(cursor.execute('SELECT identity_mode FROM admin_accounts').fetchone()[0],'legacy_qq')
        self.assertIsNotNone(self.store.account_for_qq('100001'))
        with self.assertRaises(SecurityError):self.store.bootstrap_local_admin('another','TEST safe password 12345')


class NativeApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_epoch_zero_security_reconnect_shutdown(self):
        database=isolated_postgres_for_test(self)
        with TemporaryDirectory() as directory:
            root=Path(directory)
            async with FakePlatform() as fake:
                account=AccountKey('qq-official',fake.app_id,'test')
                config={'version':1,'account':account.key,'onebot_accounts':[],
                    'files':{'private_directory':str(root/'private-files'),'native_routes':[],
                        'generation_enabled':False,'min_free_bytes':0}}
                env={'AI_ONEBOT_ENABLED':'false','AI_QQ_OFFICIAL_ENABLED':'true',
                    'AI_QQ_OFFICIAL_HOST_CONFIG_FILE':private(root/'host.json',json.dumps(config).encode()),
                    'AI_QQ_OFFICIAL_APP_ID_FILE':private(root/'app',fake.app_id.encode()),
                    'AI_QQ_OFFICIAL_APP_SECRET_FILE':private(root/'appsecret',fake.app_secret.encode()),
                    'AI_QQ_OFFICIAL_BIND_PEPPER_FILE':private(root/'pepper',b'a'*64),
                    'AI_QQ_OFFICIAL_OWNER_OPENIDS':'TEST_OWNER','AI_QQ_OFFICIAL_API_BASE':fake.base_url}
                chosen=replace(settings,postgres_dsn=database.dsn,postgres_schema=database.schema,
                    admin_secret_file=private(root/'console-secret',b'TEST_CONSOLE_SECRET_32_BYTES_LONG!'),
                    admin_enabled=True,sandbox_enabled=False,
                    ledger_enabled=True,turn_journal_enabled=True,
                    outbox_enabled=True,quota_enabled=True,durable_jobs_enabled=True,
                    semantic_enabled=False,media_enabled=False,observability_enabled=False,
                    archive_enabled=False)
                @asynccontextmanager
                async def protocol(*args,**kwargs):
                    async with open_official_protocol(*args,**kwargs) as gateway:
                        run=gateway.run
                        async def authenticated_test_ready(callback):
                            async def forward(frame):
                                if frame.get('t')=='READY':
                                    frame={**frame,'d':{**frame['d'],'user':{'id':'TEST_REAL_BOT','bot':True}}}
                                await callback(frame)
                            await run(forward)
                        gateway.run=authenticated_test_ready
                        yield gateway
                epoch_errors=[]
                def runtime(*args,**kwargs):
                    instance=OfficialRuntime(*args,**kwargs,allow_test_server=True,protocol_factory=protocol,
                        probe_seconds=.05,retry_seconds=.05)
                    serve=instance._serve
                    async def checked(*args,**kwargs):
                        try:return await serve(*args,**kwargs)
                        except Exception as error:
                            epoch_errors.append(error)
                            raise
                    instance._serve=checked
                    return instance
                app=FastAPI();host=OfficialApplicationRuntime(chosen,state_dir=root/'state',cache_dir=root/'cache',
                    project_root=root,env=env,logger=logging.getLogger('test-native-startup'))
                # Private runner is synthetic; all PG stores, current account,
                # owner, source, project factories and sender are real.
                with patch('src.plugins.ai_chat.adapters.qq_official.runtime.OfficialRuntime',side_effect=runtime):
                    try:
                        await host.start(app)
                        try:await asyncio.wait_for(fake.wait_ready(),10)
                        except TimeoutError:
                            if epoch_errors:raise epoch_errors[0]
                            raise
                        async with asyncio.timeout(10):
                            while host.context.official_runtime.file_service is None:
                                await asyncio.sleep(.02)
                        context=host.context;official=context.official_runtime;token=official._token
                        async with asyncio.timeout(10):
                            while not (await context.official_observability.sample())['healthy']:
                                await asyncio.sleep(.02)
                        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                            response=await client.get('/health/qq-official')
                        self.assertEqual(response.status_code,200)
                        self.assertNotIn('TEST_',response.text)
                        self.assertIsInstance(context.mobile_authorization,ConsoleAuthorization)
                        self.assertEqual(context.migration_service.baselines,{})
                        self.assertIsNotNone(official.approval_service)
                        self.assertIs(official.approval_service.token,token)
                        self.assertIs(official.file_service.token,token)
                        self.assertNotIn('agent.sandbox_exec',context.job_worker.registered_kinds)
                        context.mobile_authorization.store.bootstrap_local_admin('consoleadmin','TEST safe password 12345')
                        with self.assertRaises(SecurityError):await context.mobile_authorization.propose({},kind='account',payload={},summary='test')
                        await fake.emit('C2C_MESSAGE_CREATE',{'id':'TEST_STARTUP_CONTROL','author':{'user_openid':'TEST_OWNER'},
                            'content':'/whoami','timestamp':datetime.now(timezone.utc).isoformat()})
                        async with asyncio.timeout(10):
                            while not fake.received:await asyncio.sleep(.02)
                        self.assertEqual(len(fake.received),1)
                        await fake.close_connections()
                        await asyncio.wait_for(fake.wait_ready(),10)
                        self.assertTrue(fake.resume_count or fake.identify_count>=2)
                        with context.delivery_store.transaction() as cursor:
                            self.assertEqual(cursor.execute('SELECT count(*) FROM migration_runtime_baselines').fetchone()[0],0)
                            self.assertEqual(cursor.execute('SELECT count(*) FROM qq_official_admin_bindings').fetchone()[0],0)
                    finally:await host.close()
                self.assertFalse(host.ready)
                self.assertTrue(host.context._closed)
                self.assertFalse(fake.connections)
