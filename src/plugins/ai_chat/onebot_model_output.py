from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.bot_storage import DatabaseCalls
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.adapters.onebot.v11.exception import ActionFailed
from nonebot.exception import NetworkError

from .conversation_scope import ConversationScope
from .ledger import MessageLedger
from .message_ir import MediaNode
from .model_output import (
    ModelFace,
    ModelMediaReference,
    ModelMention,
    ModelText,
    may_contain_model_mention,
    parse_model_output,
)
from .onebot_codec import scope_from_event
from .stickers import qq_face_message


@dataclass(frozen=True)
class OneBotGroupMember:
    native_user_id: str
    display: str
    role: str
    title: str


def decode_group_members(raw_members: Any) -> tuple[OneBotGroupMember, ...]:
    if not isinstance(raw_members, list):
        return ()
    members: list[OneBotGroupMember] = []
    for raw in raw_members:
        if not isinstance(raw, dict):
            continue
        native_user_id = str(raw.get("user_id") or "").strip()
        if not native_user_id.isdigit() or int(native_user_id) <= 0:
            continue
        display = str(
            raw.get("card") or raw.get("nickname") or "群成员"
        ).strip()
        members.append(
            OneBotGroupMember(
                native_user_id=native_user_id,
                display=display or "群成员",
                role=str(raw.get("role") or "member"),
                title=str(raw.get("title") or "").strip(),
            )
        )
    return tuple(members)


class OneBotModelOutputResolver:
    def __init__(
        self,
        bot: Bot,
        event: MessageEvent,
        ledger: MessageLedger | None,
        *,
        scope: ConversationScope | None = None,
        db_call: DatabaseCalls | None = None,
    ) -> None:
        self.bot = bot
        self.event = event
        self.ledger = ledger
        self.db_call = db_call if db_call is not None else DatabaseCalls()
        self.scope = scope or (
            scope_from_event(event) if isinstance(event, MessageEvent) else None
        )
        self._members: dict[str, OneBotGroupMember] | None = None
        self._member_principals: dict[str, int] = {}
        self._visible_principal_displays: dict[int, str] = {}
        self._sent_media: set[tuple[str, int, int | None]] = set()

    async def render(self, text: str) -> Message:
        roster: tuple[tuple[str, int], ...] = ()
        self_principal_id: int | None = None
        if may_contain_model_mention(text):
            if self.ledger is not None and self.scope is not None:
                try:
                    roster = await self.db_call(
                        self.ledger.principal_roster, self.scope, 100
                    )
                except (OSError, RuntimeError, TypeError, ValueError):
                    roster = ()
            if self._is_group():
                await self._load_members()
                roster = roster + tuple(
                    (member.display, self._member_principals[native_id])
                    for native_id, member in (self._members or {}).items()
                    if native_id in self._member_principals
                )
            self._visible_principal_displays.update(
                (principal_id, display) for display, principal_id in roster
            )
            bot_native_user_id = str(
                getattr(self.event, "self_id", "")
                or (
                    self.scope.bot_native_user_id
                    if self.scope is not None
                    else ""
                )
            ).strip()
            if self.ledger is not None and bot_native_user_id:
                try:
                    self_principal_id = await self.db_call(
                        self.ledger.principal_id_for_native,
                        "onebot-v11",
                        bot_native_user_id,
                    )
                except (OSError, RuntimeError, TypeError, ValueError):
                    self_principal_id = None

        message = Message()
        for node in parse_model_output(
            text,
            roster=roster,
            self_principal_id=self_principal_id,
        ):
            if isinstance(node, ModelText):
                message.append(MessageSegment.text(node.text))
            elif isinstance(node, ModelMention):
                self._append_mention(message, node)
            elif isinstance(node, ModelFace):
                self._append_face(message, node)
            elif isinstance(node, ModelMediaReference):
                await self.db_call(self._append_media, message, node)
        return message

    async def _load_members(self) -> None:
        if self._members is not None:
            return
        if not self._is_group():
            self._members = {}
            return
        raw_group_id = getattr(self.event, "group_id", None) or (
            self.scope.native_conversation_id if self.scope is not None else ""
        )
        try:
            raw_members = await self.bot.get_group_member_list(
                group_id=int(raw_group_id),
            )
        except (
            ActionFailed,
            AttributeError,
            NetworkError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            self._members = {}
            return
        members = decode_group_members(raw_members)
        self._members = {
            member.native_user_id: member
            for member in members
        }
        if self.ledger is not None:
            try:
                self._member_principals = await self.db_call(
                    self.ledger.ensure_principal_identities,
                    "onebot-v11",
                    [(member.native_user_id, member.display) for member in members],
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                self._member_principals = {}

    def _append_mention(self, message: Message, node: ModelMention) -> None:
        native_user_id = next(
            (
                candidate
                for candidate, principal_id in self._member_principals.items()
                if principal_id == node.principal_id
            ),
            "",
        )
        member = (self._members or {}).get(native_user_id)
        bot_native_user_id = str(
            getattr(self.event, "self_id", "")
            or (
                self.scope.bot_native_user_id
                if self.scope is not None
                else ""
            )
        )
        if member is not None and native_user_id != bot_native_user_id:
            message.append(MessageSegment.at(int(native_user_id)))
            return
        display = self._visible_principal_displays.get(
            node.principal_id,
            "群成员",
        )
        safe_display = display.strip()
        if not safe_display or safe_display.isdigit():
            safe_display = "群成员"
        message.append(MessageSegment.text(f"@{safe_display}"))

    @staticmethod
    def _append_face(message: Message, node: ModelFace) -> None:
        face = qq_face_message(str(node.native_id))
        if isinstance(face, MessageSegment):
            message.append(face)
            return
        label = node.display or str(node.native_id)
        message.append(MessageSegment.text(f"[QQ表情:{label}]"))

    def _append_media(
        self,
        message: Message,
        node: ModelMediaReference,
    ) -> None:
        key = (
            node.media_kind,
            node.canonical_message_id,
            node.segment_index,
        )
        if key in self._sent_media:
            return
        target = (
            self.ledger.get_in_scope(
                self.scope,
                node.canonical_message_id,
            )
            if self.ledger is not None and self.scope is not None
            else None
        )
        media_nodes = (
            [
                item
                for item in target.body.nodes
                if isinstance(item, MediaNode)
                and item.media_kind == node.media_kind
                and (
                    node.segment_index is None
                    or item.segment_index == node.segment_index
                )
            ]
            if target is not None
            else []
        )
        if not media_nodes:
            label = "表情包" if node.media_kind == "sticker" else "图片"
            detail = f":{node.display}" if node.display else "不可用"
            message.append(MessageSegment.text(f"[{label}{detail}]"))
            return

        rendered = Message()
        for media in media_nodes:
            source = str(
                media.source
                or media.raw_data.get("url")
                or media.raw_data.get("file")
                or ""
            ).strip()
            if source:
                rendered.append(MessageSegment.image(source))
        if not rendered:
            label = "表情包" if node.media_kind == "sticker" else "图片"
            message.append(MessageSegment.text(f"[{label}不可用]"))
            return
        message.extend(rendered)
        self._sent_media.add(key)

    def _is_group(self) -> bool:
        return isinstance(self.event, GroupMessageEvent) or (
            self.scope is not None and self.scope.kind == "group"
        )
