from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)

from .conversation_scope import ConversationScope
from .message_ir import (
    CardNode,
    EmoteNode,
    ForwardNode,
    MediaNode,
    MentionNode,
    MessageBody,
    TextNode,
    UnsupportedNode,
    fallback_text,
    render_fallback_text,
)
from .message_lowering import ONEBOT_V11_CAPABILITIES, lower_message

if TYPE_CHECKING:
    from .ledger import CanonicalMessage, MessageLedger


@dataclass(frozen=True)
class DecodedOneBotMessage:
    body: MessageBody
    reply_to_native_message_id: str | None = None


def decode_onebot_message(raw_message: Any) -> DecodedOneBotMessage:
    nodes = []
    reply_to: str | None = None

    for index, (segment_type, data) in enumerate(
        _message_segments(raw_message)
    ):
        if segment_type == "reply":
            if reply_to is None:
                candidate = str(data.get("id") or "").strip()
                reply_to = candidate or None
            continue
        if segment_type == "text":
            nodes.append(TextNode(index, str(data.get("text") or "")))
            continue
        if segment_type == "at":
            native_user_id = str(data.get("qq") or "").strip()
            display = str(
                data.get("name")
                or data.get("display")
                or data.get("text")
                or ""
            ).strip()
            nodes.append(
                MentionNode(index, native_user_id, display, raw_data=data)
            )
            continue
        if segment_type == "face":
            raw = data.get("raw")
            raw_name = raw.get("faceText") if isinstance(raw, dict) else ""
            nodes.append(
                EmoteNode(
                    index,
                    str(data.get("id") or ""),
                    str(data.get("faceText") or raw_name or ""),
                    data,
                )
            )
            continue
        if segment_type in {"image", "mface"}:
            summary = str(data.get("summary") or "")
            is_sticker = segment_type == "mface" or (
                str(data.get("subType") or data.get("sub_type") or "") == "1"
                or "表情" in summary
            )
            nodes.append(
                MediaNode(
                    index,
                    "sticker" if is_sticker else "image",
                    source=str(data.get("url") or data.get("file") or ""),
                    name=str(data.get("file") or ""),
                    description=summary,
                    source_type=segment_type,
                    raw_data=data,
                )
            )
            continue
        if segment_type == "record":
            nodes.append(
                MediaNode(
                    index,
                    "audio",
                    source=str(data.get("url") or data.get("file") or ""),
                    name=str(data.get("file") or ""),
                    source_type=segment_type,
                    raw_data=data,
                )
            )
            continue
        if segment_type == "video":
            nodes.append(
                MediaNode(
                    index,
                    "video",
                    source=str(data.get("url") or data.get("file") or ""),
                    name=str(data.get("file") or ""),
                    source_type=segment_type,
                    raw_data=data,
                )
            )
            continue
        if segment_type in {"file", "onlinefile"}:
            name = str(
                data.get("name")
                or data.get("fileName")
                or data.get("file")
                or ""
            )
            nodes.append(
                MediaNode(
                    index,
                    "file",
                    source=str(data.get("file_id") or data.get("file") or ""),
                    name=name,
                    description=name,
                    source_type=segment_type,
                    raw_data=data,
                )
            )
            continue
        if segment_type in {"json", "xml"}:
            title, url = extract_card_summary(data)
            nodes.append(
                CardNode(
                    index,
                    title=title,
                    url=url,
                    source_type=segment_type,
                    raw_data=data,
                )
            )
            continue
        if segment_type in {"forward", "node"}:
            raw_count = data.get("count")
            try:
                count = int(raw_count) if raw_count is not None else None
            except (TypeError, ValueError):
                count = None
            nodes.append(
                ForwardNode(
                    index,
                    native_id=str(data.get("id") or data.get("message_id") or ""),
                    count=max(count, 0) if count is not None else None,
                    description=str(data.get("summary") or ""),
                    raw_data=data,
                )
            )
            continue

        nodes.append(
            UnsupportedNode(
                index,
                segment_type or "unknown",
                f"[{segment_type or 'unknown'}]",
                data,
            )
        )

    return DecodedOneBotMessage(MessageBody(tuple(nodes)), reply_to)


