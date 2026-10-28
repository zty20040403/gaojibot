from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
import importlib
import json
import sys
import unittest
from unittest.mock import AsyncMock, patch

from src.qq_official.events import parse_dispatch
from src.plugins.ai_chat.adapters.qq_official.codec import DecodedMessage, decode_message
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.platform_types import AccountKey
from tests.test_qq_official_inbox import OfficialInboxTests

from src.plugins.ai_chat.adapters.qq_official.commands import route_command
from src.plugins.ai_chat.adapters.qq_official.input_normalization import normalize_input
from tests.test_qq_official_commands import RouterTests, inbound, require_control_schema


def _router_fixture(cls=RouterTests):
    return cls()


def frame(text, mentions=(), event='GROUP_AT_MESSAGE_CREATE'):
    return {'op': 0, 't': event, 's': 1, 'id': 'TEST_EVENT', 'd': {'id': 'TEST_MSG',
        'group_openid': 'TEST_GROUP', 'author': {'member_openid': 'TEST_MEMBER'},
        'content': text, 'mentions': list(mentions), 'timestamp': '2027-01-15T08:00:00Z'}}


def normalize(text, mentions=(), event='GROUP_AT_MESSAGE_CREATE'):
    dispatch = parse_dispatch(frame(text, mentions, event))
    decoded = decode_message(dispatch, AccountKey('qq-official', 'TEST_APP', 'test'), 1800000000)
    assert isinstance(decoded, DecodedMessage)
    return normalize_input(dispatch)


class NormalizationTests(unittest.TestCase):
    def test_actual_dispatch_codec_bind_prefix_accepts_only_structural_self(self):
        for key in ('id', 'member_openid', 'user_openid'):
            for token in ('<@TEST_BOT>', '<@!TEST_BOT>'):
                with self.subTest(key=key, token=token):
                    result = normalize(token + ' /bind submit BC-' + 'a' * 32 + ' 723891',
                        ({key: 'TEST_BOT', 'is_you': True},))
                    self.assertEqual((result.kind, result.command.action, result.command.code), ('control', 'submit', '723891'))
                    self.assertIsNone(result.input_text)
                    self.assertNotIn('723891', repr(result))

    def test_forged_conflicting_all_and_nonboolean_mentions_never_authorize_or_leak(self):
        cases = ((), ({'id': 'OTHER', 'is_you': True},), ({'id': 'TEST_BOT', 'is_you': 1},),
            ({'id': 'TEST_BOT', 'is_you': False},), ({'id': 'TEST_BOT', 'nickname': 'bot'},),
            ({'id': 'TEST_BOT', 'is_you': True, 'member_openid': 'OTHER'},),
            ({'id': 'TEST_BOT', 'is_you': True, 'user_openid': None},),
            ({'id': 'TEST_BOT', 'is_you': True, 'scope': 'all'},),
            ({'id': 'TEST_BOT', 'is_you': True}, {'id': 'TEST_BOT', 'is_you': False}),
            ({'id': '!TEST_BOT', 'is_you': True}, {'id': 'TEST_BOT', 'is_you': True}))
        for mentions in cases:
            with self.subTest(mentions=mentions):
                result = normalize('<@!TEST_BOT> /bind submit BC-' + 'a' * 32 + ' 723891', mentions)
                self.assertEqual(result.kind, 'quarantine')
                self.assertIsNone(result.command)
                self.assertIsNone(result.input_text)
                self.assertNotIn('723891', repr(result))

    def test_raw_at_name_or_embedded_binding_is_privacy_suppression_not_authority(self):
        for text in ('@bot /bind submit test 723891', 'please /bind submit test 723891',
                     '<@FAKE>/绑定 submit test 723891', '<@TEST_BOT> /bind submit test 723891'):
            result = normalize(text)
            self.assertEqual(result.kind, 'quarantine')
            self.assertIsNone(result.command)

    def test_plain_text_preserved_and_only_trusted_leading_mentions_removed(self):
        evidence = ({'id': 'TEST_BOT', 'is_you': True},)
        for original, expected in (('  ordinary  ', '  ordinary  '),
            ('<@TEST_BOT>  /help', '/help'), ('<@!TEST_BOT><@TEST_BOT> hi', 'hi'),
            ('hello <@TEST_BOT> /help', 'hello <@TEST_BOT> /help')):
            self.assertEqual(normalize(original, evidence).input_text, expected)
        self.assertEqual(normalize('<@TEST_BOT> /help').input_text, '<@TEST_BOT> /help')

    def test_group_at_event_and_display_name_do_not_replace_evidence(self):
        result = normalize('<@TEST_BOT> /help', ({'nickname': 'TEST_BOT', 'is_you': True},))
        self.assertEqual((result.input_text, result.stripped_mentions), ('<@TEST_BOT> /help', 0))

    def test_whoami_alias_has_no_arguments_or_reducer_side_effect(self):
        for text in ('/whoami', '<@TEST_BOT> /whoami'):
            result = normalize(text, ({'id': 'TEST_BOT', 'is_you': True},))
            self.assertEqual((result.kind, result.command.action), ('control', 'whoami'))
        result = normalize('/whoami 723891')
        self.assertEqual((result.kind, result.command.valid), ('control', False))

    def test_opaque_ids_exact_and_input_bounds(self):
        result = normalize('<@!身份/opaque:β> /help', ({'id': '身份/opaque:β', 'is_you': True},))
        self.assertEqual(result.input_text, '/help')
        dispatch = parse_dispatch(frame('normal'))
        for text in ('\x00secret', '\ud800', '界' * 87382):
            result = normalize_input(replace(dispatch, message=replace(dispatch.message, content=text)))
            self.assertEqual((result.kind, result.reason), ('quarantine', 'input_text_invalid'))

    def test_source_unchanged_and_repr_contains_no_text(self):
        source = frame('<@!TEST_BOT> /help', ({'id': 'TEST_BOT', 'is_you': True},))
        expected = deepcopy(source)
        result = normalize_input(parse_dispatch(source))
        self.assertEqual(source, expected)
        self.assertNotIn('/help', repr(result))


