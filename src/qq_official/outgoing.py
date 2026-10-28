"""Minimal safe wire payloads; window allocation belongs to the adapter."""
from __future__ import annotations

import re
from typing import Any

_PLATFORM_TAG = re.compile(r"<(?:/?qqbot(?:-|\b)|@|/?faceType\s*=|/?emoji(?::|\b))", re.IGNORECASE)


def escape_platform_tags(text: str) -> str:
    """Literal/model text may not manufacture native mentions or QQ elements."""
    return _PLATFORM_TAG.sub(lambda match: "&lt;" + match.group(0)[1:], text)


def _reply_fields(
    *, msg_id: str | None, event_id: str | None, msg_seq: int | None, reference: str | None,
) -> dict[str, Any]:
    if msg_id and event_id:
        raise ValueError("Choose a message credential or an event credential")
    if (msg_id or event_id) and (isinstance(msg_seq, bool) or not isinstance(msg_seq, int) or not 0 <= msg_seq <= 65535):
        raise ValueError("Passive reply requires a valid allocated msg_seq")
    if msg_seq is not None and (isinstance(msg_seq, bool) or not isinstance(msg_seq, int) or not 0 <= msg_seq <= 65535):
        raise ValueError("msg_seq is outside the protocol range")
    fields: dict[str, Any] = {}
    for key, value in (("msg_id", msg_id), ("event_id", event_id)):
        if value is not None:
            if not isinstance(value, str) or not value:
                raise ValueError("Reply credential must not be empty")
            fields[key] = value
    if msg_seq is not None:
        fields["msg_seq"] = msg_seq
    if reference is not None:
        if not isinstance(reference, str) or not reference:
            raise ValueError("Reference index must not be empty")
        fields["message_reference"] = {"message_id": reference}
    return fields


def _payload(
    content: str, *, markdown: bool, msg_id: str | None, event_id: str | None,
    msg_seq: int | None, reference: str | None, max_chars: int, max_bytes: int | None,
) -> dict[str, Any]:
    if not isinstance(content, str) or not content:
        raise ValueError("Message text must not be empty")
    content = escape_platform_tags(content)
    try:
        byte_count = len(content.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("Message text contains invalid Unicode") from None
    if len(content) > max_chars or (max_bytes is not None and byte_count > max_bytes):
        raise ValueError("Message text exceeds configured wire limit")
    fields = _reply_fields(msg_id=msg_id, event_id=event_id, msg_seq=msg_seq, reference=reference)
    if markdown:
        return {"msg_type": 2, "markdown": {"content": content}, **fields}
    return {"msg_type": 0, "content": content, **fields}


def text_payload(
    content: str, *, msg_id: str | None = None, event_id: str | None = None,
    msg_seq: int | None = None, reference: str | None = None,
    max_chars: int = 2000, max_bytes: int | None = None,
) -> dict[str, Any]:
    return _payload(content, markdown=False, msg_id=msg_id, event_id=event_id, msg_seq=msg_seq,
                    reference=reference, max_chars=max_chars, max_bytes=max_bytes)


def markdown_payload(
    content: str, *, msg_id: str | None = None, event_id: str | None = None,
    msg_seq: int | None = None, reference: str | None = None,
    max_chars: int = 2000, max_bytes: int | None = None,
) -> dict[str, Any]:
    # Leave formulas/code untouched. Format-specific conversion requires verified
    # platform rendering behavior and belongs to M3, not the protocol builder.
    return _payload(content, markdown=True, msg_id=msg_id, event_id=event_id, msg_seq=msg_seq,
                    reference=reference, max_chars=max_chars, max_bytes=max_bytes)
