"""Real disposable PostgreSQL + account leases, protected M3 persistence."""
import json
from dataclasses import replace
from unittest.mock import patch

from tests.qq_official_outbox_fixture import OutboxFixture
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.turn_journal import TurnJournal
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.outbox import OfficialOutbox
from src.plugins.ai_chat.adapters.qq_official.outbox_types import OutboxInvariantError, stable_json, digest
from src.plugins.ai_chat.adapters.qq_official.plans import enqueue_plan
from src.plugins.ai_chat.adapters.qq_official.protected_payload import ProtectedContentRef, commitment
from src.plugins.ai_chat.adapters.qq_official.protected_plans import PROTECTED_BODY, protected_rendered
from src.plugins.ai_chat.adapters.qq_official.rate_policy import LocalRatePermit
from src.plugins.ai_chat.adapters.qq_official.renderer import render_plan
from src.qq_official.errors import QQAPIError, classify_error


class Port:
    def __init__(self, case):
        self.case=case
        self.grant=object()
        self.allowed=True
        self.available=True
        self.active=None
        self.content={'msg_type':7,'media':{'file_info':'TEST_SECRET_NATIVE_FILE_INFO'}}
        self.calls=[]
    def _check(self, content_ref,binding,now):
        if (not self.allowed or content_ref!=self.case.ref or binding.endpoint!=self.case.endpoint
                or binding.requester!=self.case.actor or binding.source_native_message_id!='TEST_SOURCE'
                or binding.source_canonical_message_id!=self.case.source.canonical_message_id
                or binding.source_scope_key!=self.case.endpoint.scope.key
                or binding.expires_at!=self.case.deadline or binding.endpoint_generation!=1 or binding.route_epoch!=1
                or now>=self.case.deadline):
            raise PermissionError('TEST_DENIED_DETAIL')
    def authorize_enqueue(self,cursor,*,token,source_grant,content_ref,binding,now):
        self._check(content_ref,binding,now)
        if source_grant is not self.grant or binding.plan_id is not None or binding.delivery_id is not None:
            raise PermissionError('TEST_FORGED_SOURCE')
        self.calls.append('enqueue')
    def authorize(self,cursor,*,token,content_ref,binding,now):
        self._check(content_ref,binding,now)
        if self.active!=(binding.plan_id,binding.delivery_id):
            raise PermissionError('TEST_WRONG_ACTIVE_PLAN')
        self.calls.append('authorize')
    def resolve(self,cursor,*,token,content_ref,binding,now):
        self.authorize(cursor,token=token,content_ref=content_ref,binding=binding,now=now)
        if not self.available:
            raise PermissionError('TEST_VAULT_DELETED')
        self.calls.append('resolve')
        return json.loads(stable_json(self.content))


