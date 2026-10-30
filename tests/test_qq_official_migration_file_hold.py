"""Real source/console/file worker; a migration holds only untouched files."""
from types import SimpleNamespace
from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official.file_artifacts import file_job_kind
from src.plugins.ai_chat.adapters.qq_official.file_dispatcher import OfficialFileDispatcher
from src.plugins.ai_chat.workers.durable_jobs import DurableJobWorker
from tests import test_qq_official_migration_verification_host as fixture


class MigrationFileHoldTests(fixture.VerificationHostFixture):
    async def plan_history(self):
        self.migration_plan = await self.service.console.mutate(
            cookie=self.mapping_cookie, csrf=self.mapping_csrf, action='plan',
            request=dict(source_endpoint=self.source.key, source_scope=self.source_scope.key,
                         target_endpoint=self.endpoint.key, items=[], deadline=self.now+300))

    async def action(self, action):
        return await self.service.console.mutate(cookie=self.mapping_cookie, csrf=self.mapping_csrf,
            action=action, request={'migration_id': self.migration_plan['migration_id']},
            preview_digest=self.migration_plan['preview_digest'])

    def file_worker(self):
        dispatcher = OfficialFileDispatcher(self.token, self.files, self.payloads, self.outbox,
            self.rest, calls=self.calls, lease_check=lambda: True,
            check_delivery_ready=lambda *_: True, clock=lambda: self.now)
        worker = DurableJobWorker(self.jobs, database_calls=self.calls,
            logger=SimpleNamespace(error=lambda *_: None, warning=lambda *_: None,
                                   info=lambda *_: None))
        worker.register(file_job_kind(self.account), dispatcher)
        return worker

    async def tick(self, worker):
        with patch('src.plugins.ai_chat.storage.jobs.time', SimpleNamespace(time=lambda: self.now)):
            return await worker.run_once()

    def delivery(self, artifact):
        return dict(self.query('SELECT * FROM qq_official_file_deliveries WHERE artifact_id=?',
                               (artifact.artifact_id,))[0])

    def job(self, artifact):
        return self.jobs.get(self.delivery(artifact)['job_id'])

    async def test_planned_real_artifact_waits_then_same_job_verifies_and_commits(self):
        await self.plan_history()
        # Existing fixture uses real authenticated TxA/TxB, M4 execution and
        # ArtifactStore registration with actual PDF bytes; the route is planned.
        artifact = self.artifact()
        worker = self.file_worker()
        original = dict(self.query('SELECT * FROM qq_official_file_artifacts WHERE artifact_id=?',
                                   (artifact.artifact_id,))[0])
        job_id = self.job(artifact).job_id
        for action in (None, 'freeze', 'drain', 'switch', 'verify'):
            if action:
                await self.action(action)
            self.assertEqual(await self.tick(worker), 1)
            job = self.job(artifact)
            self.assertEqual((job.job_id, job.status, job.attempts), (job_id, 'pending', 0))
            self.assertEqual(job.last_error, 'migration_file_verification_pending')
            self.assertEqual(self.delivery(artifact)['state'], 'stored')
            self.assertEqual(self.fake.media_calls, [])
            self.assertEqual(self.fake.received, [])
            self.assertFalse(self.query('SELECT * FROM qq_official_file_stage_attempts'))
            self.now += 15
        response = await self.http_request('file', artifact.artifact_id)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {'state': 'awaiting_file', 'plan_id': None})
        self.assertEqual(await self.tick(worker), 1)
        self.assertEqual(self.job(artifact).job_id, job_id)
        self.assertEqual(self.job(artifact).status, 'succeeded')
        self.assertEqual(len(self.fake.media_calls), 1)
        plan = self.delivery(artifact)['active_plan_id']
        delivery_id = self.query('SELECT delivery_id FROM deliveries WHERE plan_id=?', (plan,))[0]['delivery_id']
        self.assertEqual((await self.sender.send_delivery(delivery_id)).status, 'committed')
        text = await self.http_request()
        self.assertEqual(text.status_code, 200, text.text)
        text_id = self.query('SELECT delivery_id FROM deliveries WHERE plan_id=?',
                             (text.json()['plan_id'],))[0]['delivery_id']
        self.assertEqual((await self.sender.send_delivery(text_id)).status, 'committed')
        await self.session.maintain()
        self.assertEqual({r['kind'] for r in self.query('SELECT kind FROM migration_verification_receipts')},
                         {'file', 'text'})
        await self.action('complete')
        self.assertEqual(self.query('SELECT phase FROM migration_routes')[0]['phase'], 'completed')
        current = dict(self.query('SELECT * FROM qq_official_file_artifacts WHERE artifact_id=?',
                                 (artifact.artifact_id,))[0])
        for key in ('pending_deadline_at', 'source_native_message_id', 'source_canonical_message_id',
                    'endpoint_generation', 'route_epoch', 'source_execution_id'):
            self.assertEqual(current[key], original[key])
        self.assertLessEqual(current['retain_until'], original['retain_until'])
        self.assertEqual(self.delivery(artifact)['state'], 'message_accepted')

    async def test_original_retention_expiry_stops_hold_without_upload_or_deadline_refresh(self):
        await self.plan_history()
        artifact = self.artifact()
        worker = self.file_worker()
        expires = self.now + 4
        self.query('UPDATE qq_official_file_artifacts SET retain_until=? WHERE artifact_id=?',
                   (expires, artifact.artifact_id))
        self.assertEqual(await self.tick(worker), 1)
        self.assertEqual(self.job(artifact).next_attempt_at, expires)
        self.assertEqual(self.job(artifact).attempts, 0)
        self.now = expires
        self.assertEqual(await self.tick(worker), 1)
        self.assertEqual(self.delivery(artifact)['state'], 'cancelled')
        self.assertEqual(self.job(artifact).status, 'succeeded')
        self.assertFalse(self.fake.media_calls)
        self.assertEqual(self.query('SELECT retain_until FROM qq_official_file_artifacts')[0]['retain_until'], expires)

    async def test_existing_upload_unknown_is_neither_held_nor_replayed(self):
        artifact = self.artifact()
        worker = self.file_worker()
        self.fake.drop_response = True
        self.assertEqual(await self.tick(worker), 1)
        self.assertEqual(self.delivery(artifact)['state'], 'upload_unknown')
        before = self.delivery(artifact)
        stage = [dict(r) for r in self.query('SELECT * FROM qq_official_file_stage_attempts')]
        self.assertEqual(len(self.fake.media_calls), 1)
        await self.plan_history()
        self.now += 15
        self.assertEqual(await self.tick(worker), 0)
        self.assertEqual(self.delivery(artifact), before)
        self.assertEqual([dict(r) for r in self.query('SELECT * FROM qq_official_file_stage_attempts')], stage)
        self.assertEqual(len(self.fake.media_calls), 1)
        self.assertFalse(self.fake.received)
