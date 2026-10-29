from dataclasses import replace
import json
import unittest
from src.plugins.ai_chat.adapters.qq_official.protected_payload import (ProtectedContentRef,ProtectedPayloadError,commitment,
                               envelope_for_wire,verify_envelope,validate_content)


def wire(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


class ProtectedTests(unittest.TestCase):
    def setUp(self):
        self.payload={'msg_type':0,'content':'TEST report.pdf\nhttps://downloads.example/files/TEST_SECRET_256bit_token'}
        self.ref=ProtectedContentRef('a'*32,'b'*32,'link',commitment(self.payload),1800000300)
        self.actual=wire({**self.payload,'msg_id':'TEST_SOURCE','msg_seq':1,'message_reference':{'message_id':'TEST_INDEX'}})

    def test_wire_secret_absent_from_durable_envelope(self):
        stored,meta,digest=envelope_for_wire(self.ref,self.actual)
        self.assertNotIn('TEST_SECRET',stored)
        self.assertNotIn('downloads.example',stored)
        self.assertNotIn('report.pdf',stored)
        ref,routing=verify_envelope(stored,storage_digest=meta,wire_digest=digest,ref=self.ref,wire_json=self.actual)
        self.assertEqual(ref,self.ref)
        self.assertEqual(routing['msg_seq'],1)

    def test_historical_evidence_needs_no_current_credentials(self):
        stored,meta,digest=envelope_for_wire(self.ref,self.actual)
        self.assertEqual(verify_envelope(stored,storage_digest=meta,wire_digest=digest)[0],self.ref)

    def test_native_has_no_accidental_text_or_service_send(self):
        payload={'msg_type':7,'media':{'file_info':'TEST_SECRET_FILE_INFO'}}
        ref=replace(self.ref,route='native',content_commitment=commitment(payload))
        stored,meta,digest=envelope_for_wire(ref,wire(payload))
        self.assertNotIn('TEST_SECRET',stored)
        self.assertEqual(verify_envelope(stored,storage_digest=meta,wire_digest=digest)[1],{})
        for extra in ({'srv_send_msg':True},{'content':' '},{'msg_type':True}):
            with self.assertRaises(ProtectedPayloadError):
                envelope_for_wire(ref,wire({**payload,**extra}))

    def test_modified_secret_or_route_does_not_match_frozen_attempt(self):
        stored,meta,digest=envelope_for_wire(self.ref,self.actual)
        for changed in (self.actual.replace('256bit','new256bit'),self.actual.replace('TEST_SOURCE','OTHER_SOURCE'),
                        self.actual.replace('TEST_INDEX','OTHER_INDEX'),self.actual.replace('"msg_seq":1','"msg_seq":2')):
            with self.assertRaises(ProtectedPayloadError):
                verify_envelope(stored,storage_digest=meta,wire_digest=digest,wire_json=changed)
        with self.assertRaises(ProtectedPayloadError):
            verify_envelope(stored,storage_digest=meta,wire_digest=digest,ref=replace(self.ref,artifact_id='c'*32))

    def test_malformed_or_noncanonical_wire_rejected(self):
        for bad in ('{}','[]','null',json.dumps({**self.payload,'msg_id':'TEST','msg_seq':1}),
                    wire({**self.payload,'msg_id':'TEST'}),wire({**self.payload,'msg_seq':1}),
                    wire({**self.payload,'msg_id':'TEST','event_id':'TEST','msg_seq':1}),
                    wire({**self.payload,'message_reference':{'message_id':'TEST','extra':'TEST'}})):
            with self.assertRaises(ProtectedPayloadError):
                envelope_for_wire(self.ref,bad)

    def test_envelope_corruption_does_not_become_receipt_evidence(self):
        stored,meta,digest=envelope_for_wire(self.ref,self.actual)
        for value,checksum in ((stored.replace('TEST_SOURCE','TEST_OTHER'),meta),(stored,'0'*64),
                               (stored.replace('"v":1','"v":true'),meta)):
            with self.assertRaises(ProtectedPayloadError):
                verify_envelope(value,storage_digest=checksum,wire_digest=digest)
        with self.assertRaises(ProtectedPayloadError):
            validate_content(self.ref,{**self.payload,'content':'changed'})


if __name__=='__main__':
    unittest.main()