class ProtectedOutboxTests(OutboxFixture):
    def setUp(self):
        super().setUp()
        with self.tx() as cursor:
            observe_ready(cursor,self.token,{'op':0,'t':'READY','d':{'session_id':'TEST_READY',
                'user':{'id':'TEST_BOT','bot':True}}},self.now)
        self.deliveries=DeliveryStore(self.database,recover_interrupted=False)
        self.ledger=MessageLedger(self.database)
        self.journal=TurnJournal(self.database)
        for store in (self.deliveries,self.ledger,self.journal): self.addCleanup(store.close)
        self.source=self.ledger.record_message(self.endpoint.scope,native_message_id='TEST_SOURCE',
            sender_native_user_id=self.actor.native_user_id,sender_display='Test',
            body=MessageBody((TextNode(0,'TEST request attachment'),)),occurred_at=self.now,
            actor=self.actor,source_endpoint=self.endpoint)
        self.query('''INSERT INTO qq_official_messages
            (message_id,account_key,endpoint_key,native_message_id,canonical_message_id,direction,created_at)
            VALUES (?,?,?,'TEST_SOURCE',?,'inbound',?)''',
            ('1'*32,self.account.key,self.endpoint.key,self.source.canonical_message_id,self.now))
        self.deadline=self.now+200
        self.port=Port(self)
        self.ref=ProtectedContentRef('a'*32,'b'*32,'native',commitment(self.port.content),self.deadline)
        self.outbox=self.make_outbox()
        self.window_id=self.window()

    def make_outbox(self,port=True):
        return OfficialOutbox(self.deliveries,admission=lambda *_:True,pepper=b'TEST'*8,
            clock=lambda:self.now,protected_payloads=self.port if port else None)

    def enqueue(self,**overrides):
        args=dict(content_ref=self.ref,source_grant=self.port.grant,endpoint=self.endpoint,requester=self.actor,
            logical_key='TEST_ARTIFACT',source_scope_key=self.endpoint.scope.key,expires_at=self.deadline,
            source_native_message_id='TEST_SOURCE',source_canonical_message_id=self.source.canonical_message_id)
        args.update(overrides)
        plan=self.outbox.enqueue_artifact(self.token,**args)
        self.port.active=(plan.plan_id,plan.delivery_ids[0])
        return plan

    def prepare(self,delivery):
        permit=LocalRatePermit(self.account.key,self.endpoint.key,'group','passive','unverified',float(self.now))
        return self.outbox.prepare(self.token,delivery,permit)

    def invoked(self):
        plan=self.enqueue()
        reservation=self.prepare(plan.delivery_ids[0])
        self.assertTrue(self.outbox.mark_dispatching(self.token,reservation))
        wire=self.outbox.request_started(reservation)
        return plan,reservation,wire

    def dump_rows(self):
        return stable_json({table:[dict(r) for r in self.query('SELECT * FROM '+table)]
            for table in ('delivery_plans','deliveries','delivery_attempts','delivery_receipts','messages')})

    def assert_secret_free(self):
        data=self.dump_rows()
        for secret in ('TEST_SECRET_NATIVE_FILE_INFO','TEST_SECRET_LINK_TOKEN','https://download.example/'):
            self.assertNotIn(secret,data)

    def test_native_wire_only_in_ram_storage_envelope_and_receipt_redaction(self):
        plan,reservation,wire=self.invoked()
        self.assertEqual(wire['media']['file_info'],'TEST_SECRET_NATIVE_FILE_INFO')
        self.assertEqual(wire['msg_id'],'TEST_SOURCE')
        self.assertEqual(wire['msg_seq'],1)
        attempt=self.query('SELECT * FROM delivery_attempts')[0]
        envelope=json.loads(attempt['wire_payload_json'])
        self.assertEqual(envelope['mode'],'protected')
        self.assertEqual(envelope['wire_digest'],digest(reservation.wire_json))
        self.assertEqual(attempt['wire_storage_digest'],digest(attempt['wire_payload_json']))
        receipt=self.outbox.capture_receipt(reservation,{'id':'TEST_SENT','body':'TEST_SECRET_NATIVE_FILE_INFO'})
        result=self.outbox.finalize(self.token,receipt,ledger=self.ledger)
        self.assertEqual(result.status,'committed')
        message=self.query("SELECT * FROM messages WHERE direction='outbound'")[0]
        self.assertEqual(message['body_json'],PROTECTED_BODY)
        self.assertEqual(message['message_kind'],'command')
        self.assertTrue(json.loads(message['raw_event_json'])['content_redacted'])
        self.assert_secret_free()

    def test_link_resolves_exact_frozen_url_and_no_url_in_persistent_rows(self):
        self.port.content={'msg_type':0,'content':'Download: https://download.example/files/TEST_SECRET_LINK_TOKEN'}
        self.ref=replace(self.ref,route='link',content_commitment=commitment(self.port.content))
        plan,reservation,wire=self.invoked()
        self.assertEqual(wire['content'],self.port.content['content'])
        receipt=self.outbox.capture_receipt(reservation,{'id':'TEST_LINK_SENT'})
        self.assertEqual(self.outbox.finalize(self.token,receipt,ledger=self.ledger).status,'committed')
        self.assert_secret_free()

    def test_stable_idempotency_ref_changes_conflict_and_ordinary_enqueue_has_no_secret_parameter(self):
        first=self.enqueue()
        second=self.enqueue()
        self.assertEqual(first.delivery_ids,second.delivery_ids)
        self.assertFalse(second.created)
        self.ref=replace(self.ref,credential_ref='c'*32)
        with self.assertRaisesRegex(OutboxInvariantError,'identity_conflict'):
            self.enqueue()
        with self.assertRaises(TypeError):
            self.outbox.enqueue(self.token,protected_ref=self.ref)
        self.assertEqual(len(self.query('SELECT * FROM delivery_plans')),1)

    def test_missing_port_and_forged_source_cannot_enqueue(self):
        self.outbox=self.make_outbox(port=False)
        with self.assertRaises(OutboxInvariantError): self.enqueue()
        self.outbox=self.make_outbox()
        with self.assertRaises(PermissionError): self.enqueue(source_grant=object())
        self.assertFalse(self.query('SELECT * FROM deliveries'))

    def test_candidate_rechecks_active_plan_and_source_without_resolving_secret(self):
        delivery=self.enqueue().delivery_ids[0]
        self.port.calls.clear()
        self.assertEqual(self.outbox.candidate(self.token,delivery).send_mode,'passive')
        self.assertEqual(self.port.calls,['authorize'])
        self.port.active=('wrong',delivery)
        self.assertEqual(self.outbox.candidate(self.token,delivery).status,'cancelled')
        self.assertFalse(self.query('SELECT * FROM delivery_attempts'))

    def test_resolve_mismatch_rolls_back_slot_seq_and_attempt(self):
        delivery=self.enqueue().delivery_ids[0]
        self.port.content={'msg_type':7,'media':{'file_info':'TEST_CHANGED_SECRET'}}
        with self.assertRaises(ValueError): self.prepare(delivery)
        self.assertFalse(self.query('SELECT * FROM delivery_attempts'))
        self.assertEqual(self.query('SELECT next_seq,reserved_count FROM qq_official_reply_windows')[0]['next_seq'],1)
        self.assertEqual(self.deliveries.get(delivery).status,'pending')

    def test_changed_secret_or_revoked_grant_after_prepare_blocks_handoff(self):
        delivery=self.enqueue().delivery_ids[0]
        reservation=self.prepare(delivery)
        self.port.content={'msg_type':7,'media':{'file_info':'TEST_CHANGED_SECRET'}}
        self.assertFalse(self.outbox.mark_dispatching(self.token,reservation))
        self.port.content={'msg_type':7,'media':{'file_info':'TEST_SECRET_NATIVE_FILE_INFO'}}
        self.port.allowed=False
        self.assertFalse(self.outbox.mark_dispatching(self.token,reservation))
        with self.assertRaises(OutboxInvariantError): self.outbox.request_started(reservation)

    def test_late_receipt_after_expiry_vault_delete_route_revoke_and_new_lease(self):
        plan,reservation,_=self.invoked()
        self.outbox.fail(self.token,reservation,QQAPIError('transport_ambiguous',stage='send'))
        self.account_lease.close()
        self.token=self.lease().acquire()
        self.now=self.deadline+50
        self.port.allowed=False
        self.port.available=False
        self.query("UPDATE qq_official_endpoints SET binding_state='revoked',generation=generation+1,route_epoch=route_epoch+1")
        receipt=self.outbox.capture_receipt(reservation,{'id':'TEST_LATE'})
        self.outbox=self.make_outbox(port=False)
        result=self.outbox.finalize(self.token,receipt,ledger=self.ledger)
        self.assertEqual(result.status,'committed')
        self.assertEqual(self.outbox.finalize(self.token,receipt,ledger=self.ledger).status,'committed')
        self.assertEqual(len(self.query("SELECT * FROM messages WHERE direction='outbound'")),1)
        self.assert_secret_free()

    def test_restart_never_requeues_unknown_or_requires_vault_to_preserve_evidence(self):
        plan,reservation,_=self.invoked()
        self.account_lease.close()
        self.token=self.lease().acquire()
        self.outbox=self.make_outbox(port=False)
        self.outbox.recover(self.token)
        self.assertEqual(self.deliveries.get(reservation.delivery_id).status,'ambiguous')
        self.assertEqual(self.outbox.list_due(self.token),())
        self.assertEqual(self.query('SELECT reserved_count FROM qq_official_reply_windows')[0]['reserved_count'],1)

    def test_protected_format_error_never_selects_placeholder_or_plain_fallback(self):
        plan,reservation,_=self.invoked()
        result=self.outbox.fail(self.token,reservation,classify_error(400,50037))
        self.assertIn(result.status,{'failed','ambiguous'})
        row=self.query('SELECT * FROM deliveries')[0]
        self.assertEqual(row['variant'],'primary')
        self.assertFalse(row['format_fallback_used'])
        self.assertEqual(self.outbox.list_due(self.token),())
        self.assert_secret_free()

    def test_envelope_or_storage_digest_tamper_cannot_capture_or_finalize(self):
        plan,reservation,_=self.invoked()
        original=self.query('SELECT wire_payload_json,wire_storage_digest FROM delivery_attempts')[0]
        self.query("UPDATE delivery_attempts SET wire_storage_digest=?",('f'*64,))
        with self.assertRaises(OutboxInvariantError): self.outbox.capture_receipt(reservation,{'id':'TEST_TAMPER'})
        self.query('UPDATE delivery_attempts SET wire_storage_digest=?',(original['wire_storage_digest'],))
        receipt=self.outbox.capture_receipt(reservation,{'id':'TEST_GOOD'})
        envelope=json.loads(original['wire_payload_json'])
        envelope['content_ref']['credential_ref']='d'*32
        changed=stable_json(envelope)
        self.query('UPDATE delivery_attempts SET wire_payload_json=?,wire_storage_digest=?',(changed,digest(changed)))
        with self.assertRaises(OutboxInvariantError): self.outbox.finalize(self.token,receipt,ledger=self.ledger)
        self.assertFalse(self.query("SELECT * FROM messages WHERE direction='outbound'"))
        self.assertEqual(self.query('SELECT state FROM delivery_attempts')[0]['state'],'dispatched')

    def test_receipt_quote_uses_only_safe_frozen_routing(self):
        self.query('''INSERT INTO qq_official_message_indexes
            (account_key,endpoint_key,index_kind,index_value,native_message_id,canonical_message_id,created_at,updated_at)
            VALUES (?,?,'msg_idx','TEST_INDEX','TEST_SOURCE',?,?,?)''',
            (self.account.key,self.endpoint.key,self.source.canonical_message_id,self.now,self.now))
        plan,reservation,wire=self.invoked()
        self.assertEqual(wire['message_reference'],{'message_id':'TEST_INDEX'})
        receipt=self.outbox.capture_receipt(reservation,{'id':'TEST_QUOTED'})
        self.port.available=False
        self.assertEqual(self.outbox.finalize(self.token,receipt,ledger=self.ledger).status,'committed')
        message=self.query("SELECT reply_to_native_message_id FROM messages WHERE direction='outbound'")[0]
        self.assertEqual(message['reply_to_native_message_id'],'TEST_SOURCE')

    def test_prepare_transaction_rollback_keeps_source_and_sequence_unspent(self):
        delivery=self.enqueue().delivery_ids[0]
        with patch('src.plugins.ai_chat.adapters.qq_official.outbox.reserve_send_policy',side_effect=RuntimeError('TEST')):
            with self.assertRaises(RuntimeError): self.prepare(delivery)
        self.assertFalse(self.query('SELECT * FROM delivery_attempts'))
        self.assertEqual(self.query('SELECT next_seq FROM qq_official_reply_windows')[0]['next_seq'],1)
        self.assertEqual(self.deliveries.get(delivery).status,'pending')

    def test_internal_protected_plan_cannot_be_otp_multi_part_or_expiry_extended(self):
        with self.tx() as cursor:
            with self.assertRaises(OutboxInvariantError):
                enqueue_plan(cursor,token=self.token,rendered=protected_rendered(self.endpoint),requester=self.actor,
                    logical_key='TEST_BAD',source_scope_key=self.endpoint.scope.key,expires_at=self.deadline+1,
                    now=self.now,purpose='requested_artifact',admission=lambda *_:True,protected_ref=self.ref)
        for purpose,rendered in (
            ('final',protected_rendered(self.endpoint)),
            ('requested_artifact',render_plan(MessageBody((TextNode(0,'abcdef '*40),)),self.endpoint,max_chars=20)),
        ):
            with self.tx() as cursor:
                with self.assertRaises(OutboxInvariantError):
                    enqueue_plan(cursor,token=self.token,rendered=rendered,requester=self.actor,
                        logical_key='TEST_BAD',source_scope_key=self.endpoint.scope.key,expires_at=self.deadline,
                        now=self.now,purpose=purpose,admission=lambda *_:True,protected_ref=self.ref)
        self.assertFalse(self.query('SELECT * FROM deliveries'))

    def test_database_requires_storage_digest_only_for_protected_and_fixed_redacted_body(self):
        from src.bot_storage.database import DatabaseError
        plan,reservation,_=self.invoked()
        with self.assertRaises(DatabaseError):
            self.query('UPDATE delivery_attempts SET wire_storage_digest=NULL')
        with self.assertRaises(DatabaseError):
            self.query('UPDATE deliveries SET body_json=?',(PROTECTED_BODY+' ',))
        ordinary=self.outbox.enqueue(self.token,rendered=render_plan(MessageBody((TextNode(0,'TEST plain'),)),self.endpoint),
            requester=self.actor,logical_key='TEST_PLAIN',source_scope_key=self.endpoint.scope.key,
            expires_at=self.deadline,source_native_message_id='TEST_SOURCE').delivery_ids[0]
        normal=self.prepare(ordinary)
        with self.assertRaises(DatabaseError):
            self.query('UPDATE delivery_attempts SET wire_storage_digest=? WHERE attempt_id=?',('a'*64,normal.attempt_id))
        self.assertIsNone(self.query('SELECT wire_storage_digest FROM delivery_attempts WHERE attempt_id=?',
            (normal.attempt_id,))[0]['wire_storage_digest'])
        # A SQL NULL mode belongs to legacy OneBot; it must not make the
        # protected-storage CHECK evaluate to UNKNOWN and admit a digest.
        scope=ConversationScope('onebot-v11','group','100')
        legacy,_=self.deliveries.enqueue(idempotency_key='TEST_LEGACY_NULL_MODE',
            source_scope_key=scope.key,target_scope=scope,
            body=MessageBody((TextNode(0,'TEST legacy'),)),now=self.now)
        self.assertIsNotNone(self.deliveries.begin_direct_attempt(legacy.delivery_id,now=self.now))
        with self.assertRaises(DatabaseError):
            self.query('UPDATE delivery_attempts SET wire_storage_digest=? WHERE delivery_id=?',
                ('b'*64,legacy.delivery_id))
        legacy_attempt=self.query('SELECT payload_mode,wire_storage_digest FROM delivery_attempts WHERE delivery_id=?',
            (legacy.delivery_id,))[0]
        self.assertIsNone(legacy_attempt['payload_mode'])
        self.assertIsNone(legacy_attempt['wire_storage_digest'])

    def test_exact_journal_link_and_receipt_commit_are_atomic(self):
        turn=self.journal.start_turn(self.endpoint.scope,trigger_canonical_message_id=self.source.canonical_message_id,
            objective='TEST attachment',provider='test',model='test',prompt_version='v1',tool_catalog_version='v1')
        plan=self.enqueue(turn_id=turn.turn_id)
        reservation=self.prepare(plan.delivery_ids[0])
        self.assertTrue(self.outbox.mark_dispatching(self.token,reservation))
        self.outbox.request_started(reservation)
        receipt=self.outbox.capture_receipt(reservation,{'id':'TEST_JOURNAL'})
        with patch.object(self.journal,'link_send',side_effect=RuntimeError('TEST_LINK_FAILURE')):
            with self.assertRaises(RuntimeError):
                self.outbox.finalize(self.token,receipt,ledger=self.ledger,journal=self.journal)
        self.assertFalse(self.query("SELECT * FROM messages WHERE direction='outbound'"))
        self.assertEqual(self.query('SELECT state FROM delivery_attempts')[0]['state'],'dispatched')
        self.assertEqual(self.outbox.finalize(self.token,receipt,ledger=self.ledger,journal=self.journal).status,'committed')
        self.assertEqual(len(self.query('SELECT * FROM turn_send_links')),1)
        self.assert_secret_free()


if __name__=='__main__':
    import unittest
    unittest.main()
