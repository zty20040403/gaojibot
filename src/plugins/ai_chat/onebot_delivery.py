"""Onebot Delivery responsibilities extracted from the plugin entrypoint."""

from __future__ import annotations

import asyncio
import time
import httpx
from nonebot import get_bots
from nonebot.adapters.onebot.v11 import (
    Bot,
    Message,
    MessageSegment,
)
from nonebot.adapters.onebot.v11.exception import (
    ActionFailed,
)
from .bridges import (
    BridgeError,
    BridgeOutcomeUnknown,
    BridgePermanentError,
    BridgeRetryableError,
)
from .conversation_scope import (
    ConversationScope,
)
from .delivery import (
    Delivery,
)
from .observability import (
    telemetry,
)
from .onebot_codec import (
    record_onebot_outgoing,
    render_onebot_body,
)
from .onebot_availability import delivery_blocker
from .reminders import (
    Reminder,
)
from .handler_services import HandlerService
from .group_wiki import NEWCOMER_DELIVERY_PREFIX, WikiSuperseded
from .mcp_bridge.continuations import DELIVERY_PREFIX as MCP_DELIVERY_PREFIX
from .mcp_bridge.store import McpDenied
from .project_file_outbox import FILE_PREFIX
from .project_file_transport import file_identity, deliver_file
from .agent.control import RouteAuthorityLost


