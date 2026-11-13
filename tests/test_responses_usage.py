import json
import unittest

from src.plugins.ai_chat.model_usage import TokenUsage
from src.plugins.ai_chat.responses_usage import ResponsesUsage

USAGE = {'input_tokens':1000,'output_tokens':50,'input_tokens_details':{'cached_tokens':600}}


def event(usage=USAGE,**extra):
    return b'data: '+json.dumps({'type':'response.completed','response':{'usage':usage,**extra}},ensure_ascii=False).encode()+b'\n\n'


class ResponsesUsageTests(unittest.TestCase):
    def test_arbitrary_chunks_utf8_crlf_and_bare_cr_preserve_one_terminal_receipt(self):
        for newline in (b'\n',b'\r\n',b'\r'):
            wire = event(note='TEST_中文').replace(b'\n',newline)
            observer = ResponsesUsage('text/event-stream; charset=utf-8')
            for byte in wire:observer.feed(bytes([byte]))
            self.assertEqual(observer.finish(),TokenUsage(1000,50,600,0))

    def test_partial_events_output_text_and_embedded_fake_usage_are_not_receipts(self):
        for wire in (event()[:-1],b'data: [DONE]\n\n',b'data: bad json\n\n',
                b'data: '+json.dumps({'type':'response.output_text.delta','delta':event().decode(),'usage':USAGE}).encode()+b'\n\n'):
            observer = ResponsesUsage('text/event-stream'); observer.feed(wire)
            self.assertFalse(observer.finish().known)

    def test_multiline_json_data_and_identical_repeat_do_not_double_count(self):
        observer = ResponsesUsage('text/event-stream')
        wire = b': heartbeat\n'+event().replace(b'"response":',b'\ndata: "response":')+event()
        observer.feed(wire)
        self.assertEqual(observer.finish(),TokenUsage(1000,50,600,0))
        observer.feed(event(USAGE|{'output_tokens':51}))
        self.assertFalse(observer.finish().known)
        observer.feed(event())
        self.assertFalse(observer.finish().known)

    def test_oversized_events_are_bounded_and_do_not_poison_later_valid_events(self):
        observer = ResponsesUsage('text/event-stream',limit=256)
        observer.feed(b'data: '+b'x'*10000)
        self.assertLessEqual(len(observer.line)+len(observer.data),512)
        observer.feed(b'\n\n'+event())
        self.assertEqual(observer.finish(),TokenUsage(1000,50,600,0))
        observer = ResponsesUsage('text/event-stream',limit=256)
        observer.feed((b'data: TEST_LINE\n'*1000)+b'\n')
        self.assertFalse(observer.finish().known)

    def test_compaction_json_is_bounded_and_never_inspects_nested_model_text(self):
        for kind in ('response','response.compaction'):
            wire = json.dumps({'object':kind,'usage':USAGE}).encode()
            observer = ResponsesUsage('application/json')
            for byte in wire:observer.feed(bytes([byte]))
            self.assertEqual(observer.finish(),TokenUsage(1000,50,600,0))
        for wire in (b'{',json.dumps({'output':[{'usage':USAGE}]}).encode(),b' '*2000):
            observer = ResponsesUsage('application/json',limit=1024); observer.feed(wire)
            self.assertFalse(observer.finish().known)
            self.assertEqual(len(observer.data),0)
