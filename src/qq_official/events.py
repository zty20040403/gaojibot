"""Bounded QQ dispatch parsing, without application identity or deduplication.

Field contracts checked against tencent-connect/qqbot-nodejs at
429a0f1b798f3b0fcef3679e207dbc0469a1df2d. Raw fields are untrusted evidence;
URLs are never fetched here and text markers never grant mention privileges.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import math
from typing import Any, Mapping
from urllib.parse import urlsplit

MESSAGE_EVENTS = frozenset({"GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"})
LIFECYCLE_EVENTS = frozenset({"GROUP_ADD_ROBOT", "GROUP_DEL_ROBOT", "GROUP_MSG_RECEIVE", "GROUP_MSG_REJECT", "C2C_MSG_RECEIVE", "C2C_MSG_REJECT"})
MAX_EVENT_BYTES = 256 * 1024
MAX_DEPTH = 16
MAX_NODES = 4096
MAX_COLLECTION = 256


class EventParseError(ValueError):
    """Contains only a fixed reason, never the rejected event."""


def _bounded_copy(value: Any, *, max_bytes: int = MAX_EVENT_BYTES) -> Any:
    nodes = 0
    size = 0

    def visit(item: Any, depth: int) -> Any:
        nonlocal nodes, size
        nodes += 1
        if depth > MAX_DEPTH or nodes > MAX_NODES:
            raise EventParseError("event_structure_limit")
        if isinstance(item, str):
            try:
                size += len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
            except UnicodeError:
                raise EventParseError("event_invalid_unicode") from None
            result = item
        elif isinstance(item, dict):
            if len(item) > MAX_COLLECTION:
                raise EventParseError("event_collection_limit")
            if any(not isinstance(k, str) for k in item):
                raise EventParseError("event_invalid_key")
            size += 2 + max(0, len(item) - 1) + len(item)
            result = {visit(k, depth + 1): visit(v, depth + 1) for k, v in item.items()}
        elif isinstance(item, list):
            if len(item) > MAX_COLLECTION:
                raise EventParseError("event_collection_limit")
            size += 2 + max(0, len(item) - 1)
            result = [visit(v, depth + 1) for v in item]
        elif item is None or isinstance(item, (bool, int, float)):
            if isinstance(item, float) and not math.isfinite(item):
                raise EventParseError("event_invalid_number")
            if isinstance(item, int) and item.bit_length() > 64:
                raise EventParseError("event_number_limit")
            size += len(json.dumps(item, allow_nan=False))
            result = item
        else:
            raise EventParseError("event_invalid_value")
        if size > max_bytes:
            raise EventParseError("event_size_limit")
        return result

    return visit(value, 0)


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _items(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


@dataclass(frozen=True)
class Author:
    native_id: str | None = field(default=None, repr=False)
    display_name: str | None = field(default=None, repr=False)
    platform_role: str = ""
    is_bot: bool = False
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Attachment:
    url: str | None = field(default=None, repr=False)
    content_type: str | None = field(default=None, repr=False)
    filename: str | None = field(default=None, repr=False)
    size: int | None = None
    voice_wav_url: str | None = field(default=None, repr=False)
    asr_refer_text: str | None = field(default=None, repr=False)
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class MessageElement:
    msg_idx: str | None = field(default=None, repr=False)
    content: str = field(default="", repr=False)
    author: Author | None = field(default=None, repr=False)
    attachments: tuple[Attachment, ...] = field(default=(), repr=False)
    elements: tuple[MessageElement, ...] = field(default=(), repr=False)
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Message:
    kind: str
    endpoint_id: str | None = field(repr=False)
    message_id: str | None = field(repr=False)
    author: Author = field(repr=False)
    content: str = field(repr=False)
    timestamp: str | None = field(repr=False)
    occurred_at: float | None
    mentions_bot: bool
    mentions: tuple[Mapping[str, Any], ...] = field(repr=False)
    message_type: int | None
    msg_idx: str | None = field(repr=False)
    ref_msg_idx: str | None = field(repr=False)
    reference_conflict: bool
    attachments: tuple[Attachment, ...] = field(repr=False)
    elements: tuple[MessageElement, ...] = field(repr=False)
    ark_data: Mapping[str, Any] = field(repr=False)
    jump_url: str | None = field(repr=False)
    raw: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True)
class Dispatch:
    event_type: str = field(repr=False)
    seq: int | None
    event_id: str | None = field(repr=False)
    message: Message | None = field(default=None, repr=False)
    data: Mapping[str, Any] = field(default_factory=dict, repr=False)


def _author(value: Any, kind: str) -> Author:
    data = _object(value)
    role = data.get("member_role", "")
    return Author(
        native_id=_string(data.get("member_openid" if kind == "group" else "user_openid")),
        display_name=_string(data.get("nickname")) or _string(data.get("username")),
        platform_role=role if role in ("owner", "admin", "member") else "",
        is_bot=data.get("bot") is True,
        raw=data,
    )


def _attachments(value: Any) -> tuple[Attachment, ...]:
    return tuple(Attachment(
        url=_string(item.get("url")), content_type=_string(item.get("content_type")),
        filename=_string(item.get("filename")), size=_int(item.get("size")),
        voice_wav_url=_string(item.get("voice_wav_url")),
        asr_refer_text=_string(item.get("asr_refer_text")), raw=item,
    ) for item in _items(value) if isinstance(item, dict))


def _elements(value: Any, kind: str) -> tuple[MessageElement, ...]:
    return tuple(MessageElement(
        msg_idx=_string(item.get("msg_idx")), content=_string(item.get("content")) or "",
        author=_author(item["author"], kind) if isinstance(item.get("author"), dict) else None,
        attachments=_attachments(item.get("attachments")),
        elements=_elements(item.get("msg_elements"), kind), raw=item,
    ) for item in _items(value) if isinstance(item, dict))


def _indices(data: dict[str, Any]) -> tuple[str | None, str | None, bool]:
    ext = _object(data.get("message_scene")).get("ext")
    fields: dict[str, set[str]] = {"msg_idx": set(), "ref_msg_idx": set()}

    def collect(key: Any, value: Any) -> None:
        if isinstance(key, str) and key.strip() in fields and isinstance(value, str) and value.strip():
            fields[key.strip()].add(value.strip())

    if isinstance(ext, dict):
        for key, value in ext.items():
            collect(key, value)
    else:
        for item in _items(ext):
            if isinstance(item, str) and "=" in item:
                collect(*item.split("=", 1))
            elif isinstance(item, dict):
                collect(item.get("key"), item.get("value"))
    if data.get("message_type") == 103:
        for item in _items(data.get("msg_elements")):
            if isinstance(item, dict):
                collect("ref_msg_idx", item.get("msg_idx"))
    conflict = any(len(values) > 1 for values in fields.values())
    msg_values, ref_values = fields["msg_idx"], fields["ref_msg_idx"]
    return (next(iter(msg_values)) if len(msg_values) == 1 else None,
            next(iter(ref_values)) if len(ref_values) == 1 else None, conflict)


def _timestamp(value: Any) -> tuple[str | None, float | None]:
    text = _string(value)
    if text is None:
        return None, None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return text, None
        return text, parsed.timestamp()
    except (ValueError, OverflowError):
        return text, None


def parse_dispatch(envelope: Mapping[str, Any] | str | bytes) -> Dispatch:
    """Parse a dispatch without trusting textual @ tags or guessing identity.

    Invalid/oversized input raises a redacted EventParseError for quarantine.
    Unknown dispatch types retain bounded data, and absent IDs remain None.
    """
    if isinstance(envelope, (str, bytes)):
        try:
            encoded_size = len(envelope if isinstance(envelope, bytes) else envelope.encode())
        except UnicodeError:
            raise EventParseError("event_invalid_unicode") from None
        if encoded_size > MAX_EVENT_BYTES:
            raise EventParseError("event_size_limit")
        try:
            envelope = json.loads(envelope)
        except (ValueError, RecursionError):
            raise EventParseError("event_invalid_json") from None
    data = _bounded_copy(envelope)
    if not isinstance(data, dict) or data.get("op") != 0 or not isinstance(data.get("t"), str):
        raise EventParseError("event_invalid_dispatch")
    if not isinstance(data.get("d"), dict):
        raise EventParseError("event_invalid_body")
    body, event_type = data["d"], data["t"]
    message = None
    if event_type in MESSAGE_EVENTS:
        kind = "c2c" if event_type == "C2C_MESSAGE_CREATE" else "group"
        author = _author(body.get("author"), kind)
        mentions = tuple(item for item in _items(body.get("mentions")) if isinstance(item, dict))
        msg_idx, ref_idx, conflict = _indices(body)
        timestamp, occurred_at = _timestamp(body.get("timestamp"))
        ark = _object(body.get("ark_data"))
        jump_url = _string(ark.get("jump_url"))
        if jump_url:
            try:
                parsed = urlsplit(jump_url)
                if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
                    jump_url = None
            except ValueError:
                jump_url = None
        message = Message(
            kind=kind, endpoint_id=_string(body.get("group_openid")) if kind == "group" else author.native_id,
            message_id=_string(body.get("id")), author=author, content=_string(body.get("content")) or "",
            timestamp=timestamp, occurred_at=occurred_at,
            mentions_bot=event_type == "GROUP_AT_MESSAGE_CREATE" or any(item.get("is_you") is True for item in mentions),
            mentions=mentions, message_type=_int(body.get("message_type")), msg_idx=msg_idx,
            ref_msg_idx=ref_idx, reference_conflict=conflict,
            attachments=_attachments(body.get("attachments")), elements=_elements(body.get("msg_elements"), kind),
            ark_data=ark, jump_url=jump_url, raw=body,
        )
    return Dispatch(event_type=event_type, seq=_int(data.get("s")), event_id=_string(data.get("id")), message=message, data=body)
