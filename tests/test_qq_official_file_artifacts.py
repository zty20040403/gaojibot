from dataclasses import replace
from contextlib import contextmanager
import hashlib
import os
import unittest

from tests.qq_official_file_fixture import FileFixture, PDF, KEY
from src.plugins.ai_chat.execution_control import LeaseLost
from src.plugins.ai_chat.adapters.qq_official.file_artifacts import ArtifactError, detect_content



class ArtifactTests(FileFixture):
    def test_artifact_real_bytes_and_same_durable_queue(self):
        item = self.put()
        self.assertTrue(item.created)
        self.assertFalse(item.message_accepted)
        self.assertEqual(item.delivery_state, 'stored')
        self.assertEqual(self.blobs.read(item.sha256), PDF)
        self.assertEqual(item.byte_size, len(PDF))
        self.assertEqual(item.sha256, hashlib.sha256(PDF).hexdigest())
        row = self.query('SELECT * FROM qq_official_file_artifacts')[0]
        self.assertEqual(row['source_execution_id'], self.handle.execution_id)
        job = self.query('SELECT * FROM qq_official_file_deliveries')[0]
        self.assertEqual(self.jobs.get(job['job_id']).payload, {'artifact_id':item.artifact_id})
        self.assertNotIn('TEST_GROUP', repr(item))

    def test_artifact_idempotent_and_content_conflict(self):
        first = self.put()
        self.now += 1
        second = self.put()
        self.assertFalse(second.created)
        self.assertEqual(first.artifact_id, second.artifact_id)
        with self.assertRaisesRegex(ArtifactError,'request_conflict'):
            self.files.register(self.source, content=PDF+b'changed', filename='report.pdf', logical_request_key=KEY)
        self.assertEqual(len(self.query('SELECT * FROM qq_official_file_artifacts')),1)

    def test_artifact_unissued_copy_cannot_authorize(self):
        self.source = replace(self.source)
        with self.assertRaisesRegex(LeaseLost,'unissued'):
            self.put()
        self.assertFalse(self.query('SELECT * FROM qq_official_file_artifacts'))

    def test_artifact_revoked_before_disk_write(self):
        self.admitted = False
        with self.assertRaises(LeaseLost):
            self.put()
        self.assertFalse(list((self.blobs.root/'blobs').glob('*/*')))

    def test_artifact_source_expires_during_disk_write_rolls_back(self):
        put = self.blobs.put
        def late(content):
            result = put(content)
            self.now += 86401
            return result
        self.blobs.put = late
        with self.assertRaises(LeaseLost):
            self.put()
        self.assertFalse(self.query('SELECT * FROM qq_official_file_artifacts'))
        self.assertFalse(self.query('SELECT * FROM qq_official_file_deliveries'))
        self.assertEqual(len(list((self.blobs.root/'blobs').glob('*/*'))),1)

    def test_artifact_unknown_job_commit_does_not_create_manifest(self):
        def interrupted(**_):
            raise RuntimeError('TEST rollback before durable enqueue')
        self.jobs.enqueue = interrupted
        with self.assertRaises(RuntimeError):
            self.put()
        self.assertFalse(self.query('SELECT * FROM qq_official_file_artifacts'))
        self.assertEqual(len(list((self.blobs.root/'blobs').glob('*/*'))),1)

    def test_artifact_immutable_manifest_and_monotonic_retention(self):
        item = self.put()
        with self.assertRaises(Exception):
            self.query('UPDATE qq_official_file_artifacts SET filename=? WHERE artifact_id=?', ('swapped.pdf',item.artifact_id))
        self.query('UPDATE qq_official_file_artifacts SET retain_until=?,first_possible_send_at=? WHERE artifact_id=?',
                   (self.now+3600,self.now,item.artifact_id))
        with self.assertRaises(Exception):
            self.query('UPDATE qq_official_file_artifacts SET retain_until=? WHERE artifact_id=?', (self.now+7200,item.artifact_id))
        with self.assertRaises(Exception):
            self.query('UPDATE qq_official_file_artifacts SET first_possible_send_at=? WHERE artifact_id=?', (self.now+1,item.artifact_id))
        self.assertEqual(self.files.get(item.artifact_id).retain_until,self.now+3600)

    def test_artifact_quota_and_nonrenewing_deadline(self):
        self.files.max_bytes = len(PDF)
        self.files.quota_bytes = len(PDF)
        self.put()
        with self.assertRaisesRegex(ArtifactError,'quota'):
            self.files.register(self.source, content=PDF, filename='second.pdf', logical_request_key=hashlib.sha256(b'OTHER').hexdigest())
        self.now += 1
        self.source = self.issue()
        with self.assertRaisesRegex(ArtifactError,'conflict'):
            self.put()

    def test_artifact_draft_is_separate_from_delivery(self):
        item = self.put(draft=True,draft_reason='TEST independent acceptance incomplete')
        self.assertTrue(item.draft)
        self.assertEqual(item.filename,'草稿-report.pdf')
        self.assertFalse(item.message_accepted)
        self.assertEqual(self.query('SELECT draft_reason FROM qq_official_file_artifacts')[0]['draft_reason'],
                         'TEST independent acceptance incomplete')

    def test_named_arbitrary_files_and_filename_validation(self):
        self.assertEqual(detect_content(b'TEST data','report.csv',10000),'text/csv')
        self.assertEqual(detect_content(b'TEST data','report.zip',10000),'application/zip')
        with self.assertRaises(ArtifactError):
            detect_content(PDF,'../report.pdf',10000)

    def test_artifact_verified_read_rejects_forged_fingerprint(self):
        item = self.put()
        self.assertEqual(self.files.read_verified(item.artifact_id,sha256=item.sha256,byte_size=len(PDF)),PDF)
        with self.assertRaises(ArtifactError):
            self.files.read_verified(item.artifact_id,sha256='0'*64,byte_size=len(PDF))
        with self.assertRaises(ArtifactError):
            self.files.read_verified(item.artifact_id,sha256=item.sha256,byte_size=len(PDF)+1)

    def test_artifact_gc_commit_before_unlink_and_keeps_tombstone(self):
        item = self.put()
        self.now = item.pending_deadline_at
        self.assertEqual(self.files.collect_expired(),1)
        self.assertFalse(self.blobs.path(item.sha256).exists())
        self.assertEqual(self.files.get(item.artifact_id).bytes_state,'deleted')
        self.assertEqual(self.files.get(item.artifact_id).delivery_state,'expired')
        self.assertEqual(self.files.collect_expired(),0)

    def test_artifact_gc_rollback_does_not_unlink_live_bytes(self):
        item = self.put()
        self.now = item.pending_deadline_at
        @contextmanager
        def interrupted():
            with self.tx() as cursor:
                yield cursor
                raise RuntimeError('TEST transaction rollback')
        self.files.transaction = interrupted
        with self.assertRaises(RuntimeError):
            self.files.collect_expired()
        self.files.transaction = self.tx
        self.assertEqual(self.files.get(item.artifact_id).bytes_state,'available')
        self.assertTrue(self.blobs.path(item.sha256).exists())

    def test_artifact_gc_shared_bytes_survive_other_source_reference(self):
        first = self.put()
        self.query('UPDATE qq_official_file_artifacts SET retain_until=? WHERE artifact_id=?',
                   (self.now,first.artifact_id))
        second = self.files.register(self.source,content=PDF,filename='second.pdf',
            logical_request_key=hashlib.sha256(b'SECOND').hexdigest())
        self.assertEqual(self.files.collect_expired(),1)
        self.assertTrue(self.blobs.path(second.sha256).exists())
        self.assertEqual(self.files.read_verified(second.artifact_id,sha256=second.sha256,byte_size=len(PDF)),PDF)

    def test_artifact_orphans_wait_grace_and_keep_referenced_bytes(self):
        item = self.put()
        orphan = self.blobs.put(PDF+b'TEST orphan')
        old = self.now-86401
        os.utime(self.blobs.path(item.sha256),(old,old))
        os.utime(self.blobs.path(orphan.sha256),(old,old))
        self.assertEqual(self.files.collect_orphans(),(1,None))
        self.assertTrue(self.blobs.path(item.sha256).exists())
        self.assertFalse(self.blobs.path(orphan.sha256).exists())


if __name__ == "__main__":
    unittest.main()