class OneBotDelivery(HandlerService):
    async def _mcp_delivery_identity(self, delivery: Delivery):
        runtime = getattr(self.context, "mcp_runtime", None)
        if runtime is None or not hasattr(runtime.store, "delivery_identity"):
            raise BridgeRetryableError("MCP 已停用，暂停续跑结果投递")
        try:
            return await self.context.db_call(runtime.store.delivery_identity, delivery)
        except McpDenied as exc:
            raise BridgePermanentError("MCP 续跑投递范围不匹配") from exc

    async def _deliver_reminder(self, bot: Bot, reminder: Reminder) -> None:
        text = f"提醒：{reminder.message}"
        if reminder.conversation_kind == "group":
            group_id = int(reminder.native_conversation_id)
            if not self.services.group_enabled(group_id):
                self.context.logger.info(
                    f"Dropping {reminder.handle}: target QQ group is disabled."
                )
                return
            segments = Message()
            try:
                creator_id = int(reminder.creator_native_user_id)
            except (TypeError, ValueError):
                creator_id = 0
            if creator_id > 0:
                segments.append(MessageSegment.at(creator_id))
                segments.append(MessageSegment.text(" "))
            segments.append(MessageSegment.text(text))
            response = await bot.send_group_msg(
                group_id=group_id,
                message=segments,
            )
        else:
            segments = Message(text)
            response = await bot.send_private_msg(
                user_id=int(reminder.native_conversation_id),
                message=segments,
            )

        native_message_id = self.services.replies._sent_message_id(response)
        if native_message_id is not None and self.context.message_ledger is not None:
            scope = ConversationScope(
                reminder.platform,
                reminder.conversation_kind,  # type: ignore[arg-type]
                reminder.native_conversation_id,
                actor_native_user_id=reminder.creator_native_user_id,
                bot_native_user_id=str(bot.self_id),
            )
            record_onebot_outgoing(
                self.context.message_ledger,
                scope,
                native_message_id=native_message_id,
                message=segments,
                occurred_at=int(time.time()),
            )

    async def _reminder_loop(self) -> None:
        while True:
            await asyncio.sleep(self.context.settings.reminder_check_seconds)
            if self.context.reminder_store is None:
                continue
            bots = [bot for bot in get_bots().values() if isinstance(bot, Bot)]
            if not bots:
                continue
            bot = bots[0]
            for reminder in self.context.reminder_store.claim_due(limit=10):
                try:
                    migration = getattr(self.context, "migration_service", None)
                    if migration is None:
                        await self._deliver_reminder(bot, reminder)
                    else:
                        try:
                            account = await self.context.db_call(migration.outgoing_account, "reminder", reminder.reminder_id)
                            selected = next((item for item in bots if str(item.self_id) == account.app_id), None)
                            if selected is None:
                                self.context.reminder_store.mark_failed(reminder.reminder_id, "原机器人尚未连接", retry_seconds=30)
                                continue
                        except Exception:
                            raise RouteAuthorityLost("reminder_source_unavailable") from None
                        async with migration.outgoing_context("reminder", reminder.reminder_id, selected):
                            await self._deliver_reminder(selected, reminder)
                except RouteAuthorityLost:
                    self.context.reminder_store.park_route_attention(reminder)
                    self.context.logger.warning("Managed reminder stopped for source/receipt review.")
                except ActionFailed as exc:
                    if self.services.replies._is_napcat_send_timeout(exc):
                        self.context.logger.warning(
                            f"Reminder {reminder.handle} send outcome is unknown; "
                            "marking it delivered to avoid a duplicate."
                        )
                        self.context.reminder_store.mark_sent(reminder.reminder_id)
                    else:
                        self.context.reminder_store.mark_failed(reminder.reminder_id, str(exc))
                        self.context.logger.warning(f"Reminder {reminder.handle} send failed: {exc}")
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    self.context.reminder_store.mark_failed(reminder.reminder_id, str(exc))
                    self.context.logger.warning(f"Reminder {reminder.handle} delivery failed: {exc}")
                else:
                    self.context.reminder_store.mark_sent(reminder.reminder_id)

    async def _deliver_onebot_outbox(self, bot: Bot, delivery: Delivery) -> None:
        if delivery.idempotency_key.startswith(FILE_PREFIX):
            await deliver_file(self.context, bot, delivery)
            return
        is_project_report = delivery.idempotency_key.startswith("subagent-final:") and ":project:" in delivery.idempotency_key
        if is_project_report:
            from .project_task_reporting import validate_report_delivery

            await validate_report_delivery(self.context, bot, delivery)
        is_wiki_brief = delivery.idempotency_key.startswith(NEWCOMER_DELIVERY_PREFIX)
        is_mcp_reply = delivery.idempotency_key.startswith(MCP_DELIVERY_PREFIX)
        if is_mcp_reply:
            identity = await self._mcp_delivery_identity(delivery)
            if str(bot.self_id) != identity["bot_id"]:
                raise BridgePermanentError("MCP 续跑必须使用原机器人账号投递")
            if not await self.context.mcp_runtime.scope_allowed(**identity):
                raise BridgeRetryableError("无法核实 MCP 原群成员身份，暂不发送结果")
        if is_wiki_brief:
            wiki = getattr(self.services, "wiki", None)
            if wiki is None:
                raise BridgePermanentError("群百科已停用，取消新人简报")
            await wiki.validate_newcomer_delivery(bot, delivery)
        if delivery.target_kind == "group" and not self.services.group_enabled(
            int(delivery.target_native_conversation_id)
        ):
            raise BridgePermanentError("target QQ group is disabled")
        message = render_onebot_body(delivery.body)
        if delivery.reply_to_native_message_id:
            try:
                reply_id = int(delivery.reply_to_native_message_id)
            except (TypeError, ValueError):
                reply_id = 0
            if reply_id > 0:
                message = Message([MessageSegment.reply(reply_id), *message])
        if delivery.target_kind == "group":
            with telemetry.delivery(delivery.target_platform):
                response = await bot.send_group_msg(
                    group_id=int(delivery.target_native_conversation_id),
                    message=message,
                )
        else:
            with telemetry.delivery(delivery.target_platform):
                response = await bot.send_private_msg(
                    user_id=int(delivery.target_native_conversation_id),
                    message=message,
                )
        native_message_id = self.services.replies._sent_message_id(response)
        if (is_wiki_brief or is_mcp_reply or is_project_report) and not native_message_id:
            raise BridgeOutcomeUnknown("消息没有取得 QQ 回执，暂停自动重发")
        if self.context.delivery_store is not None:
            self.context.delivery_store.mark_committed(
                delivery.delivery_id,
                native_message_id=native_message_id or "",
            )
        # Group knowledge stays group-scoped, even when delivered privately to a newcomer.
        if is_wiki_brief:
            return
        if native_message_id is not None and self.context.mirror_state is not None:
            self.context.mirror_state.confirm_delivery(
                delivery.delivery_id,
                str(native_message_id),
            )
        is_mirror_delivery = bool(
            self.context.mirror_state is not None
            and self.context.mirror_state.is_mirror_delivery(delivery.delivery_id)
        )
        if is_mirror_delivery:
            return
        if native_message_id is not None and self.context.message_ledger is not None:
            scope = ConversationScope(
                delivery.target_platform,
                delivery.target_kind,  # type: ignore[arg-type]
                delivery.target_native_conversation_id,
                bot_native_user_id=str(bot.self_id),
            )
            stored = record_onebot_outgoing(
                self.context.message_ledger,
                scope,
                native_message_id=native_message_id,
                message=message,
                occurred_at=int(time.time()),
            )
            if (
                delivery.turn_id is not None
                and self.context.turn_journal is not None
            ):
                self.context.turn_journal.link_send(
                    delivery.turn_id,
                    stored.canonical_message_id,
                    node_id=f"outbox:{delivery.delivery_id}",
                )
            if self.context.bridge_manager is not None:
                self.context.bridge_manager.mirror_local_outgoing(
                    source_scope=scope,
                    source_native_event_id=str(native_message_id),
                    canonical_message_id=stored.canonical_message_id,
                    body=delivery.body,
                    occurred_at=int(time.time()),
                    reply_to_native_message_id=delivery.reply_to_native_message_id,
                )

    async def _delivery_loop(self) -> None:
        while True:
            await asyncio.sleep(self.context.settings.outbox_check_seconds)
            if self.context.delivery_store is None:
                continue
            expired = self.context.delivery_store.park_expired_attempts()
            if expired:
                self.context.logger.warning(
                    f"Parked {expired} expired delivery lease(s) as ambiguous."
                )
            bots = [bot for bot in get_bots().values() if isinstance(bot, Bot)]
            account_blockers: dict[str, str | None] = {}
            for delivery in self.context.delivery_store.claim_due(limit=20, exclude_platforms=() if bots else ("onebot-v11",)):
                try:
                    if delivery.idempotency_key.startswith(("mirror:", "mirror-out:")):
                        if self.context.bridge_manager is None:
                            raise BridgePermanentError("消息互通已关闭，此条旧投递已停止")
                        self.context.bridge_manager.validate_delivery(delivery)
                    if delivery.target_platform == "onebot-v11":
                        selected_bot = bots[0] if bots else None
                        migration = getattr(self.context, "migration_service", None)
                        if migration is not None:
                            try:
                                account = await self.context.db_call(migration.outgoing_account, "delivery", delivery.delivery_id)
                            except Exception:
                                raise RouteAuthorityLost("delivery_source_unavailable") from None
                            selected_bot = next((bot for bot in bots if str(bot.self_id) == account.app_id), None)
                        if delivery.idempotency_key.startswith(FILE_PREFIX):
                            try:
                                identity = await file_identity(self.context, delivery)
                            except BridgeRetryableError as exc:
                                await self.context.db_call(self.context.delivery_store.defer_unsent, delivery, str(exc))
                                continue
                            selected_bot = next((bot for bot in bots if str(bot.self_id) == identity["bot_native_user_id"]), None)
                        elif delivery.idempotency_key.startswith("subagent-final:"):
                            task_id = int(delivery.idempotency_key.split(":")[1])
                            dispatch = self.context.subagent_store.control(task_id)["dispatch"]
                            selected_bot = next((bot for bot in bots if str(bot.self_id) == str(dispatch["bot_id"])), None)
                        elif delivery.idempotency_key.startswith("routine-final:"):
                            store = getattr(self.context, "routine_store", None)
                            if store is None:
                                self.context.delivery_store.defer_unsent(delivery, "例行任务已停用，暂停投递")
                                continue
                            run = await self.context.db_call(store.get_run, delivery.idempotency_key.split(":", 1)[1])
                            if run is None or run.spec.scope_key != delivery.target_scope.key:
                                raise BridgePermanentError("例行任务投递范围不匹配")
                            selected_bot = next((bot for bot in bots if str(bot.self_id) == run.spec.bot_id), None)
                        elif delivery.idempotency_key.startswith(MCP_DELIVERY_PREFIX):
                            try:
                                identity = await self._mcp_delivery_identity(delivery)
                            except BridgeRetryableError as exc:
                                self.context.delivery_store.defer_unsent(delivery, str(exc))
                                continue
                            selected_bot = next((bot for bot in bots if str(bot.self_id) == identity["bot_id"]), None)
                        elif delivery.idempotency_key.startswith(NEWCOMER_DELIVERY_PREFIX):
                            wiki_store = getattr(self.context, "group_wiki_store", None)
                            if wiki_store is None:
                                raise BridgePermanentError("群百科已停用，取消新人简报")
                            try:
                                payload = await self.context.db_call(wiki_store.newcomer_delivery, delivery)
                            except WikiSuperseded as exc:
                                raise BridgePermanentError(str(exc)) from exc
                            selected_bot = next((bot for bot in bots if str(bot.self_id) == payload["bot_id"]), None)
                        if selected_bot is None:
                            self.context.delivery_store.defer_unsent(delivery, "原任务的 QQ 账号尚未连接")
                            continue
                        # A connected adapter can belong to a logged-out QQ account.
                        bot_id = str(selected_bot.self_id)
                        if bot_id not in account_blockers:
                            account_blockers[bot_id] = await delivery_blocker(selected_bot)
                        blocker = account_blockers[bot_id]
                        if blocker is not None:
                            self.context.delivery_store.defer_unsent(delivery, blocker)
                            continue
                        if migration is None:
                            await self._deliver_onebot_outbox(selected_bot, delivery)
                        else:
                            async with migration.outgoing_context("delivery", delivery.delivery_id, selected_bot):
                                await self._deliver_onebot_outbox(selected_bot, delivery)
                    elif self.context.bridge_manager is not None:
                        native_id = await self.context.bridge_manager.deliver(delivery)
                        self.context.delivery_store.mark_committed(
                            delivery.delivery_id,
                            native_message_id=native_id,
                        )
                    else:
                        raise BridgeRetryableError(
                            f"没有注册 {delivery.target_platform} 投递器"
                        )
                except RouteAuthorityLost:
                    self.context.delivery_store.mark_ambiguous(delivery.delivery_id, "source_route_needs_attention")
                    self.context.logger.warning("Managed delivery stopped for source/receipt review.")
                except ActionFailed as exc:
                    if self.services.replies._is_napcat_send_timeout(exc):
                        self.context.delivery_store.mark_ambiguous(
                            delivery.delivery_id,
                            str(exc),
                        )
                        self.context.logger.warning(
                            f"{delivery.handle} timed out; waiting for echo "
                            "instead of retrying blindly."
                        )
                    else:
                        self.context.delivery_store.mark_failed(
                            delivery.delivery_id,
                            str(exc),
                            retryable=False,
                        )
                        self.context.logger.warning(f"{delivery.handle} was rejected: {exc}")
                except BridgeOutcomeUnknown as exc:
                    self.context.delivery_store.mark_ambiguous(delivery.delivery_id, str(exc))
                    self.context.logger.warning(
                        f"{delivery.handle} outcome is unknown; waiting for echo."
                    )
                except BridgePermanentError as exc:
                    self.context.delivery_store.mark_failed(
                        delivery.delivery_id,
                        str(exc),
                        retryable=False,
                    )
                    self.context.logger.warning(f"{delivery.handle} was rejected: {exc}")
                except BridgeRetryableError as exc:
                    self.context.delivery_store.mark_failed(
                        delivery.delivery_id,
                        str(exc),
                        retryable=True,
                        retry_seconds=min(30 * max(delivery.attempts, 1), 300),
                    )
                    self.context.logger.warning(f"{delivery.handle} will retry: {exc}")
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    self.context.delivery_store.mark_failed(
                        delivery.delivery_id,
                        str(exc),
                        retryable=True,
                        retry_seconds=min(30 * max(delivery.attempts, 1), 300),
                    )
                    self.context.logger.warning(f"{delivery.handle} delivery failed softly: {exc}")

    async def _matrix_sync_loop(self) -> None:
        while True:
            if self.context.bridge_manager is None or self.context.bridge_manager.matrix is None:
                return
            try:
                processed = await self.context.bridge_manager.sync_matrix_once()
                if processed:
                    self.context.logger.info(f"Matrix bridge ingested {processed} new event(s).")
            except asyncio.CancelledError:
                raise
            except (BridgeError, httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
                self.context.logger.warning(f"Matrix sync failed without advancing its cursor: {exc}")
                await asyncio.sleep(self.context.settings.matrix_sync_retry_seconds)

    async def _imessage_sync_loop(self) -> None:
        while True:
            manager = self.context.bridge_manager
            if manager is None or manager.imessage is None or not manager.imessage_poll_enabled:
                return
            try:
                processed = await manager.sync_imessage_once()
                if processed:
                    self.context.logger.info(f"iMessage bridge ingested {processed} new event(s).")
            except asyncio.CancelledError:
                raise
            except BridgeError as exc:
                self.context.logger.warning(str(exc))
            await asyncio.sleep(self.context.settings.imessage_poll_seconds)
