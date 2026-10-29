"""Protected M3 wire storage codec; no I/O, queue or send authority.

The live reservation holds actual wire bytes in memory. Durable attempt storage
keeps a versioned envelope with safe routing and independent integrity hashes.
Historical receipts can be finalized after protected credentials expire.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re


class ProtectedPayloadError(ValueError):
    pass


def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def commitment(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _hex(value,size):
    return isinstance(value,str) and re.fullmatch('[a-f0-9]{'+str(size)+'}',value) is not None


def _native(value):
    return (isinstance(value,str) and 1 <= len(value.encode()) <= 256
            and not any(ord(x)<32 or ord(x)==127 for x in value))


@dataclass(frozen=True,repr=False)
class ProtectedContentRef:
    artifact_id: str
    credential_ref: str
    route: str
    content_commitment: str
    expires_at: int

    def __post_init__(self):
        if (not _hex(self.artifact_id,32) or not _hex(self.credential_ref,32)
                or self.route not in {'native','link'} or not _hex(self.content_commitment,64)
                or type(self.expires_at) is not int or not 0 < self.expires_at <= 2**63-1):
            raise ProtectedPayloadError('invalid_protected_reference')

    def data(self):
        return dict(artifact_id=self.artifact_id,credential_ref=self.credential_ref,route=self.route,
            content_commitment=self.content_commitment,expires_at=self.expires_at)

    def __repr__(self):
        return 'ProtectedContentRef(<protected>)'


def _routing(value):
    if not isinstance(value,dict) or set(value)-{'msg_id','event_id','msg_seq','message_reference'}:
        raise ProtectedPayloadError('invalid_protected_routing')
    credential = 'msg_id' in value or 'event_id' in value
    if 'msg_id' in value and 'event_id' in value:
        raise ProtectedPayloadError('invalid_protected_routing')
    if credential != ('msg_seq' in value):
        raise ProtectedPayloadError('invalid_protected_routing')
    if credential:
        key='msg_id' if 'msg_id' in value else 'event_id'
        if not _native(value[key]) or type(value['msg_seq']) is not int or not 1 <= value['msg_seq'] <= 65535:
            raise ProtectedPayloadError('invalid_protected_routing')
    if 'message_reference' in value:
        ref=value['message_reference']
        if not isinstance(ref,dict) or set(ref)!={'message_id'} or not _native(ref['message_id']):
            raise ProtectedPayloadError('invalid_protected_routing')
    return value


def validate_content(ref,content):
    if not isinstance(ref,ProtectedContentRef) or not isinstance(content,dict):
        raise ProtectedPayloadError('invalid_protected_content')
    if ref.route=='native':
        if (set(content)!={'msg_type','media'} or type(content['msg_type']) is not int
                or content['msg_type']!=7 or not isinstance(content['media'],dict)
                or set(content['media'])!={'file_info'} or not isinstance(content['media']['file_info'],str)
                or not 1 <= len(content['media']['file_info'].encode()) <= 65536
                or any(ord(c)<32 for c in content['media']['file_info'])):
            raise ProtectedPayloadError('invalid_protected_content')
    elif (set(content)!={'msg_type','content'} or type(content['msg_type']) is not int
            or content['msg_type']!=0 or not isinstance(content['content'],str)
            or not 1 <= len(content['content'].encode()) <= 16384):
        raise ProtectedPayloadError('invalid_protected_content')
    # The trusted publisher builds the exact text/URL. This hash is not a grant;
    # source authority and the persisted publication binding are checked by M3.
    if commitment(content)!=ref.content_commitment:
        raise ProtectedPayloadError('protected_content_changed')
    return content


def envelope_for_wire(ref,wire_json):
    if not isinstance(wire_json,str) or len(wire_json.encode())>262144:
        raise ProtectedPayloadError('invalid_protected_wire')
    try:
        wire=json.loads(wire_json)
    except (TypeError,ValueError):
        raise ProtectedPayloadError('invalid_protected_wire') from None
    if not isinstance(wire,dict) or _json(wire)!=wire_json:
        raise ProtectedPayloadError('noncanonical_protected_wire')
    routing={k:v for k,v in wire.items() if k in {'msg_id','event_id','msg_seq','message_reference'}}
    _routing(routing)
    validate_content(ref,{k:v for k,v in wire.items() if k not in routing})
    wire_digest=hashlib.sha256(wire_json.encode()).hexdigest()
    envelope={'v':1,'mode':'protected','content_ref':ref.data(),'routing':routing,'wire_digest':wire_digest}
    stored=_json(envelope)
    return stored,hashlib.sha256(stored.encode()).hexdigest(),wire_digest


def verify_envelope(stored,*,storage_digest,wire_digest,ref=None,wire_json=None):
    if (not isinstance(stored,str) or len(stored.encode())>8192
            or not _hex(storage_digest,64) or not _hex(wire_digest,64)
            or hashlib.sha256(stored.encode()).hexdigest()!=storage_digest):
        raise ProtectedPayloadError('protected_envelope_corrupt')
    try:
        envelope=json.loads(stored)
        if (not isinstance(envelope,dict) or set(envelope)!={'v','mode','content_ref','routing','wire_digest'}
                or type(envelope['v']) is not int or envelope['v']!=1 or envelope['mode']!='protected'
                or envelope['wire_digest']!=wire_digest or _json(envelope)!=stored
                or not isinstance(envelope['content_ref'],dict)):
            raise ValueError
        restored=ProtectedContentRef(**envelope['content_ref'])
        _routing(envelope['routing'])
        if ref is not None and restored!=ref:
            raise ValueError
        if wire_json is not None:
            rebuilt,expected_storage,expected_wire=envelope_for_wire(restored,wire_json)
            if (rebuilt,expected_storage,expected_wire)!=(stored,storage_digest,wire_digest):
                raise ValueError
        return restored,dict(envelope['routing'])
    except (ValueError,TypeError,KeyError):
        raise ProtectedPayloadError('protected_envelope_corrupt') from None
