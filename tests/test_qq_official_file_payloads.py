"""Real source + native/link private vaults + protected M3, without HTTP."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import hashlib
import json
import secrets
from unittest.mock import patch

from tests.qq_official_file_fixture import FileFixture, PDF
from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.agent.control import LeaseLost
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from src.plugins.ai_chat.adapters.qq_official.file_artifacts import file_job_kind
from src.plugins.ai_chat.adapters.qq_official.file_links import ArtifactRef, FileCredentialVault, FileLinkPublisher
from src.plugins.ai_chat.adapters.qq_official.file_publications import PgPublicationStore
from src.plugins.ai_chat.adapters.qq_official.file_payloads import TrustedFilePayloads
from src.plugins.ai_chat.adapters.qq_official.file_dispatcher import OfficialFileDispatcher
from src.plugins.ai_chat.adapters.qq_official.native_media import fingerprint, parse_upload_receipt
from src.plugins.ai_chat.adapters.qq_official.native_vault import NativeUploadBinding, NativeUploadVault
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.send_store import OfficialSendStore
from src.plugins.ai_chat.adapters.qq_official.protected_plans import ProtectedBinding, PROTECTED_BODY
from src.plugins.ai_chat.adapters.qq_official.send_types import SendReservation, stable_json
from src.qq_official.outgoing import escape_platform_tags

NATIVE_SECRET = 'TEST_SECRET_NATIVE_FILE_INFO'
LINK_SECRET = 'TEST_SECRET_LINK_' + 'A' * 26
ORIGIN = 'https://download.example'


class FilePayloadTests(FileFixture):
    def setUp(self):
        super().setUp()
        self.item = self.put()
        self.file_job = self.jobs.claim_due('TEST_PAYLOAD_FILE_WORKER',
            kinds=(file_job_kind(self.account),), now=self.now)[0]
        self.link_vault = FileCredentialVault(self.blobs.root.parent/'link-vault')
        self.native_vault = NativeUploadVault(self.blobs.root.parent/'native-vault', clock=lambda:self.now)
        self.addCleanup(self.link_vault.close)
        self.addCleanup(self.native_vault.close)
        self.route_enabled = True
        self.port = TrustedFilePayloads(admission=self.admission,
            route_policy=lambda *_:self.route_enabled, link_vault=self.link_vault,
            native_vault=self.native_vault, link_origin=ORIGIN)
        with self.tx() as cursor:
            self.grant = self.port.issue(cursor, token=self.token, job=self.file_job,
                artifact_id=self.item.artifact_id, now=self.now)
            observe_ready(cursor,self.token,{'op':0,'t':'READY','d':{'session_id':'TEST_PAYLOAD_READY',
                'user':{'id':'TEST_BOT','bot':True}}},self.now)
        self.deliveries = DeliveryStore(self.database, recover_interrupted=False)
        self.addCleanup(self.deliveries.close)
        self.outbox = OfficialSendStore(self.deliveries, admission=self.admission, pepper=b'TEST_PAYLOAD_PEPPER'*2,
            clock=lambda:self.now, protected_payloads=self.port)
        self.window(source=self.handle.source_native_message_id)

    def native(self, *, state='confirmed', operation='upload_small', commitment_ok=True):
        fp = fingerprint([PDF], filename=self.item.filename, file_type=4)
        self.native_binding = NativeUploadBinding(self.item.artifact_id,secrets.token_hex(16),
            self.endpoint,fp,self.item.mime)
        media = parse_upload_receipt(200, {'file_info':NATIVE_SECRET,'file_uuid':'TEST_PRIVATE_UUID','ttl':300},
            endpoint=self.endpoint,artifact=fp,now=self.now)
        self.native_reference = self.native_vault.save(self.native_binding,media)
        self.query('''INSERT INTO qq_official_file_stage_attempts
            (stage_attempt_id,artifact_id,operation,request_commitment,job_id,job_lease_generation,
             account_epoch,state,created_at,expires_at,settled_at,receipt_commitment)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            (self.native_binding.stage_attempt_id,self.item.artifact_id,operation,'f'*64,self.file_job.job_id,
             self.file_job.lease_generation,self.token.epoch,state,self.now,self.now+60,
             None if state=='dispatched' else self.now,
             self.native_reference.binding_digest if commitment_ok else '0'*64))
        with self.tx() as cursor:
            return self.port.register_native(cursor,self.grant,self.native_binding,self.native_reference,self.now)

    def publish(self):
        async def run():
            calls = DatabaseCalls()
            dispatcher = OfficialFileDispatcher(self.token,self.files,self.port,self.outbox,None,
                calls=calls,lease_check=lambda:True,check_delivery_ready=lambda *_:True,clock=lambda:self.now)
            with self.tx() as cursor:
                _, intent, _ = dispatcher._link_intent(cursor,self.grant)
            def authority(cursor, artifact, expiry):
                row,_ = self.port.assert_grant(cursor,self.grant,self.now)
                return (artifact.artifact_id==row['artifact_id'] and artifact.sha256==row['sha256']
                    and artifact.size==row['byte_size'] and expiry==intent['publication_expires_at'] and expiry<=row['retain_until'])
            @asynccontextmanager
            async def unread(_):
                raise AssertionError('publication must not read/send bytes')
                yield  # pragma: no cover
            try:
                store = PgPublicationStore(transaction_factory=self.tx,calls=calls,
                    clock=lambda:self.now,publication_authority=authority)
                publisher = FileLinkPublisher(store=store,vault=self.link_vault,open_artifact=unread,
                    origin=ORIGIN,clock=lambda:self.now)
                ref = ArtifactRef(self.item.artifact_id,self.item.sha256,self.item.byte_size,self.item.mime,self.item.filename)
                with patch('src.plugins.ai_chat.adapters.qq_official.file_links.secrets.token_urlsafe',return_value=LINK_SECRET):
                    self.link = await publisher.publish(ref,request_key=intent['publication_request_key'],
                        expires_at=intent['publication_expires_at'])
                self.publication = await store.get(self.link.publication_ref)
            finally:
                await calls.close()
        asyncio.run(run())
        with self.tx() as cursor:
            return self.port.register_link(cursor,self.grant,self.publication.publication_ref,self.now)

    def handoff(self, ref):
        with self.tx() as cursor:
            return self.port.handoff(cursor,self.grant,ref,self.outbox,self.now)

    def binding(self, ref, plan=None):
        return ProtectedBinding(self.endpoint,self.actor,self.endpoint.scope.key,
            self.message.canonical_message_id,self.handle.source_native_message_id,None,1,1,ref.expires_at,
            plan.plan_id if plan else None,plan.delivery_ids[0] if plan else None)

    def prepare(self, plan):

        value = self.outbox.prepare(self.token, plan.delivery_ids[0], send_mode='proactive')
        self.assertIsInstance(value,SendReservation)
        return value

    def safe_rows(self):
        tables=('qq_official_file_artifacts','qq_official_file_deliveries','qq_official_file_publications',
            'qq_official_file_credentials','qq_official_file_message_plans','qq_official_file_stage_attempts',
            'delivery_plans','deliveries','delivery_attempts','delivery_receipts','messages','agent_turns')
        text=stable_json({name:[dict(row) for row in self.query('SELECT * FROM '+name)] for name in tables})
        for secret in (NATIVE_SECRET,LINK_SECRET,ORIGIN+'/files/'):
            self.assertNotIn(secret,text)
        return text

    def test_copied_cross_port_or_stale_job_cannot_issue_authority(self):
        for grant in (object(), replace(self.grant)):
            with self.assertRaisesRegex(PermissionError,'unissued'), self.tx() as cursor:
                self.port.assert_grant(cursor,grant,self.now)
        other=TrustedFilePayloads(admission=self.admission)
        with self.assertRaisesRegex(PermissionError,'unissued'), self.tx() as cursor:
            other.assert_grant(cursor,self.grant,self.now)
        with self.assertRaises(LeaseLost), self.tx() as cursor:
            self.port.issue(cursor,token=self.token,job=replace(self.file_job,lease_generation=999),
                artifact_id=self.item.artifact_id,now=self.now)

    def test_link_real_vault_to_m3_is_redacted_in_sql_repr_and_ledger(self):
        ref=self.publish()
        plan=self.handoff(ref)
        self.assertEqual(self.handoff(ref).plan_id,plan.plan_id)
        reservation=self.prepare(plan)
        pass
        wire=reservation.payload()
        self.assertIn(ORIGIN+'/files/'+LINK_SECRET,wire['content'])
        self.assertIn(self.item.sha256,wire['content'])
        self.safe_rows()
        for value in (self.port,self.grant,ref,reservation,self.link,self.publication):
            self.assertNotIn(LINK_SECRET,repr(value))
        receipt=self.outbox.capture_receipt(reservation,{'id':'TEST_LINK_RECEIPT','content':wire['content']})
        result=self.outbox.finalize(self.token,receipt,ledger=self.ledger)
        self.assertEqual(result.status,'committed')
        self.assertTrue(self.files.get(self.item.artifact_id).message_accepted)
        self.assertEqual(self.query("SELECT body_json FROM messages WHERE direction='outbound'")[0]['body_json'],PROTECTED_BODY)
        self.safe_rows()

    def test_native_stage_and_private_vault_feed_exact_m3_wire(self):
        ref=self.native()
        plan=self.handoff(ref)
        reservation=self.prepare(plan)
        pass
        wire=reservation.payload()
        self.assertEqual(wire['media'],{'file_info':NATIVE_SECRET})
        self.assertEqual(wire['msg_type'],7)
        self.assertNotIn('msg_id',wire)
        self.assertFalse(self.files.get(self.item.artifact_id).message_accepted)
        self.safe_rows()

    def test_native_unknown_stage_is_not_upload_success(self):
        with self.assertRaisesRegex(PermissionError,'evidence_unavailable'):
            self.native(state='unknown')
        self.assertEqual(self.query('SELECT * FROM qq_official_file_credentials'),[])
        self.assertEqual(self.query('SELECT * FROM delivery_plans'),[])

    def test_native_prepare_only_is_not_upload_completion(self):
        with self.assertRaisesRegex(PermissionError,'evidence_unavailable'):
            self.native(operation='upload_prepare')

    def test_native_receipt_commitment_must_match_vault_binding(self):
        with self.assertRaisesRegex(PermissionError,'evidence_unavailable'):
            self.native(commitment_ok=False)

    def test_binding_checks_every_account_actor_endpoint_and_source_field(self):
        ref=self.publish()
        binding=self.binding(ref)
        other_account=replace(self.account,environment='sandbox')
        variants=[replace(binding,endpoint=replace(self.endpoint,account=other_account)),
            replace(binding,endpoint=EndpointKey(self.account,'private','TEST_MEMBER')),
            replace(binding,requester=ActorKey(self.account,'group_member','TEST_GROUP','TEST_OTHER_ACTOR')),
            replace(binding,source_scope_key='TEST_OTHER_SCOPE'),replace(binding,source_canonical_message_id=self.message.canonical_message_id+1),
            replace(binding,source_native_message_id='TEST_OTHER_SOURCE'),replace(binding,endpoint_generation=2),
            replace(binding,route_epoch=2),replace(binding,turn_id=1),replace(binding,expires_at=ref.expires_at+1)]
        for changed in variants:
            with self.assertRaises(PermissionError), self.tx() as cursor:
                self.port.authorize_enqueue(cursor,token=self.token,source_grant=self.grant,
                    content_ref=ref,binding=changed,now=self.now)
        self.assertEqual(self.query('SELECT * FROM delivery_plans'),[])

    def test_exact_handoff_association_cannot_be_substituted(self):
        ref=self.publish()
        plan=self.handoff(ref)
        good=self.binding(ref,plan)
        for changed in (replace(good,plan_id='f'*32),replace(good,delivery_id=plan.delivery_ids[0]+1)):
            with self.assertRaises(PermissionError), self.tx() as cursor:
                self.port.resolve(cursor,token=self.token,content_ref=ref,binding=changed,now=self.now)
        with self.tx() as cursor:
            content=self.port.resolve(cursor,token=self.token,content_ref=ref,binding=good,now=self.now)
        self.assertIn(LINK_SECRET,content['content'])

    def test_replayed_handoff_rejects_changed_full_reference(self):
        ref=self.publish()
        plan=self.handoff(ref)
        variants=[replace(ref,artifact_id='f'*32),replace(ref,content_commitment='0'*64),
                  replace(ref,expires_at=ref.expires_at+1),replace(ref,route='native')]
        for changed in variants:
            with self.subTest(field=changed.route), self.assertRaises(PermissionError):
                self.handoff(changed)
        self.assertEqual(len(self.query('SELECT * FROM delivery_plans')),1)
        self.assertEqual(self.handoff(ref).delivery_ids,plan.delivery_ids)



    def test_historical_receipt_after_vault_deletion_and_revoke_still_settles(self):
        ref=self.publish(); plan=self.handoff(ref); reservation=self.prepare(plan)
        pass
        reservation.payload()
        self.link_vault.delete(ref.credential_ref)
        self.admitted=False
        self.query('UPDATE qq_official_endpoints SET route_epoch=route_epoch+1 WHERE endpoint_key=?',(self.endpoint.key,))
        self.now=ref.expires_at+1
        with patch.object(self.port,'resolve',side_effect=AssertionError('historical secret read')):
            receipt=self.outbox.capture_receipt(reservation,{'id':'TEST_LATE_RECEIPT'})
            result=self.outbox.finalize(self.token,receipt,ledger=self.ledger)
        self.assertEqual(result.status,'committed')
        self.assertTrue(self.files.get(self.item.artifact_id).message_accepted)
        self.assertEqual(len(self.query('SELECT * FROM delivery_attempts')),1)
        self.safe_rows()

    def test_native_historical_receipt_survives_private_vault_deletion(self):
        ref=self.native(); plan=self.handoff(ref); reservation=self.prepare(plan)
        self.assertFalse(self.files.get(self.item.artifact_id).message_accepted)
        pass
        reservation.payload()
        self.native_vault.delete(self.native_reference,self.native_binding)
        self.query("UPDATE qq_official_turn_executions SET state='needs_attention' WHERE execution_id=?",(self.handle.execution_id,))
        self.now=ref.expires_at+1
        with patch.object(self.port,'resolve',side_effect=AssertionError('historical vault read')):
            receipt=self.outbox.capture_receipt(reservation,{'id':'TEST_LATE_NATIVE'})
            self.assertEqual(self.outbox.finalize(self.token,receipt,ledger=self.ledger).status,'committed')
        self.assertTrue(self.files.get(self.item.artifact_id).message_accepted)
        self.safe_rows()

    def test_new_send_fails_closed_if_link_vault_is_missing(self):
        ref=self.publish(); plan=self.handoff(ref)
        self.link_vault.delete(ref.credential_ref)
        with self.assertRaisesRegex(PermissionError,'credential_unavailable'):
            self.prepare(plan)
        self.assertEqual(self.query('SELECT * FROM delivery_attempts'),[])

    def test_native_draft_is_denied_and_link_preserves_literal_full_reason(self):
        # New immutable draft artifact; no modification of the original manifest.
        reason='内容待核实 <qqbot-at-user id="TEST_OTHER"/> 尾部完整说明'
        self.item=self.files.register(self.source,content=PDF,filename='draft.pdf',
            logical_request_key=hashlib.sha256(b'TEST_DRAFT').hexdigest(),draft=True,draft_reason=reason)
        self.assertTrue(self.jobs.mark_succeeded(self.file_job.job_id,self.file_job.lease_owner,
            now=self.now,lease_generation=self.file_job.lease_generation))
        self.file_job=self.jobs.claim_due('TEST_DRAFT_FILE_WORKER',kinds=(file_job_kind(self.account),),now=self.now)[0]
        with self.tx() as cursor:
            self.grant=self.port.issue(cursor,token=self.token,job=self.file_job,artifact_id=self.item.artifact_id,now=self.now)
        with self.assertRaisesRegex(PermissionError,'native_draft'):
            self.native()
        ref=self.publish(); plan=self.handoff(ref); reservation=self.prepare(plan)
        wire=json.loads(reservation.wire_json)
        self.assertNotIn('<qqbot',wire['content'])
        self.assertIn(escape_platform_tags(reason),wire['content'])
        self.assertIn(self.item.filename,wire['content'])
