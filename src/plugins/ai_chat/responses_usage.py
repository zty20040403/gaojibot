"""Bounded usage-only observer for the host's Responses HTTP stream.

Content is forwarded unchanged elsewhere. Text deltas, tool output, embedded
JSON and VM RPC notifications cannot supply a usage receipt. Oversized or
contradictory receipts remain unknown rather than guessing a charge.
"""
import json

from .model_usage import TokenUsage


class ResponsesUsage:
    def __init__(self,content_type,*,limit=1024*1024):
        kind = content_type.split(';',1)[0].strip().lower()
        self.sse = kind=='text/event-stream'
        self.json = kind=='application/json' or kind.endswith('+json')
        self.limit = limit
        self.line,self.data = bytearray(),bytearray()
        self.skip_lf = self.drop_line = self.drop_event = False
        self.seen = self.conflict = False
        self.usage = TokenUsage.read(None)

    def feed(self,chunk):
        if not self.sse:
            if self.json and not self.drop_event:
                if len(self.data)+len(chunk)>self.limit:
                    self.data.clear(); self.drop_event = True
                else:self.data.extend(chunk)
            return
        pos = 0
        while pos<len(chunk):
            if self.skip_lf:
                self.skip_lf = False
                if chunk[pos]==10:
                    pos += 1
                    continue
            ends = [index for delimiter in (b'\r',b'\n') if (index:=chunk.find(delimiter,pos))>=0]
            end = min(ends) if ends else len(chunk)
            if not self.drop_line:
                if len(self.line)+end-pos>self.limit:
                    self.line.clear(); self.drop_line = True
                else:self.line.extend(memoryview(chunk)[pos:end])
            if end==len(chunk):break
            if self.drop_line:
                self.drop_event = True; self.data.clear()
            else:self._line(bytes(self.line))
            self.line.clear(); self.drop_line = False
            self.skip_lf = chunk[end]==13
            pos = end+1

    def _line(self,line):
        if not line:
            if self.data and not self.drop_event:self._decode(bytes(self.data),sse=True)
            self.data.clear(); self.drop_event = False
        elif (line==b'data' or line.startswith(b'data:')) and not self.drop_event:
            value = line[5:]
            if value.startswith(b' '):value = value[1:]
            if len(self.data)+len(value)+1>self.limit:
                self.data.clear(); self.drop_event = True
            else:self.data.extend(value+b'\n')

    def _decode(self,data,*,sse):
        try:payload = json.loads(data)
        except (ValueError,UnicodeError,RecursionError):return
        if not isinstance(payload,dict):return
        if sse:
            if payload.get('type') not in {'response.completed','response.incomplete','response.failed'}:return
            payload = payload.get('response')
            if not isinstance(payload,dict):return
        elif payload.get('object','response') not in {'response','response.compaction'}:return
        usage = TokenUsage.read(payload.get('usage'))
        if self.seen and usage!=self.usage:self.conflict = True
        self.seen = True
        self.usage = TokenUsage.read(None) if self.conflict else usage

    def finish(self):
        if self.json and not self.drop_event:self._decode(bytes(self.data),sse=False)
        # SSE EOF without a blank line is not a completed event. A previously
        # received terminal event remains valid even if EOF/transport fails.
        self.line.clear(); self.data.clear()
        return self.usage
