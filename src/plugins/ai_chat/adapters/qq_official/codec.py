"""QQ official inbound codec; no persistence, authorization or network I/O.

Only authenticated transport input may reach this adapter.  ``raw_data`` and
quoted authors remain content evidence, never membership or send authority.
Ingest must resolve references against the exact account and actual endpoint.
This module intentionally does not create InboundMessage or ReplyCredential.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
import json
import re
from typing import Any, Literal
import unicodedata
from urllib.parse import urlsplit

from src.qq_official.events import Dispatch, EventParseError, MessageElement, parse_dispatch
from src.plugins.ai_chat.message_ir import (
    CardNode, EmoteNode, ForwardNode, MediaNode, MentionNode, MessageBody,
    TextNode, UnsupportedNode, body_to_json,
)
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey

MAX_NODES = 512
MAX_ELEMENTS = 128
MAX_ELEMENT_DEPTH = 8
MAX_IDENTIFIER_BYTES = 256
MAX_ACCOUNT_KEY_BYTES = 512
MAX_ENDPOINT_KEY_BYTES = 1000
MAX_ACTOR_KEY_BYTES = 1280
MAX_FACE_BYTES = 4096
MAX_URL_CHARS = 8192
_TOKEN = re.compile(
    r'<@([^<>\r\n]{1,2048})>|'
    r'<faceType=(\d{1,3}),faceId="([^"<>\r\n]{1,128})",ext="([A-Za-z0-9+/=]{0,8192})">'
)
_KNOWN_FIELDS = frozenset({
    "id", "author", "group_openid", "content", "timestamp", "mentions",
    "message_type", "message_scene", "attachments", "msg_elements", "ark_data",
})


@dataclass(frozen=True)
class MentionEvidence:
    native_user_id: str | None = field(repr=False)
    display: str = field(repr=False)
    is_you: bool = False
    is_bot: bool = False
    scope: str = ""
    usable_group_identity: bool = False
    conflicting: bool = False


@dataclass(frozen=True)
class AuthorEvidence:
    """Quoted authors are not ActorKeys and never establish current membership."""
    native_user_id: str | None = field(repr=False)
    display: str | None = field(repr=False)
    platform_role: str = ""
    is_bot: bool = False


@dataclass(frozen=True)
class ElementEvidence:
    message_index: str | None = field(repr=False)
    author: AuthorEvidence | None = field(repr=False)
    body: MessageBody = field(repr=False)
    children: tuple[ElementEvidence, ...] = field(default=(), repr=False)
    truncated: bool = False


@dataclass(frozen=True)
class ReferenceEvidence:
    endpoint: EndpointKey = field(repr=False)
    message_index: str | None = field(repr=False)
    referenced_index: str | None = field(repr=False)
    conflicting: bool = False
    # No reply_to_native_message_id: only ingest can prove an index lookup.


@dataclass(frozen=True)
class ConversionEvidence:
    mentions: tuple[MentionEvidence, ...] = field(default=(), repr=False)
    elements: tuple[ElementEvidence, ...] = field(default=(), repr=False)
    unknown_field_names: tuple[str, ...] = field(default=(), repr=False)
    warnings: tuple[str, ...] = ()
    truncated: bool = False
    message_type: int | None = None


@dataclass(frozen=True)
class DecodedMessage:
    endpoint: EndpointKey = field(repr=False)
    actor: ActorKey = field(repr=False)
    native_message_id: str = field(repr=False)
    event_id: str | None = field(repr=False)
    sender_display: str = field(repr=False)
    platform_role: str
    body: MessageBody = field(repr=False)
    mentions_bot: bool
    occurred_at: int
    received_at: int
    platform_occurred_at: int | None
    timestamp_source: Literal["platform", "received_missing", "received_future"]
    reference: ReferenceEvidence = field(repr=False)
    evidence: ConversionEvidence = field(repr=False)


@dataclass(frozen=True)
class RejectedMessage:
    reason: str
    disposition: Literal["quarantine", "ignored", "bot_origin"] = "quarantine"
    endpoint: EndpointKey | None = field(default=None, repr=False)
    native_message_id: str | None = field(default=None, repr=False)
    event_id: str | None = field(default=None, repr=False)
    # Transport supplied member/user ID, not an established self-bot identity.
    author_native_id: str | None = field(default=None, repr=False)


def _identifier(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        return None
    return value if len(value.encode("utf-8")) <= MAX_IDENTIFIER_BYTES else None


def _label(value: Any, limit: int = 512) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _contains_nul(value: Any) -> bool:
    # PostgreSQL TEXT/JSONB cannot store NUL, including an escaped JSON NUL.
    # The enclosing M0 parser has already bounded depth and total node count.
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_contains_nul(key) or _contains_nul(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_nul(item) for item in value)
    return False


def _url(value: Any) -> str:
    if not isinstance(value, str) or len(value) > MAX_URL_CHARS or any(ord(c) <= 32 or unicodedata.category(c) in {"Cc", "Cf"} for c in value):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            return ""
        _ = parsed.port  # Reject invalid ports as well as malformed bracketed hosts.
        return value
    except ValueError:
        return ""


class _Budget:
    def __init__(self):
        self.nodes = 0
        self.elements = 0
        self.truncated = False
        self.warnings: set[str] = set()

    def warn(self, reason: str):
        self.warnings.add(reason)


class _Body:
    def __init__(self, budget: _Budget):
        self.budget = budget
        self.nodes: list[Any] = []

    def add(self, node_type, *args, **kwargs):
        if self.budget.nodes >= MAX_NODES:
            self.budget.truncated = True
            self.budget.warn("node_limit")
            return
        self.nodes.append(node_type(len(self.nodes), *args, **kwargs))
        self.budget.nodes += 1

    def text(self, value: str):
        if not value:
            return
        if self.nodes and isinstance(self.nodes[-1], TextNode):
            old = self.nodes[-1]
            self.nodes[-1] = TextNode(old.segment_index, old.text + value)
        else:
            self.add(TextNode, value)

    def finish(self) -> MessageBody:
        return MessageBody(tuple(self.nodes))


def _mentions(message) -> tuple[MentionEvidence, ...]:
    result = []
    for item in message.mentions:
        native = _identifier(item.get("member_openid"))
        candidates = {_identifier(item.get(key)) for key in ("member_openid", "user_openid", "id")}
        candidates.discard(None)
        conflict = len(candidates) > 1
        scope = item.get("scope") if item.get("scope") in ("single", "all") else ""
        result.append(MentionEvidence(
            native, _label(item.get("nickname") or item.get("username")),
            item.get("is_you") is True, item.get("bot") is True, scope,
            message.kind == "group" and native is not None and scope != "all" and not conflict,
            conflict,
        ))
    return tuple(result)


def _text_nodes(body: _Body, text: str, mentions: tuple[MentionEvidence, ...]):
    usable: dict[str, MentionEvidence] = {}
    blocked: set[str] = set()
    for item in mentions:
        if item.native_user_id and item.usable_group_identity:
            previous = usable.get(item.native_user_id)
            if previous is not None and previous != item:
                blocked.add(item.native_user_id)
            usable[item.native_user_id] = item
        elif item.native_user_id:
            blocked.add(item.native_user_id)
    offset = 0
    for match in _TOKEN.finditer(text):
        if body.budget.nodes >= MAX_NODES - 1:
            body.text(text[offset:])  # Preserve remaining literal text within the final slot.
            body.budget.warn("token_limit")
            return
        body.text(text[offset:match.start()])
        native, face_type, face_id, encoded = match.groups()
        mention = usable.get(native) if native not in blocked else None
        if native is not None and mention is not None:
            body.add(MentionNode, native, mention.display, raw_data={
                "source": "qq-official.mentions", "is_you": mention.is_you,
                "bot": mention.is_bot,
            })
        elif face_type in {"1", "3", "6"}:
            name = ""
            try:
                if len(encoded) > 4 * ((MAX_FACE_BYTES + 2) // 3):
                    raise ValueError
                decoded = base64.b64decode(encoded, validate=True)
                if len(decoded) > MAX_FACE_BYTES:
                    raise ValueError
                data = json.loads(decoded) if decoded else {}
                if not isinstance(data, dict):
                    raise ValueError
                name = _label(data.get("text"), 256) if face_type != "6" else ""
            except (ValueError, UnicodeError, binascii.Error, RecursionError):
                body.budget.warn("invalid_face_metadata")
            body.add(EmoteNode, face_id, name, raw_data={"face_type": int(face_type)})
        else:
            body.text(match.group())
        offset = match.end()
    body.text(text[offset:])


def _media_nodes(body: _Body, attachments, *, sticker: bool = False):
    for item in attachments:
        mime = _label(item.content_type, 128).lower().split(";", 1)[0].strip()
        primary, wav = _url(item.url), _url(item.voice_wav_url)
        if mime.startswith("image/"):
            kind = "sticker" if sticker else "image"
        elif mime.startswith("video/"):
            kind = "video"
        elif mime.startswith("audio/") or wav:
            kind = "audio"
        else:
            kind = "file"
        raw = {"size": item.size if item.size is not None and item.size >= 0 else None}
        if wav:
            raw["voice_wav_url"] = wav
        if item.asr_refer_text is not None:
            raw["asr_refer_text"] = item.asr_refer_text
            raw["asr_source"] = "platform_external_content"
        if item.url and not primary or item.voice_wav_url and not wav:
            body.budget.warn("unsafe_media_url")
        body.add(MediaNode, kind, source=primary or wav, name=_label(item.filename),
                 mime=mime, source_type="qq-official.attachment", raw_data=raw)


def _elements(values: tuple[MessageElement, ...], budget: _Budget, depth: int = 0) -> tuple[ElementEvidence, ...]:
    result = []
    for item in values:
        if depth >= MAX_ELEMENT_DEPTH or budget.elements >= MAX_ELEMENTS:
            budget.truncated = True
            budget.warn("element_limit")
            break
        budget.elements += 1
        body = _Body(budget)
        # Top-level mention evidence cannot authenticate text inside a quote.
        _text_nodes(body, item.content, ())
        _media_nodes(body, item.attachments)
        author = item.author
        author_evidence = None if author is None else AuthorEvidence(
            _identifier(author.native_id), _label(author.display_name) or None,
            author.platform_role, author.is_bot,
        )
        children = _elements(item.elements, budget, depth + 1)
        result.append(ElementEvidence(_identifier(item.msg_idx), author_evidence, body.finish(), children,
                                      len(children) < len(item.elements)))
    return tuple(result)


def _element_json(element: ElementEvidence) -> dict[str, Any]:
    author = element.author
    return {
        "message_index": element.message_index,
        "author": None if author is None else {
            "native_user_id": author.native_user_id, "display": author.display,
            "platform_role": author.platform_role, "bot": author.is_bot,
            "identity_status": "quoted_evidence_only",
        },
        "body": json.loads(body_to_json(element.body)),
        "children": [_element_json(child) for child in element.children],
        "truncated": element.truncated,
    }


def _reference_evidence(message, endpoint: EndpointKey, budget: _Budget) -> ReferenceEvidence:
    # M0 tolerates surrounding whitespace in index values; an application
    # lookup must not silently merge different opaque identifiers that way.
    values: dict[str, set[str]] = {"msg_idx": set(), "ref_msg_idx": set()}
    invalid = False

    def collect(key, value):
        nonlocal invalid
        if not isinstance(key, str) or key.strip() not in values:
            return
        native = _identifier(value)
        if native is None:
            invalid = True
        else:
            values[key.strip()].add(native)

    scene = message.raw.get("message_scene")
    ext = scene.get("ext") if isinstance(scene, dict) else None
    if isinstance(ext, dict):
        for key, value in ext.items():
            collect(key, value)
    elif isinstance(ext, list):
        for item in ext:
            if isinstance(item, str) and "=" in item:
                collect(*item.split("=", 1))
            elif isinstance(item, dict):
                collect(item.get("key"), item.get("value"))
    if message.message_type == 103:
        for item in message.elements:
            if item.msg_idx is not None:
                collect("ref_msg_idx", item.msg_idx)
    conflict = invalid or any(len(items) > 1 for items in values.values())
    if conflict:
        budget.warn("invalid_reference" if invalid else "conflicting_reference")
    return ReferenceEvidence(
        endpoint,
        next(iter(values["msg_idx"])) if not conflict and values["msg_idx"] else None,
        next(iter(values["ref_msg_idx"])) if not conflict and values["ref_msg_idx"] else None,
        conflict,
    )


def decode_message(dispatch: Dispatch, account: AccountKey, received_at: int) -> DecodedMessage | RejectedMessage:
    """Decode bounded transport evidence, never admission or reply authority.

    Reparse ``Dispatch.data`` to avoid trusting stale derived dataclass fields.
    All rejection reasons are fixed, safe diagnostics.  Bad configuration raises
    ValueError; malformed peer content returns a terminal RejectedMessage.
    """
    if not isinstance(account, AccountKey) or account.platform != "qq-official":
        raise ValueError("official codec requires an official account")
    if _identifier(account.app_id) is None or len(account.key.encode()) > MAX_ACCOUNT_KEY_BYTES:
        raise ValueError("official codec account key exceeds safe limits")
    if type(received_at) is not int or received_at < 0:
        raise ValueError("received_at must be a nonnegative integer")
    if not isinstance(dispatch, Dispatch):
        return RejectedMessage("invalid_dispatch")
    try:
        parsed = parse_dispatch({"op": 0, "t": dispatch.event_type, "s": dispatch.seq,
                                 "id": dispatch.event_id, "d": dispatch.data})
    except EventParseError:
        return RejectedMessage("invalid_dispatch_payload")
    if _contains_nul(parsed.data):
        return RejectedMessage("unsupported_nul_payload")
    message = parsed.message
    if message is None:
        return RejectedMessage("not_message_event", "ignored")
    endpoint_id, actor_id = _identifier(message.endpoint_id), _identifier(message.author.native_id)
    endpoint = EndpointKey(account, "group" if message.kind == "group" else "private", endpoint_id) if endpoint_id else None
    message_id, event_id = _identifier(message.message_id), _identifier(parsed.event_id)
    if parsed.event_id is not None and event_id is None:
        return RejectedMessage("invalid_event_id")
    if endpoint is not None and len(endpoint.key.encode()) > MAX_ENDPOINT_KEY_BYTES:
        return RejectedMessage("identity_key_limit")
    if message.author.is_bot:
        return RejectedMessage("bot_authored", "bot_origin", endpoint, message_id, event_id, actor_id)
    if endpoint is None or actor_id is None:
        return RejectedMessage("missing_or_invalid_identity")
    if message_id is None:
        return RejectedMessage("missing_or_invalid_message_id", endpoint=endpoint, event_id=event_id)
    actor = ActorKey(account, "group_member" if message.kind == "group" else "c2c_user",
                     endpoint_id if message.kind == "group" else "", actor_id)
    if len(actor.key.encode()) > MAX_ACTOR_KEY_BYTES:
        return RejectedMessage("identity_key_limit")
    budget = _Budget()
    mentions = _mentions(message)
    body = _Body(budget)
    _text_nodes(body, message.content, mentions)
    sticker = any(isinstance(node, EmoteNode) and node.raw_data.get("face_type") == 6 for node in body.nodes)
    _media_nodes(body, message.attachments, sticker=sticker)
    elements = _elements(message.elements, budget)
    unknown_fields = tuple(sorted(set(message.raw) - _KNOWN_FIELDS))[:16]
    if message.message_type == 102:
        body.add(ForwardNode, "", count=len(elements) if elements else None,
                 description="平台提供的转发内容" if elements else "转发结构缺失",
                 raw_data={"version": 1, "source": "qq-official.forward",
                           "elements": [_element_json(item) for item in elements],
                           "truncated": budget.truncated})
    elif message.message_type == 103:
        body.add(UnsupportedNode, "qq-official.quote", "[引用内容，待核验原消息]" if elements else "[引用消息，内容结构缺失]",
                 raw_data={"version": 1, "elements": [_element_json(item) for item in elements],
                           "truncated": budget.truncated})
    elif message.message_type == 3:
        ark = message.ark_data
        card = {key: _label(ark.get(key), 2048) for key in ("title", "source", "summary") if isinstance(ark.get(key), str)}
        body.add(CardNode, title=card.get("title", ""), url=_url(message.jump_url),
                 source_type="qq-official.ark", raw_data={**card, "ark_data": dict(ark)})
    elif message.message_type not in {None, 0}:
        body.add(UnsupportedNode, "qq-official.unknown", "[未识别的消息结构]",
                 raw_data={"message_type": message.message_type, "unknown_field_names": list(unknown_fields)})
    elif elements:
        body.add(UnsupportedNode, "qq-official.elements", "[附带的消息元素]",
                 raw_data={"version": 1, "elements": [_element_json(item) for item in elements]})
    platform_time = int(message.occurred_at) if message.occurred_at is not None and message.occurred_at >= 0 else None
    source = "received_missing" if platform_time is None else "received_future" if platform_time > received_at else "platform"
    occurred_at = platform_time if source == "platform" else received_at
    reference = _reference_evidence(message, endpoint, budget)
    return DecodedMessage(
        endpoint, actor, message_id, event_id, _label(message.author.display_name), message.author.platform_role,
        body.finish(), message.kind == "group" and message.mentions_bot, occurred_at, received_at, platform_time, source,
        reference,
        ConversionEvidence(mentions, elements, unknown_fields, tuple(sorted(budget.warnings)), budget.truncated,
                           message.message_type),
    )
