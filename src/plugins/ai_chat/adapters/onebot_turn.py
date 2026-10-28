"""Convert real OneBot input at the edge of the shared turn application."""

from __future__ import annotations

import re

from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent

from ..conversation_scope import ConversationScope
from ..message_ir import MentionNode
from ..message_lowering import ONEBOT_V11_CAPABILITIES
from ..onebot_availability import delivery_blocker
from ..onebot_codec import decode_onebot_message, render_onebot_body, scope_from_event
from ..platform_types import (
    AccountKey, ActorKey, EffectiveCapabilities, EndpointKey, InboundMessage,
    PlatformFeatures, SendOutcome, SendRequest,
)
from ..tasks import LegacyTaskIdentity


class OneBotTurnPlatform:
    """A real OneBot event and its connected bot, bound to one actual endpoint.

    Existing reply planning/outbox code remains the owner of OneBot delivery.
    This port exposes its native transport for callers with an already prepared
    send intent; it never retries a request whose result could be unknown.
    """

    capabilities = ONEBOT_V11_CAPABILITIES
    features = PlatformFeatures(
        group_files=True, reactions=True, forward_messages=True, member_list=True,
        native_ocr=True, inline_voice_text=True, native_files=True,
        proactive_group=True,
    )

    def __init__(
        self, bot: Bot, event: MessageEvent, *, canonical_scope: ConversationScope,
    ) -> None:
        physical_scope = scope_from_event(event)
        self.account = AccountKey("onebot-v11", str(event.self_id), "production")
        if isinstance(bot, Bot) and str(bot.self_id) != self.account.app_id:
            raise ValueError("OneBot event belongs to a different connected account")
        self.endpoint = EndpointKey(
            self.account, physical_scope.kind, physical_scope.native_conversation_id,
        )
        actor = ActorKey(self.account, "user", "", str(event.user_id))
        decoded = decode_onebot_message(event.original_message)
        sender = event.sender
        role = str(getattr(sender, "role", "") or "")
        self.inbound = InboundMessage(
            endpoint=self.endpoint,
            actor=actor,
            event_id=None,
            native_message_id=str(event.message_id),
            sender_display=str(
                getattr(sender, "card", "") or getattr(sender, "nickname", "")
                or ("群成员" if physical_scope.kind == "group" else "用户")
            ),
            platform_role=role if role in {"owner", "admin", "member"} else "",
            body=decoded.body,
            mentions_bot=any(
                isinstance(node, MentionNode) and node.native_user_id == self.account.app_id
                for node in decoded.body.nodes
            ),
            reply_to_native_message_id=decoded.reply_to_native_message_id,
            occurred_at=int(event.time),
            reply_credential=None,
            canonical_scope=canonical_scope,
        )
        self.legacy_task_identity = LegacyTaskIdentity(
            user_id=event.user_id,
            group_id=event.group_id if isinstance(event, GroupMessageEvent) else None,
            message_id=event.message_id,
        )
        self._bot = bot
        self._event = event
        self._sent_message_ids: set[str] = set()

    def _require_endpoint(self, endpoint: EndpointKey) -> None:
        if endpoint != self.endpoint:
            raise ValueError("OneBot turn cannot operate on another endpoint")

    async def ready(self, endpoint: EndpointKey) -> str | None:
        self._require_endpoint(endpoint)
        return await delivery_blocker(self._bot)

    async def effective_capabilities(self, endpoint: EndpointKey) -> EffectiveCapabilities:
        self._require_endpoint(endpoint)
        # Implementation support is not evidence of group administrative rights.
        return EffectiveCapabilities(endpoint, revision=0, grants={})

    async def send(self, request: SendRequest) -> SendOutcome:
        self._require_endpoint(request.endpoint)
        if request.actor != self.inbound.actor:
            raise ValueError("OneBot turn cannot change its requesting actor")
        if request.route_epoch != self.inbound.route_epoch:
            return SendOutcome("failed", reason="stale_route")
        if request.reply_credential is not None:
            return SendOutcome("failed", reason="unsupported_reply_credential")
        if (request.reply_to_native_message_id is not None
                and request.reply_to_native_message_id != self.inbound.native_message_id
                and request.reply_to_native_message_id not in self._sent_message_ids):
            # An endpoint argument alone is no proof that an arbitrary native
            # message belongs there. Other verified receipts are added in M3.
            return SendOutcome("failed", reason="unverified_reply_target")
        message = render_onebot_body(request.body)
        if request.reply_to_native_message_id is not None:
            # Native conversion is confined to this OneBot adapter.
            from nonebot.adapters.onebot.v11 import MessageSegment
            message.insert(0, MessageSegment.reply(int(request.reply_to_native_message_id)))
        try:
            response = await self._bot.send(self._event, message)
        except Exception:
            return SendOutcome("ambiguous", reason="onebot_send_outcome_unknown")
        message_id = response.get("message_id") if isinstance(response, dict) else None
        if type(message_id) is int:
            native_id = str(message_id)
        elif isinstance(message_id, str) and re.fullmatch(r"-?[0-9]+", message_id):
            try:
                native_id = str(int(message_id))
            except ValueError:
                native_id = ""
        else:
            native_id = ""
        if not native_id:
            return SendOutcome("ambiguous", reason="onebot_receipt_missing")
        self._sent_message_ids.add(native_id)
        return SendOutcome(
            "committed", native_message_id=native_id,
            receipt={"message_id": native_id},
        )

    async def recall(self, endpoint: EndpointKey, native_message_id: str) -> bool:
        self._require_endpoint(endpoint)
        if native_message_id not in self._sent_message_ids:
            return False
        # Consume before yielding: concurrent calls, cancellation and unknown
        # transport outcomes cannot replay this local recall permission.
        self._sent_message_ids.remove(native_message_id)
        await self._bot.delete_msg(message_id=int(native_message_id))
        return True
