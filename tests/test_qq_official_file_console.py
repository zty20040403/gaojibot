"""Real console session + PG source/lease + private CAS; no public link or QQ I/O."""
from contextlib import asynccontextmanager
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
import httpx

from src.bot_security.passwords import hash_password
from src.bot_security.service import MobileAuthorization
from src.bot_security.store import SecurityStore
from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.admin import AdminServices, register_admin
from src.plugins.ai_chat.adapters.qq_official.file_access import VerifiedStream
from src.plugins.ai_chat.adapters.qq_official.file_services import FileServiceConfig, FileServices
from src.plugins.ai_chat.adapters.qq_official.send_store import OfficialSendStore
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey
from tests.qq_official_file_fixture import FileFixture, PDF, KEY

PASSWORD = 'TEST-console-password-123456'


class ConsoleArtifactTests(FileFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory(prefix='qq-console-artifact-')
        self.addCleanup(temp.cleanup)
        self.private = Path(temp.name)
        self.deliveries = DeliveryStore(self.database, recover_interrupted=False)
        self.addCleanup(self.deliveries.close)
        self.outbox = OfficialSendStore(self.deliveries, admission=self.admission,
            pepper=b'TEST_CONSOLE_PEPPER' * 3, clock=lambda: self.now)
        self.security = SecurityStore(self.database, b'TEST_CONSOLE_SECURITY' * 2, clock=lambda: self.now)
        self.addCleanup(self.security.close)
        self.admin = self.security.bootstrap('console_admin', PASSWORD, '12345678')
        self.security.apply_account_change(self.admin, {'action': 'create', 'username': 'viewer',
            'password_hash': hash_password(PASSWORD), 'role': 'member', 'qq_id': None})

    async def asyncSetUp(self):
        self.calls = DatabaseCalls()
        self.runtime = SimpleNamespace(_calls=self.calls, deliveries=self.deliveries,
            jobs=self.jobs, outbox=self.outbox, _stopping=False, _token=self.token,
            store=SimpleNamespace(admitted=self.admission, clock=lambda: self.now))
        # Deliberately no native proof, link origin/reachability, or guest. An
        # authenticated console read must not require a platform delivery route.
        self.service = FileServices(self.runtime, self.token, None,
            FileServiceConfig(self.private, None, min_free_bytes=0))
        self.item = self.service.artifacts.register(self.source, content=PDF,
            filename='测试报告.pdf', logical_request_key=KEY)
        self.mobile = MobileAuthorization(self.security, sender=None, bot_selector=lambda: None)
        self.app = FastAPI()
        register_admin(self.app, AdminServices(version='test', started_at=1,
            database=self.database, settings=SimpleNamespace(admin_origin='https://test'),
            mobile_authorization=self.mobile, official_file_service=lambda: self.service))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
            base_url='https://test', headers={'Origin': 'https://test'})
        self.path = '/bot-admin/api/v1/qq-official/artifacts/' + self.item.artifact_id + '/download'

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.service.close()
        await self.calls.close()

    async def login(self, username='console_admin'):
        result = await self.client.post('/bot-admin/api/v1/auth/login',
            json={'username': username, 'password': PASSWORD})
        self.assertEqual(result.status_code, 200, result.text)

    async def test_admin_get_and_head_use_real_cas_without_route_or_delivery_mutation(self):
        await self.login()
        before = self.query('SELECT * FROM qq_official_file_deliveries')
        jobs = self.query('SELECT job_id,status FROM durable_jobs ORDER BY job_id')
        for path in (self.path, self.path.replace('/api/v1/', '/api/')):
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.content, PDF)
            self.assertEqual(response.headers['x-content-sha256'], hashlib.sha256(PDF).hexdigest())
            self.assertEqual(response.headers['content-type'], 'application/pdf')
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
            self.assertTrue(response.headers['content-disposition'].startswith('attachment;'))
            self.assertIn("filename*=UTF-8''%E6%B5%8B", response.headers['content-disposition'])
            self.assertNotIn(str(self.private), str(response.headers))
            head = await self.client.head(path)
            self.assertEqual(head.status_code, 200)
            self.assertEqual(head.content, b'')
            self.assertEqual(head.headers['content-length'], str(len(PDF)))
        self.assertEqual(self.query('SELECT * FROM qq_official_file_deliveries'), before)
        self.assertEqual(self.query('SELECT job_id,status FROM durable_jobs ORDER BY job_id'), jobs)
        self.assertEqual(self.query('SELECT * FROM qq_official_file_publications'), [])
        self.assertEqual(self.query('SELECT * FROM delivery_attempts'), [])

    async def test_anonymous_member_and_old_bearer_cannot_open_artifact(self):
        with patch.object(self.service, 'console_artifact', side_effect=AssertionError('must not open')):
            self.assertEqual((await self.client.get(self.path)).status_code, 401)
            self.assertEqual((await self.client.get(self.path, headers={'Authorization': 'Bearer TEST_OLD_TOKEN'})).status_code, 401)
            await self.login('viewer')
            self.assertEqual((await self.client.get(self.path)).status_code, 403)

    async def test_query_paths_and_public_tokens_never_select_a_source(self):
        await self.login()
        with patch.object(self.service, 'console_artifact', side_effect=AssertionError('must not open')):
            for query in ('?path=/etc/passwd', '?account=TEST_OTHER', '?token=TEST_PUBLIC'):
                self.assertEqual((await self.client.get(self.path + query)).status_code, 404)
            self.assertEqual((await self.client.get(self.path.replace(self.item.artifact_id, 'not-an-id'))).status_code, 404)
            self.assertEqual((await self.client.get(self.path, headers={'Range': 'bytes=0-1'})).status_code, 416)

    async def test_missing_corrupt_and_deleted_bytes_are_unavailable_without_paths(self):
        await self.login()
        missing = await self.client.get(self.path.replace(self.item.artifact_id, 'f' * 32))
        self.assertEqual(missing.status_code, 404)
        # Same-size corruption must fail the full immutable hash before headers.
        path = self.service.artifacts.blobs.path(self.item.sha256)
        original = path.read_bytes()
        mode = path.stat().st_mode & 0o777
        path.chmod(0o600)
        path.write_bytes(b'X' * len(original))
        path.chmod(mode)
        corrupt = await self.client.get(self.path)
        self.assertEqual(corrupt.status_code, 404)
        self.assertNotIn(str(self.private), corrupt.text)
        path.chmod(0o600)
        path.write_bytes(original)
        path.chmod(mode)
        with self.tx() as cursor:
            cursor.execute("UPDATE qq_official_file_artifacts SET bytes_state='deleted',bytes_deleted_at=? WHERE artifact_id=?", (self.now, self.item.artifact_id))
        self.assertEqual((await self.client.get(self.path)).status_code, 404)

    async def test_revocation_and_expiry_do_not_grant_access(self):
        await self.login()
        self.admitted = False
        self.assertEqual((await self.client.get(self.path)).status_code, 404)
        self.admitted = True
        with self.tx() as cursor:
            cursor.execute("UPDATE qq_official_endpoints SET binding_state='revoked' WHERE endpoint_key=?", (self.endpoint.key,))
        self.assertEqual((await self.client.get(self.path)).status_code, 404)
        with self.tx() as cursor:
            cursor.execute("UPDATE qq_official_endpoints SET binding_state='active' WHERE endpoint_key=?", (self.endpoint.key,))
        with self.tx() as cursor:
            cursor.execute('UPDATE qq_official_file_artifacts SET retain_until=? WHERE artifact_id=?', (self.now, self.item.artifact_id))
        self.assertEqual((await self.client.head(self.path)).status_code, 404)

    async def test_real_other_account_service_cannot_read_this_accounts_artifact(self):
        await self.login()
        account = AccountKey('qq-official', self.account.app_id + '_OTHER', 'test')
        self.provision(EndpointKey(account, 'group', 'TEST_OTHER_GROUP'))
        token = self.lease(account).acquire()
        runtime = SimpleNamespace(**{**vars(self.runtime), '_token': token})
        original = self.service
        self.service = FileServices(runtime, token, None, FileServiceConfig(self.private, None, min_free_bytes=0))
        try:
            self.assertEqual((await self.client.get(self.path)).status_code, 404)
        finally:
            await self.service.close()
            self.service = original

    async def test_lost_real_account_lease_cannot_read(self):
        await self.login()
        self.account_lease.close()
        self.assertEqual((await self.client.get(self.path)).status_code, 404)

    async def test_transport_failure_and_finished_source_preserve_console_read(self):
        await self.login()
        with self.tx() as cursor:
            cursor.execute("UPDATE qq_official_file_deliveries SET state='failed',reason_code='no_delivery_route' WHERE artifact_id=?", (self.item.artifact_id,))
        self.assertEqual((await self.client.get(self.path)).content, PDF)
        with self.tx() as cursor:
            cursor.execute("UPDATE qq_official_turn_executions SET state='cancelled' WHERE execution_id=?", (self.handle.execution_id,))
        self.assertEqual((await self.client.get(self.path)).content, PDF)

    async def test_session_revocation_between_chunks_stops_and_closes_source(self):
        await self.login()
        original = self.service.console_artifact
        closed = []
        cookie = self.client.cookies.get('gaoji_session')

        @asynccontextmanager
        async def split(artifact_id):
            try:
                async with original(artifact_id) as (artifact, source):
                    async def chunks():
                        async for block in source.chunks:
                            yield block[:10]
                            self.security.logout(cookie)
                            yield block[10:]
                    yield artifact, VerifiedStream(source.sha256, source.size, chunks())
            finally:
                closed.append(True)

        with patch.object(self.service, 'console_artifact', new=split):
            with self.assertRaisesRegex(RuntimeError, 'console_download_unavailable'):
                await self.client.get(self.path)
        self.assertEqual(closed, [True])
        self.assertEqual(self.query('SELECT * FROM delivery_attempts'), [])
