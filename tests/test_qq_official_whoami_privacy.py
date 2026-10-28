"""Real transport receipts redact context only; protected send audit stays true."""
from __future__ import annotations

import json

from src.plugins.ai_chat.adapters.qq_official.renderer import render_plan
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from tests.test_qq_official_control_pipeline import ControlPipelineTests


class WhoamiContextPrivacyTests(ControlPipelineTests):
    async def test_real_whoami_send_keeps_audit_but_context_contains_no_requester_identity(self):
        for private in (True, False):
            with self.subTest(private=private):
                delivery_id, control, _ = self.control_plan(private=private)
                protected_before = self.query('''SELECT d.body_json,p.manifest_json,p.manifest_digest
                    FROM deliveries d JOIN delivery_plans p ON p.plan_id=d.plan_id WHERE d.delivery_id=?''', (delivery_id,))[0]
                outcome = await self.sender.send_delivery(delivery_id)
                self.assertEqual(outcome.status, 'committed')
                actual = self.fake.received[-1]
                text = actual['payload']['content']
                self.assertIn('TEST_UNPRIVILEGED', text)
                if not private:
                    self.assertIn('TEST_GROUP', text)
                endpoint = EndpointKey.parse(control['endpoint_key'])
                ledger_row = self.query('SELECT * FROM messages WHERE native_message_id=? AND source_endpoint_key=?',
                    (actual['receipt']['id'], endpoint.key))[0]
                self.assertEqual((ledger_row['direction'], ledger_row['message_kind']), ('outbound', 'command'))
                provenance = json.loads(ledger_row['raw_event_json'])
                self.assertIs(provenance['content_redacted'], True)
                self.assertEqual(provenance['redaction_reason'], 'qq_official_whoami_identity')
                self.assertEqual(provenance['control_kind'], 'whoami')
                context = self.ledger.render_recent(endpoint.scope)
                self.assertEqual(context, '')
                archived = self.ledger.get_in_scope(endpoint.scope, ledger_row['canonical_message_id'])
                self.assertEqual(archived.prompt_text, '')
                self.assertIn('身份查询已回复', archived.rendered_text)
                for identity in ('TEST_UNPRIVILEGED', 'TEST_GROUP'):
                    self.assertNotIn(identity, context)
                    self.assertNotIn(identity, ledger_row['body_json'])
                    self.assertNotIn(identity, ledger_row['rendered_text'])
                    self.assertNotIn(identity, ledger_row['raw_event_json'])
                protected_after = self.query('''SELECT d.body_json,p.manifest_json,p.manifest_digest
                    FROM deliveries d JOIN delivery_plans p ON p.plan_id=d.plan_id WHERE d.delivery_id=?''', (delivery_id,))[0]
                self.assertEqual(protected_after, protected_before)
                self.assertIn('TEST_UNPRIVILEGED', protected_after['body_json'])
                attempt = self.query('SELECT * FROM delivery_attempts WHERE delivery_id=?', (delivery_id,))[0]
                self.assertIn('TEST_UNPRIVILEGED', attempt['wire_payload_json'])
                self.assertEqual(ActorKey.parse(attempt['bot_actor_key']).identity_kind, 'bot')

    async def test_expired_revoked_receipt_replay_keeps_same_redacted_historical_message(self):
        delivery_id, control, _ = self.control_plan()
        self.assertEqual((await self.sender.send_delivery(delivery_id)).status, 'committed')
        receipt = self.query('SELECT * FROM delivery_receipts')[0]
        before = self.query('SELECT * FROM messages')[0]
        audit = self.query('SELECT wire_payload_json,payload_digest FROM delivery_attempts')[0]
        self.now += 100
        with self.tx() as cursor:
            cursor.execute("UPDATE qq_official_endpoints SET binding_state='revoked',route_epoch=route_epoch+1 WHERE endpoint_key=?",
                (control['endpoint_key'],))
        self.assertEqual(self.outbox.finalize(self.token, receipt['receipt_id'], ledger=self.ledger).status, 'committed')
        self.assertEqual(self.query('SELECT * FROM messages'), [before])
        self.assertEqual(self.query('SELECT wire_payload_json,payload_digest FROM delivery_attempts')[0], audit)
        self.assertEqual(self.query('SELECT receipt_digest FROM delivery_receipts')[0]['receipt_digest'], receipt['receipt_digest'])
        self.assertNotIn('TEST_UNPRIVILEGED', self.ledger.render_recent(EndpointKey.parse(control['endpoint_key']).scope))
        self.assertEqual(len(self.fake.received), 1)

    async def test_normal_reply_with_identity_like_text_is_not_rewritten(self):
        _, control, _ = self.control_plan(private=False)
        self.activate_group()
        endpoint = EndpointKey.parse(control['endpoint_key'])
        actor = ActorKey.parse(control['requester_actor_key'])
        original = '普通回复测试：当前身份 TEST_LITERAL_IDENTITY stays literal.'
        plan = self.outbox.enqueue(self.token, rendered=render_plan(MessageBody((TextNode(0, original),)), endpoint, markdown=False),
            requester=actor, logical_key='TEST_ORDINARY_REPLY', source_scope_key=endpoint.scope.key,
            source_native_message_id=control['source_native_message_id'], expires_at=self.now + 100)
        self.assertEqual((await self.sender.send_delivery(plan.delivery_ids[0])).status, 'committed')
        message = self.query('SELECT * FROM messages')[0]
        self.assertIn('TEST_LITERAL_IDENTITY', message['body_json'])
        self.assertNotIn('content_redacted', json.loads(message['raw_event_json']))
        self.assertIn('TEST_LITERAL_IDENTITY', self.ledger.render_recent(endpoint.scope))


for _name in tuple(dir(ControlPipelineTests)):
    if _name.startswith('test_') and _name not in WhoamiContextPrivacyTests.__dict__:
        setattr(WhoamiContextPrivacyTests, _name, None)
del ControlPipelineTests