def official_module(name):
    return importlib.import_module('src.plugins.ai_chat.adapters.qq_official.' + name)


class IngressNormalizationTests(OfficialInboxTests):
    # Reuse fixture methods without duplicating its broad test suite.
    def setUp(self):
        super().setUp()
        require_control_schema(self)
        self.ingest = official_module('ingest')
        self.ledger = MessageLedger(self.database)
        self.addCleanup(self.ledger.close)

    def new_store(self, **kwargs):
        store = official_module('inbox').OfficialInboxStore(self.database, jobs=self.jobs, binding=self.binding,
            authority=self.authority, clock=lambda: self.now, **kwargs)
        self.addCleanup(store.close)
        return store

    def test_prefixed_bind_pipeline_deduplicates_and_never_stores_secret(self):
        issued = self.issue()
        secret = issued.effect.code
        content = '<@!TEST_BOT> /bind submit ' + issued.challenge_id + ' ' + secret
        result = self.accept(content=content, mentions=[{'id': 'TEST_BOT', 'is_you': True}])
        self.assertEqual((result.disposition, result.binding.status), ('control', 'pending'))
        row = self.query('SELECT * FROM qq_official_inbound_events WHERE inbox_id=?', (result.inbox_id,))[0]
        self.assertIsNone(row['payload_json'])
        self.assertIsNone(row['turn_text'])
        decision = self.query('SELECT * FROM qq_official_control_decisions WHERE inbox_id=?', (result.inbox_id,))[0]
        self.assertEqual(row['control_actor_key'], decision['requester_actor_key'])
        self.assertEqual((row['control_action'], row['control_decision_code']), ('submit', 'pending'))
        repeat = self.accept(content=content, native=row['native_message_id'], mentions=[{'id': 'TEST_BOT', 'is_you': True}])
        self.assertEqual(repeat.disposition, 'duplicate')
        self.assertEqual(self.count('qq_official_binding_candidates'), 1)
        self.assertNotIn(secret, self.persisted_text())
        self.assertEqual(self.count('messages'), 0)

    def test_forged_bind_is_body_free_quarantine_without_reducer_or_window(self):
        self.activate_group()
        with patch.object(self.binding, 'apply', side_effect=AssertionError('must not authorize forged prefix')):
            result = self.accept(content='<@!TEST_BOT> /bind submit BC-' + 'a' * 32 + ' 723891')
        self.assertEqual(result.disposition, 'quarantined')
        row = self.query('SELECT * FROM qq_official_inbound_events WHERE inbox_id=?', (result.inbox_id,))[0]
        self.assertEqual(row['reason_code'], 'input_sensitive_control')
        self.assertIsNone(row['payload_json'])
        self.assertIsNone(row['turn_text'])
        self.assertNotIn('723891', self.persisted_text())
        self.assertEqual(self.count('qq_official_reply_windows'), 0)
        self.assertEqual(self.count('messages'), 0)
        self.assertEqual(self.count('durable_jobs'), 0)

    def test_help_pipeline_persists_source_text_and_real_shared_router_works_without_raw_payload(self):
        self.activate_group()
        result = self.accept(content='<@!TEST_BOT> /help', event_type='GROUP_AT_MESSAGE_CREATE',
            mentions=[{'id': 'TEST_BOT', 'is_you': True}])
        with self.tx() as cursor:
            ingested = self.ingest.process_inbox(cursor, inbox_id=result.inbox_id, lease_token=self.token,
                ledger=self.ledger, jobs=self.jobs, epoch_guard=assert_epoch, admission=self.store.admitted, now=self.now)
        self.assertEqual(ingested.status, 'processed')
        row = self.query('SELECT * FROM qq_official_inbound_events WHERE inbox_id=?', (result.inbox_id,))[0]
        self.assertEqual((row['turn_text'], row['turn_text_version']), ('/help', 1))
        with self.tx() as cursor:
            cursor.execute('UPDATE qq_official_inbound_events SET payload_json=NULL WHERE inbox_id=?', (result.inbox_id,))
        async def route():
            fixture = _router_fixture()
            await fixture.asyncSetUp()
            try:
                message = inbound(self.account)
                begin = AsyncMock()
                routed = await route_command(row['turn_text'], message, services=fixture.services,
                    admission=lambda _: True, before_effect=begin)
                self.assertIn('/model', routed.result.text)
                begin.assert_awaited_once()
            finally:
                await fixture.asyncTearDown()
                fixture.temp.cleanup()
        asyncio.run(route())
        self.assertEqual(self.count('messages'), 1)

    def test_old_misclassified_secret_is_erased_before_ledger_and_turn_text(self):
        self.activate_group()
        result = self.accept(content='ordinary input')
        row = self.query('SELECT * FROM qq_official_inbound_events WHERE inbox_id=?', (result.inbox_id,))[0]
        frame = json.loads(row['payload_json'])
        frame['d']['content'] = '<@!FORGED> /bind submit BC-' + 'a' * 32 + ' 723891'
        with self.tx() as cursor:
            cursor.execute('UPDATE qq_official_inbound_events SET payload_json=? WHERE inbox_id=?',
                (json.dumps(frame), result.inbox_id))
            outcome = self.ingest.process_inbox(cursor, inbox_id=result.inbox_id, lease_token=self.token,
                ledger=self.ledger, jobs=self.jobs, epoch_guard=assert_epoch, admission=self.store.admitted, now=self.now)
        self.assertEqual(outcome.status, 'quarantined')
        after = self.query('SELECT * FROM qq_official_inbound_events WHERE inbox_id=?', (result.inbox_id,))[0]
        self.assertIsNone(after['payload_json'])
        self.assertIsNone(after['turn_text'])
        self.assertEqual(self.count('messages'), 0)
        self.assertNotIn('723891', self.persisted_text())

    def test_turn_text_and_ledger_rollback_together(self):
        self.activate_group()
        result = self.accept(content='<@TEST_BOT> /help', event_type='GROUP_AT_MESSAGE_CREATE',
            mentions=[{'id': 'TEST_BOT', 'is_you': True}])
        with self.assertRaisesRegex(RuntimeError, 'TEST_ROLLBACK'):
            with self.tx() as cursor:
                self.ingest.process_inbox(cursor, inbox_id=result.inbox_id, lease_token=self.token,
                    ledger=self.ledger, jobs=self.jobs, epoch_guard=assert_epoch, admission=self.store.admitted, now=self.now)
                raise RuntimeError('TEST_ROLLBACK')
        row = self.query('SELECT * FROM qq_official_inbound_events WHERE inbox_id=?', (result.inbox_id,))[0]
        self.assertIsNone(row['turn_text'])
        self.assertEqual(row['status'], 'pending')
        self.assertEqual(self.count('messages'), 0)

    def test_whoami_source_receipt_window_control_job_atomic_without_owner(self):
        result = self.accept(private=True, actor='TEST_NEW_USER', content='/whoami')
        self.assertEqual((result.disposition, result.binding.status), ('control', 'identity'))
        row = self.query('SELECT * FROM qq_official_control_decisions WHERE inbox_id=?', (result.inbox_id,))[0]
        self.assertEqual((row['reply_kind'], row['state'], row['expires_at']), ('whoami', 'recorded', self.now + 30))
        self.assertIsNotNone(row['job_id'])
        self.assertIsNotNone(row['reply_window_id'])
        self.assertEqual(self.jobs.get(row['job_id']).payload, {'control_id': row['control_id']})
        self.assertEqual(self.count('qq_official_owner_grants'), 1)  # configured owner only
        self.assertEqual(self.count('messages'), 0)

    def test_control_ack_observes_only_original_timestamp_and_atomic_failure_rolls_back(self):
        module = sys.modules[type(self.store).__module__]
        before = self.snapshot()
        with patch.object(module, 'enqueue_control_job', side_effect=RuntimeError('TEST_ROLLBACK')):
            with self.assertRaisesRegex(RuntimeError, 'TEST_ROLLBACK'):
                self.accept(private=True, content='/bind cancel BC-' + 'a' * 32)
        self.assertEqual(self.snapshot(), before)
        result = self.accept(private=True, content='/bind cancel BC-' + 'a' * 32,
            timestamp='2027-01-15T07:59:00Z')
        row = self.query('SELECT * FROM qq_official_control_decisions WHERE inbox_id=?', (result.inbox_id,))[0]
        window = self.query('SELECT * FROM qq_official_reply_windows WHERE window_id=?', (row['reply_window_id'],))[0]
        self.assertEqual(window['issued_at'], self.now - 60)
        self.assertLessEqual(row['expires_at'], window['expires_at'])


# Exercise only the changed boundaries; the inherited suite runs separately.
for _name in tuple(dir(OfficialInboxTests)):
    if _name.startswith('test_') and _name not in IngressNormalizationTests.__dict__:
        setattr(IngressNormalizationTests, _name, None)
del OfficialInboxTests, RouterTests
