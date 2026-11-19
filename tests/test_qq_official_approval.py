"""Real isolated PG and fake loopback M3, using unapplied source overlay."""
from dataclasses import replace
import json
import unittest

from src.bot_security.store import SecurityStore, SecurityError
from src.plugins.ai_chat.adapters.qq_official.approval import (
 OfficialApprovalTransport)
from src.plugins.ai_chat.adapters.qq_official.validation import Denied
from src.qq_official.events import parse_dispatch
from src.plugins.ai_chat.adapters.qq_official.input_normalization import normalize_input, retired_approval_input

from tests.qq_official_approval_fixture import ApprovalFixture, SECRET

class ApprovalStoreTests(ApprovalFixture):
 def test_creation_uses_real_admin_source_and_same_queue(self):
  request,code=self.propose()
  self.assertEqual(len(code),6)
  self.assertEqual((request['quorum'],request['qq_id'],request['bot_id']),(2,None,None))
  rows=self.query('SELECT * FROM qq_official_admin_otp_sources')
  self.assertEqual((len(rows),rows[0]['expires_at']-self.now),(1,30))
  self.assertEqual(rows[0]['window_id'],self.window_a)
  self.assertNotIn(code,json.dumps([dict(row) for row in rows]))
  self.assertNotIn(code,json.dumps([dict(row) for row in self.query('SELECT * FROM admin_approvals')]))
  self.assertEqual(self.propose()[1],None)
 def test_default_source_port_denies(self):
  self.transport.source_port=OfficialApprovalTransport(token=self.token,secret=SECRET).source_port
  with self.assertRaises(SecurityError): self.propose()
  self.assertFalse(self.query('SELECT * FROM admin_approvals'))
 def test_wrong_source_actor_endpoint_scope_or_epoch_does_not_authorize(self):
  variants=[replace(self.source,actor=self.other_actor,endpoint=self.other),replace(self.source,route_epoch=1),
    replace(self.source,endpoint_route_epoch=2),replace(self.source,revision=2),replace(self.source,scope=self.other.scope)]
  for source in variants:
   with self.subTest(source=source),self.assertRaises(SecurityError):
    context=replace(self.context,source=source);self.propose(payload=context.payload(),transport_context=context)
 def test_source_loss_and_owner_revoke_fail_closed(self):
  self.source_live=False
  with self.assertRaises(SecurityError): self.propose()
  self.source_live=True
  with self.tx() as c: self.owners.apply_trusted_config(c,self.account,('TEST_ADMIN_B',),'TEST_REVOKED')
  with self.assertRaises(Denied): self.propose()
 def test_delivery_callback_cannot_claim_success_without_real_receipt(self):
  request,_=self.propose();self.store.delivered(request['approval_id'],1,True)
  row=self.query('SELECT * FROM admin_approvals')[0]
  self.assertEqual((row['status'],row['code_hash']),('cancelled',''))
 def test_restart_cancels_unsent_code_without_resigning(self):
  request,_=self.propose()
  with self.tx() as c:self.transport.recover(c)
  row=self.query('SELECT * FROM admin_approvals')[0]
  self.assertEqual((row['status'],row['code_hash']),('cancelled',''))
 def test_legacy_store_never_claims_official_queue(self):
  self.propose()
  old=SecurityStore(self.database,SECRET,clock=lambda:self.now);self.addCleanup(old.close)
  self.assertIsNone(old.claim('TEST_LEGACY_WORKER'))
  with self.assertRaises(SecurityError):old.confirm(None,'12345678','87654321','123456')
 def test_policy_exact_operation_target_and_arguments(self):
  self.assertTrue(self.policy.permits(operation='fleet.job.create',target_id='TEST_HOST',arguments=self.arguments))
  for changes in ({'target_id':'TEST_OTHER'},{'arguments':{}},{'operation':'arbitrary.shell'}):
   self.assertFalse(self.policy.permits(**(dict(operation='fleet.job.create',target_id='TEST_HOST',arguments=self.arguments)|changes)))

class InputPrivacyTests(unittest.TestCase):
 def test_trusted_mentions_and_embedded_approval_privacy(self):
  cases=[('123456',[],'control'),('<@!TEST_FAKE> 123456',[],'quarantine'),('/confirm AP-ABCDEF123456 123456',[],'control'),
   ('<@!TEST_BOT> /confirm AP-ABCDEF123456 123456',[{'id':'TEST_BOT','is_you':True}],'control'),
   ('<@!TEST_BOT> /confirm AP-ABCDEF123456 123456',[],'quarantine'),
   ('quoted /confirm AP-ABCDEF123456 123456',[],'quarantine'),('ordinary chat',[],None)]
  for text,mentions,kind in cases:
   with self.subTest(kind=kind):
    dispatch=parse_dispatch({'op':0,'t':'C2C_MESSAGE_CREATE','s':1,'d':{'id':'TEST_MESSAGE','content':text,
       'author':{'user_openid':'TEST_OWNER'},'mentions':mentions}})
    self.assertEqual(retired_approval_input(normalize_input(dispatch),text,private=True),kind)
  dispatch=parse_dispatch({'op':0,'t':'GROUP_AT_MESSAGE_CREATE','s':1,'d':{'id':'TEST_MESSAGE',
    'group_openid':'TEST_GROUP','content':'/confirm AP-ABCDEF123456 123456','author':{'member_openid':'TEST_MEMBER'}}})
  self.assertEqual(retired_approval_input(normalize_input(dispatch),dispatch.message.content,private=False),'quarantine')
