"""Caption admission is part of official ingestion, in the same real PG transaction."""
from concurrent.futures import ThreadPoolExecutor
import json
import unittest

from src.plugins.ai_chat.image_captions import CaptionSettings, caption_day
from tests import test_qq_official_ingest as ingress


class CaptionAdmissionTests(unittest.TestCase):
    setUp = ingress.QQOfficialIngestTests.setUp
    tx = ingress.QQOfficialIngestTests.tx
    query = ingress.QQOfficialIngestTests.query
    provision = ingress.QQOfficialIngestTests.provision
    seed = ingress.QQOfficialIngestTests.seed
    guard = ingress.QQOfficialIngestTests.guard
    process = ingress.QQOfficialIngestTests.process

    def image(self, **kwargs):
        inbox = self.seed(**kwargs)
        with self.tx() as cursor:
            raw = json.loads(cursor.execute('SELECT payload_json FROM qq_official_inbound_events WHERE inbox_id=?', (inbox,)).fetchone()['payload_json'])
            raw['d']['attachments'] = [{'url':'https://example.invalid/TEST_IMAGE?secret=TEST_SECRET', 'content_type':'image/png'}]
            cursor.execute('UPDATE qq_official_inbound_events SET payload_json=? WHERE inbox_id=?', (json.dumps(raw),inbox))
        return inbox

    def settings(self, limit=200):
        return CaptionSettings(self.database).set(self.endpoint.scope.key, enabled=True, daily_limit=limit)

    def test_default_off_records_media_but_does_not_spend_or_enqueue(self):
        self.process(self.image())
        links = self.query('SELECT * FROM message_media')
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]['media_kind'], 'image')
        self.assertEqual(links[0]['fetch_status'], 'disabled')
        self.assertEqual(links[0]['source_url'], '')
        self.assertEqual(self.query('SELECT * FROM media_jobs'), [])
        snapshot = CaptionSettings(self.database).snapshot(ingress.NOW)
        self.assertEqual(snapshot[0]['image_daily_limit'], 200)
        self.assertFalse(snapshot[0]['image_captions'])
        self.assertNotIn('TEST_SECRET', str(snapshot))

    def test_replay_is_idempotent_and_daily_limit_survives_new_store(self):
        self.settings(1)
        inbox = self.image()
        self.process(inbox)
        self.process(inbox)
        self.process(self.image())
        self.assertEqual([r['fetch_status'] for r in self.query('SELECT * FROM message_media ORDER BY message_media_id')], ['pending','quota'])
        self.assertEqual(len(self.query('SELECT * FROM media_jobs')), 1)
        snapshot = CaptionSettings(self.database).snapshot(ingress.NOW)
        self.assertEqual(snapshot[0]['admitted'], 1)
        self.assertEqual(snapshot[0]['quota_rejected'], 1)
        self.process(self.image(issued=ingress.NOW+86400, received=ingress.NOW+86400), now=ingress.NOW+86400)
        self.assertEqual(len(self.query('SELECT * FROM media_jobs')), 2)
        self.assertNotEqual(caption_day(ingress.NOW), caption_day(ingress.NOW+86400))

    def test_concurrent_admission_never_exceeds_limit(self):
        self.settings(3)
        inboxes = [self.image() for _ in range(8)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(self.process, inboxes))
        self.assertEqual(len(self.query('SELECT * FROM media_jobs')), 3)
        self.assertEqual(self.query('SELECT admitted FROM image_caption_daily')[0]['admitted'], 3)

    def test_ingestion_rollback_also_rolls_back_caption_reservation(self):
        self.settings(1)
        inbox = self.image()
        with self.assertRaisesRegex(RuntimeError, 'TEST rollback'):
            with self.tx() as cursor:
                self.process(inbox, cursor=cursor)
                raise RuntimeError('TEST rollback')
        self.assertEqual(self.query('SELECT * FROM message_media'), [])
        self.assertEqual(self.query('SELECT * FROM media_jobs'), [])
        self.assertEqual(self.query('SELECT * FROM image_caption_daily'), [])
        self.process(inbox)
        self.assertEqual(len(self.query('SELECT * FROM media_jobs')), 1)

    def test_settings_reject_unknown_scope_and_invalid_limits(self):
        store = CaptionSettings(self.database)
        for limit in (-1,10001,True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                store.set(self.endpoint.scope.key,enabled=True,daily_limit=limit)
        with self.assertRaises(ValueError):
            store.set('TEST_FOREIGN',enabled=True,daily_limit=200)
