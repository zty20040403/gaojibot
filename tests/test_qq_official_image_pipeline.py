"""M7 actual image pipeline over a disposable PostgreSQL schema.

Uses the existing fixture's independent random schema and migrated image
constraints, actual FileServices route evidence, file jobs/vault/M3 and loopback.
"""
from dataclasses import replace
import hashlib
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official.file_artifacts import file_job_kind
from src.plugins.ai_chat.adapters.qq_official.file_services import FileServices, FileServiceConfig, NativeRouteEvidence
from src.plugins.ai_chat.adapters.qq_official.native_media import CONTRACT
from src.plugins.ai_chat.workers.durable_jobs import DurableJobWorker, JobDeferred
from tests.qq_official_file_fixture import FileFixture
from tests import test_qq_official_file_dispatcher as helpers
from tests.test_qq_official_image_formats import png, jpeg


class ImagePipelineTests(FileFixture, unittest.IsolatedAsyncioTestCase):
    # Fixture methods only: do not inherit/re-run the legacy test methods.
    policy = helpers.FileDispatcherTests.policy
    ensure_current = helpers.FileDispatcherTests.ensure_current
    unused_download = helpers.FileDispatcherTests.unused_download
    publisher_factory = helpers.FileDispatcherTests.publisher_factory
    make_dispatcher = helpers.FileDispatcherTests.make_dispatcher
    prepare_file = helpers.FileDispatcherTests.prepare_file
    claim_file = helpers.FileDispatcherTests.claim_file
    file_row = helpers.FileDispatcherTests.file_row
    stages = helpers.FileDispatcherTests.stages
    send_result = helpers.FileDispatcherTests.send_result

    def setUp(self):
        super().setUp()
        self.image_bytes, self.image_name = png(), 'sticker.png'

    async def asyncSetUp(self):
        await helpers.FileDispatcherTests.asyncSetUp(self)
        self.proof = NativeRouteEvidence(self.endpoint, 1, 1, 1, 3500000,
            self.now - 1, self.now + 600, CONTRACT, 1)
        runtime = SimpleNamespace(_calls=self.calls, deliveries=self.deliveries, jobs=self.jobs,
            outbox=self.outbox, _token=self.token, _stopping=False,
            store=SimpleNamespace(admitted=self.admission, clock=lambda:self.now))
        config = FileServiceConfig(Path(self.temporary.name) / 'configured', None,
            route_authority=lambda *_: True, min_free_bytes=0,
            native_media_evidence=lambda cursor, endpoint, kind: self.proof if self.proof.file_type == kind else None)
        self.service = FileServices(runtime, self.token, self.rest, config)
        self.files, self.payloads, self.dispatcher = self.service.artifacts, self.service.payloads, self.service.dispatcher
        self.outbox.protected_payloads = self.payloads

    async def asyncTearDown(self):
        await self.service.close()
        await helpers.FileDispatcherTests.asyncTearDown(self)

    def put(self, **kwargs):
        return self.files.register(self.source, content=self.image_bytes, filename=self.image_name,
            logical_request_key=hashlib.sha256(b'TEST_STICKER_REQUEST').hexdigest(), **kwargs)

    async def run_worker(self):
        worker = DurableJobWorker(self.jobs, logger=SimpleNamespace(error=lambda *_:None, warning=lambda *_:None),
                                  database_calls=self.calls)
        worker.register(file_job_kind(self.account), self.dispatcher)
        with patch('src.plugins.ai_chat.storage.jobs.time', SimpleNamespace(time=lambda:self.now)):
            return await worker.run_once()

    async def test_png_real_file_services_upload_type_one_and_m3_applied(self):
        self.prepare_file()
        self.assertEqual(await self.run_worker(), 1)
        self.assertEqual(self.file_row()['state'], 'handed_off')
        self.assertEqual(self.fake.media_calls[0][3]['file_type'], 1)
        self.assertIs(self.fake.media_calls[0][3]['srv_send_msg'], False)
        self.assertEqual(self.fake.uploaded[('groups',self.endpoint.native_conversation_id)], png())
        wire = await self.send_result()
        self.assertEqual(wire['msg_type'], 7)
        self.assertEqual(self.file_row()['state'], 'message_accepted')
        self.assertEqual(len(self.fake.media_calls), 1)
        self.assertTrue(self.query('SELECT applied_at FROM delivery_receipts')[0]['applied_at'])

    async def test_jpeg_same_route_and_original_bytes(self):
        self.image_bytes, self.image_name = jpeg(), 'sticker.jpeg'
        self.prepare_file()
        await self.run_worker()
        await self.send_result()
        self.assertEqual(self.fake.uploaded[('groups',self.endpoint.native_conversation_id)], jpeg())

    async def test_file_type_four_evidence_does_not_authorize_image(self):
        self.proof = replace(self.proof, file_type=4)
        self.prepare_file()
        with self.assertRaises(JobDeferred):
            await self.dispatcher(self.claim_file())
        self.assertFalse(self.fake.media_calls or self.stages() or self.fake.received)

    async def test_revocation_before_upload_zero_http(self):
        self.prepare_file()
        job = self.claim_file()
        self.admitted = False
        outcome = await self.dispatcher(job)
        self.assertEqual(outcome['status'], 'cancelled')
        self.assertFalse(self.fake.media_calls or self.stages() or self.fake.received)

    async def test_upload_then_revocation_blocks_type_seven(self):
        self.prepare_file()
        await self.run_worker()
        self.assertEqual(len(self.fake.media_calls), 1)
        self.admitted = False
        delivery = self.query('SELECT delivery_id FROM qq_official_file_message_plans')[0]['delivery_id']
        outcome = await self.sender.send_delivery(delivery)
        self.assertNotEqual(outcome.status, 'committed')
        self.assertFalse(self.fake.received)

    async def test_unknown_upload_cannot_retry_or_change_channel(self):
        self.prepare_file()
        job = self.claim_file()
        self.fake.drop_response = True
        self.assertEqual((await self.dispatcher(job))['status'], 'upload_unknown')
        self.fake.drop_response = False
        await self.dispatcher(job)
        self.assertEqual(len(self.fake.media_calls), 1)
        self.assertFalse(self.fake.received)

    async def test_generation_readiness_keeps_type_four_and_legacy_port(self):
        from src.plugins.ai_chat.adapters.qq_official.file_generation import GenerationBindingAdapter
        binding=GenerationBindingAdapter(self.source,self.files,self.calls)
        self.assertFalse((await self.service.readiness(binding)).available)
        self.proof=replace(self.proof,file_type=4)
        self.assertTrue((await self.service.readiness(binding)).available)
        self.service.config=replace(self.service.config,native_media_evidence=None,
            native_evidence=lambda cursor,endpoint:self.proof)
        self.assertTrue((await self.service.readiness(binding)).available)
        self.proof=replace(self.proof,file_type=1)
        self.assertFalse((await self.service.readiness(binding)).available)

    async def test_existing_pdf_type_four_still_works(self):
        self.image_bytes, self.image_name = b'%PDF-1.4\n%TEST\n%%EOF\n', 'test.pdf'
        self.proof = replace(self.proof, file_type=4)
        self.prepare_file()
        await self.run_worker()
        await self.send_result()
        self.assertEqual(self.fake.media_calls[0][3]['file_type'], 4)