def render_onebot_body(body: MessageBody) -> Message:
    lowered = lower_message(
        body,
        ONEBOT_V11_CAPABILITIES,
        destination_platform="onebot-v11",
        origin_platform="onebot-v11",
    )
    message = Message()
    for chunk in lowered.chunks:
        for node in chunk.nodes:
            _append_onebot_node(message, node)
    return message


def _append_onebot_node(message: Message, node) -> None:
    if isinstance(node, TextNode):
        message.append(MessageSegment.text(node.text))
    elif isinstance(node, MentionNode):
        data = dict(node.raw_data) or {"qq": node.native_user_id}
        data["qq"] = node.native_user_id
        message.append(MessageSegment("at", data))
    elif isinstance(node, EmoteNode):
        data = dict(node.raw_data) or {"id": node.native_id}
        data["id"] = node.native_id
        message.append(MessageSegment("face", data))
    elif isinstance(node, MediaNode):
        segment_type = node.source_type or {
            "image": "image",
            "sticker": "image",
            "video": "video",
            "audio": "record",
            "file": "file",
        }[node.media_kind]
        data = dict(node.raw_data)
        if not data and node.source:
            data["file"] = node.source
        message.append(MessageSegment(segment_type, data))
    elif isinstance(node, CardNode):
        message.append(
            MessageSegment(node.source_type or "json", dict(node.raw_data))
        )
    else:
        message.append(MessageSegment.text(fallback_text(node)))


def compose_onebot_reply(
    content: Message | MessageSegment | str,
    *,
    reply_native_message_id: str | int,
    mention_native_user_id: str | int | None = None,
) -> Message:
    if isinstance(content, Message):
        decoded = decode_onebot_message(content)
    elif isinstance(content, MessageSegment):
        decoded = decode_onebot_message(Message([content]))
    else:
        decoded = DecodedOneBotMessage(
            MessageBody((TextNode(0, str(content)),))
        )

    rendered = render_onebot_body(decoded.body)
    if decoded.body.has_media("audio"):
        return rendered

    message = Message(
        [MessageSegment.reply(int(reply_native_message_id))]
    )
    already_mentioned = any(
        segment.type == "at"
        and str(segment.data.get("qq") or "")
        == str(mention_native_user_id)
        for segment in rendered
    )
    if mention_native_user_id is not None and not already_mentioned:
        message.append(MessageSegment.at(int(mention_native_user_id)))
        message.append(MessageSegment.text(" "))
    message.extend(rendered)
    return message


def scope_from_event(event: MessageEvent) -> ConversationScope:
    if isinstance(event, GroupMessageEvent):
        return ConversationScope(
            platform="onebot-v11",
            kind="group",
            native_conversation_id=str(event.group_id),
            actor_native_user_id=str(event.user_id),
            bot_native_user_id=str(event.self_id),
        )
    if isinstance(event, PrivateMessageEvent):
        return ConversationScope(
            platform="onebot-v11",
            kind="private",
            native_conversation_id=str(event.user_id),
            actor_native_user_id=str(event.user_id),
            bot_native_user_id=str(event.self_id),
        )
    raise ValueError("unsupported OneBot message event")


def record_onebot_event(
    ledger: "MessageLedger",
    event: MessageEvent,
    *,
    scope: ConversationScope | None = None,
) -> "CanonicalMessage":
    decoded = decode_onebot_message(event.original_message)
    sender = getattr(event, "sender", None)
    sender_name = str(
        getattr(sender, "card", "")
        or getattr(sender, "nickname", "")
        or ("群成员" if isinstance(event, GroupMessageEvent) else "用户")
    )
    scope = scope or scope_from_event(event)
    return ledger.record_message(
        scope,
        native_message_id=str(event.message_id),
        sender_native_user_id=str(event.user_id),
        sender_display=sender_name,
        body=decoded.body,
        occurred_at=int(event.time),
        direction=("outbound" if event.user_id == event.self_id else "inbound"),
        message_kind=_message_kind(event.original_message.extract_plain_text()),
        reply_to_native_message_id=decoded.reply_to_native_message_id,
        raw_event={
            "message_type": getattr(event, "message_type", ""),
            "sub_type": getattr(event, "sub_type", ""),
        },
        identity_platform="onebot-v11",
    )


