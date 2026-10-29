"""Candidate host-only scoped media lookup; references are not capabilities.

The caller supplies its actual executing Actor/Endpoint. No event, raw URL,
numeric QQ identity, cross-route history alias or user-selected target enters
this API. Each read rechecks the host source, then the physical ledger origin.
"""
from dataclasses import dataclass, field
import hashlib
import re

from src.plugins.ai_chat.message_ir import CardNode, ForwardNode, MediaNode, TextNode, body_to_json
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey


class MediaDenied(ValueError):
    """Fixed safe reasons only; no URL, ASR, identifiers or provider exception."""


@dataclass(frozen=True)
class MediaRef:
    canonical_id: int
    segment_index: int

    def __post_init__(self):
        if (type(self.canonical_id) is not int or self.canonical_id < 1
                or type(self.segment_index) is not int or not 0 <= self.segment_index < 512):
            raise MediaDenied('invalid_media_reference')


@dataclass(frozen=True)
class MediaSource:
    ref: MediaRef
    endpoint: EndpointKey = field(repr=False)
    requester: ActorKey = field(repr=False)
    node: MediaNode | CardNode | ForwardNode | TextNode = field(repr=False)
    body_digest: str = field(repr=False)


class LedgerMediaResolver:
    def __init__(self, *, endpoint, requester, ledger, db_call, current):
        if (not isinstance(endpoint, EndpointKey) or endpoint.account.platform != 'qq-official'
                or not isinstance(requester, ActorKey) or not requester.matches_endpoint(endpoint)
                or not callable(current) or not callable(db_call)):
            raise MediaDenied('invalid_media_context')
        self.endpoint, self.requester = endpoint, requester
        self.ledger, self.db_call, self.current = ledger, db_call, current

    async def resolve(self, ref):
        if not isinstance(ref, MediaRef):
            raise MediaDenied('invalid_media_reference')
        if await self.current(self.endpoint, self.requester) is not True:
            raise MediaDenied('media_source_unavailable')
        row = await self.db_call(self.ledger.get_in_scope, self.endpoint.scope, ref.canonical_id)
        if (row is None or row.canonical_message_id != ref.canonical_id
                or row.scope_key != self.endpoint.scope.key
                or row.source_endpoint_key != self.endpoint.key
                or row.message_kind != 'chat' or row.direction != 'inbound'):
            raise MediaDenied('media_reference_unavailable')
        matches = [n for n in row.body.nodes if n.segment_index == ref.segment_index]
        if len(matches) != 1 or not isinstance(matches[0], (MediaNode, CardNode, ForwardNode, TextNode)):
            raise MediaDenied('media_reference_unavailable')
        node = matches[0]
        # Freeze nested transport evidence: downstream calls never trust mutated
        # DTO dictionaries. The digest is integrity evidence, never permission.
        from src.plugins.ai_chat.message_ir import body_from_json
        raw = body_to_json(row.body)
        node = next(n for n in body_from_json(raw).nodes if n.segment_index == ref.segment_index)
        return MediaSource(ref, self.endpoint, self.requester, node, hashlib.sha256(raw.encode()).hexdigest())

    async def require(self, source):
        if (not isinstance(source, MediaSource) or source.endpoint != self.endpoint
                or source.requester != self.requester):
            raise MediaDenied('media_source_unavailable')
        latest = await self.resolve(source.ref)
        if latest.body_digest != source.body_digest or latest.node != source.node:
            raise MediaDenied('media_reference_changed')


@dataclass(frozen=True)
class ExternalText:
    source: str
    text: str = field(repr=False)
    truncated: bool = False
    # Explicit data classification; never consume as commands/owner evidence.
    trust: str = 'external_content'


def bounded_text(text, maximum=12000):
    if not isinstance(text, str) or '\x00' in text:
        raise MediaDenied('invalid_external_text')
    try:
        raw = text.encode('utf-8')
    except UnicodeError:
        raise MediaDenied('invalid_external_text') from None
    return raw[:maximum].decode('utf-8', errors='ignore'), len(raw) > maximum


async def transcribe(source, *, resolver, fetcher, local_asr=None):
    await resolver.require(source)
    node = source.node
    if not isinstance(node, MediaNode) or node.media_kind != 'audio':
        raise MediaDenied('voice_reference_required')
    text = node.raw_data.get('asr_refer_text')
    if (node.source_type == 'qq-official.attachment'
            and node.raw_data.get('asr_source') == 'platform_external_content'
            and isinstance(text, str) and text.strip()):
        bounded, truncated = bounded_text(text)
        return ExternalText('tencent_asr', bounded, truncated)
    if local_asr is None:
        return ExternalText('unrecognized', '')
    # Never infer a codec from a .wav URL suffix or trust declared MIME.
    content = await fetcher.fetch(source, resolver=resolver, audio=True)
    await resolver.require(source)
    result = await local_asr.transcribe(content.content, current=lambda: resolver.require(source))
    await resolver.require(source)
    bounded, truncated = bounded_text(result)
    return ExternalText('configured_local_asr', bounded, truncated)


def text_link(node):
    """One literal HTTPS link in this exact message segment, not a tool URL.

    Ambiguous multi-link segments require a more specific source; no model
    chooses a replacement URL or drives redirects. Full pinning happens later.
    """
    if not isinstance(node, TextNode):
        return None
    found = []
    for match in re.finditer(r'https://[^\s<>"\']+', node.text):
        value = match.group(0)
        if len(value) > 8192 or found:
            return None
        found.append(value)
    return found[0] if found else None
