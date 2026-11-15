"""Keep private ask exchanges out of ordinary source-turn body logging."""
from dataclasses import replace
import json


class PrivateQuestionAudit:
    def __init__(self,requests,trace):
        self.requests,self.trace = requests,trace
        self.private = False

    def batch(self,names):
        # Runs before this assistant batch's model_note or tool events: a note
        # can already quote the private question before tool_started arrives.
        if 'ask' in names:
            self.private = True
            self.requests.clear()

    def request(self,request):
        if not self.private:self.requests[:] = [request]

    def event(self,event):
        if event.tool_name=='ask':self.batch(('ask',))
        if not self.private:return event
        # Redact every later event, including tainted follow-up tool arguments
        # and unkeyed argument fingerprints. Private CQ audit keeps the bodies.
        return replace(event,arguments={},result=json.dumps({'private_question':True,'state':event.state}),
            note='私有问答正文仅保存在独立问答审计。' if event.kind=='model_note' else '',fingerprint='')

    def finish(self):
        if self.private and self.trace is not None:
            self.trace.messages.clear()
            self.trace.execution_decisions.clear()
            self.trace.model_routing.clear()