def record_onebot_api_message(
    ledger: "MessageLedger",
    scope: ConversationScope,
    raw_message: dict[str, Any],
    *,
    bot_native_user_id: str | int = "",
) -> "CanonicalMessage" | None:
    native_message_id = str(raw_message.get("message_id") or "").strip()
    if not native_message_id:
        return None
    sender = raw_message.get("sender")
    sender_data = sender if isinstance(sender, dict) else {}
    sender_native_user_id = str(
        raw_message.get("user_id")
        or sender_data.get("user_id")
        or ""
    )
    sender_name = str(
        sender_data.get("card")
        or sender_data.get("nickname")
        or ("群成员" if sender_native_user_id else "未知用户")
    )
    decoded = decode_onebot_message(
        raw_message.get("message") or raw_message.get("raw_message") or []
    )
    return ledger.record_message(
        scope,
        native_message_id=native_message_id,
        sender_native_user_id=sender_native_user_id,
        sender_display=sender_name,
        body=decoded.body,
        occurred_at=int(raw_message.get("time") or 0),
        direction=(
            "outbound"
            if sender_native_user_id
            and sender_native_user_id == str(bot_native_user_id)
            else "inbound"
        ),
        message_kind=_message_kind(render_fallback_text(decoded.body)),
        reply_to_native_message_id=decoded.reply_to_native_message_id,
        raw_event={"source": "onebot-api"},
    )


def record_onebot_outgoing(
    ledger: "MessageLedger",
    scope: ConversationScope,
    *,
    native_message_id: str | int,
    message: Message | MessageSegment | str,
    occurred_at: int,
) -> "CanonicalMessage":
    decoded = decode_onebot_message(message)
    return ledger.record_message(
        scope,
        native_message_id=str(native_message_id),
        sender_native_user_id=scope.bot_native_user_id,
        sender_display="机器人",
        body=decoded.body,
        occurred_at=occurred_at,
        direction="outbound",
        message_kind="chat",
        reply_to_native_message_id=decoded.reply_to_native_message_id,
        raw_event={"source": "onebot-outbound"},
    )


def _message_kind(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("/"):
        return "command"
    matched = re.match(
        r"^!([A-Za-z]+)(?:\s+(.*))?$",
        stripped,
        re.DOTALL,
    )
    if matched is None:
        return "chat"
    verb = matched.group(1).casefold()
    body = (matched.group(2) or "").strip()
    if verb in {"feedback", "fb", "btw"} and body:
        return "chat"
    return "command"


def render_api_attachments(
    body: MessageBody,
    canonical_message_id: int,
) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    for node in body.nodes:
        if not isinstance(node, MediaNode) or node.media_kind != "file":
            continue
        data = node.raw_data
        attachments.append(
            {
                "handle": f"file#{canonical_message_id}.{node.segment_index}",
                "file_name": str(
                    data.get("name")
                    or data.get("fileName")
                    or node.name
                    or ""
                ),
                "file_size": _safe_int(
                    data.get("file_size") or data.get("fileSize") or 0
                ),
                "type": node.source_type or "file",
            }
        )
    return attachments


def _message_segments(raw_message: Any) -> list[tuple[str, dict[str, Any]]]:
    if isinstance(raw_message, MessageSegment):
        return [(raw_message.type, dict(raw_message.data))]
    if isinstance(raw_message, Message):
        return [
            (segment.type, dict(segment.data)) for segment in raw_message
        ]
    if isinstance(raw_message, list):
        segments = []
        for item in raw_message:
            if not isinstance(item, dict):
                continue
            segment_type = item.get("type")
            data = item.get("data")
            if isinstance(segment_type, str):
                segments.append(
                    (segment_type, dict(data) if isinstance(data, dict) else {})
                )
        return segments
    if isinstance(raw_message, str):
        try:
            parsed = Message(raw_message)
        except Exception:
            return [("text", {"text": raw_message})]
        return [(segment.type, dict(segment.data)) for segment in parsed]
    return []


from .card_summary import extract_card_summary as extract_card_summary


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
