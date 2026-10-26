"""Reply Service responsibilities extracted from the plugin entrypoint."""

from __future__ import annotations

import asyncio
import re
import time
from typing import (
    Any,
)
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.adapters.onebot.v11.exception import (
    ActionFailed,
)
from nonebot.exception import (
    FinishedException,
)
from .deepseek import (
    FinalStreamState,
)
from .delivery import (
    Delivery,
)
from .message_ir import (
    MessageBody,
    TextNode,
)
from .observability import (
    telemetry,
)
from .onebot_codec import (
    compose_onebot_reply,
    decode_onebot_message,
    record_onebot_outgoing,
    render_onebot_body,
    scope_from_event,
)
from .onebot_model_output import (
    OneBotModelOutputResolver,
)
from .output_planner import (
    FAILURE_FACE_ID,
    PROCESSING_FACE_ID,
    PlannedChunk,
    plan_reply,
)
from .stickers import (
    qq_face_message,
    random_local_sticker_message,
    random_sticker_message,
)
from .handler_services import HandlerService
from .application import ChatTurnResult
from .handler_constants import (SEND_RETRY_DELAY_SECONDS, SEND_RETRY_MAX_CHARS)


class ReplyService(HandlerService):
    def _is_napcat_send_timeout(self, exc: ActionFailed) -> bool:
        info = getattr(exc, "info", {})
        if not isinstance(info, dict) or info.get("retcode") != 1200:
            return False

        detail = "\n".join(
            str(info.get(key, "")) for key in ("message", "wording")
        ).lower()
        return "timeout:" in detail and "sendmsg" in detail

    def _make_retry_text(self, message: str) -> str:
        text = re.sub(r"https?://\S+", "[链接已省略]", message)
        if len(text) <= SEND_RETRY_MAX_CHARS:
            return text
        return text[:SEND_RETRY_MAX_CHARS].rstrip() + "\n\n[回复较长，已缩短]"

    def _reply_target_segments(self, event: MessageEvent) -> list[MessageSegment]:
        if not isinstance(event, GroupMessageEvent):
            return []
        if event.user_id == event.self_id:
            return []
        return [MessageSegment.at(event.user_id), MessageSegment.text(" ")]

    def _reply_message(self,
        event: MessageEvent,
        content: Message | MessageSegment | str,
    ) -> Message:
        mention_user_id = None
        if isinstance(event, GroupMessageEvent) and event.user_id != event.self_id:
            mention_user_id = event.user_id
        return compose_onebot_reply(
            content,
            reply_native_message_id=event.message_id,
            mention_native_user_id=mention_user_id,
        )

    def _planned_chunk_message(self,
        event: MessageEvent,
        chunk: PlannedChunk,
        *,
        first: bool,
        content: Message | MessageSegment | str | None = None,
    ) -> Message:
        rendered_content = chunk.text if content is None else content
        if chunk.reply_message_id is not None and self.context.message_ledger is not None:
            target = self.context.message_ledger.get_in_scope(
                self.services.chat._conversation_scope(event),
                chunk.reply_message_id,
            )
            if target is not None and target.native_message_id:
                mention_user_id: int | None = None
                if isinstance(event, GroupMessageEvent):
                    try:
                        candidate = int(target.sender_native_user_id)
                    except (TypeError, ValueError):
                        candidate = event.self_id
                    if candidate != event.self_id:
                        mention_user_id = candidate
                return compose_onebot_reply(
                    rendered_content,
                    reply_native_message_id=int(target.native_message_id),
                    mention_native_user_id=mention_user_id,
                )
        if first:
            return self._reply_message(event, rendered_content)
        return Message(rendered_content)

    async def _render_planned_chunk_message(self,
        event: MessageEvent,
        chunk: PlannedChunk,
        *,
        first: bool,
        output_resolver: OneBotModelOutputResolver | None = None,
    ) -> Message:
        content: Message | MessageSegment | None = None
        resolved_content = (
            await output_resolver.render(chunk.text)
            if output_resolver is not None
            else None
        )
        has_native_segments = bool(
            resolved_content is not None
            and any(segment.type != "text" for segment in resolved_content)
        )
        if self.context.rich_renderer is not None and not has_native_segments:
            rich_source = (
                resolved_content.extract_plain_text()
                if resolved_content is not None
                else chunk.text
            )
            try:
                png = await self.context.rich_renderer.render(rich_source)
            except Exception as exc:
                self.context.logger.warning(f"Rich message rendering fell back to text: {exc}")
            else:
                if png:
                    self.context.logger.info("Rendered rich reply chunk as PNG.")
                    content = MessageSegment.image(png)
        if content is None and resolved_content is not None:
            content = resolved_content
            if not content:
                return Message()
        return self._planned_chunk_message(
            event,
            chunk,
            first=first,
            content=content,
        )

    def _reaction_target_message_id(self,
        event: MessageEvent,
        canonical_message_id: int | None = None,
    ) -> int | None:
        if not isinstance(event, GroupMessageEvent):
            return None
        if canonical_message_id is not None and self.context.message_ledger is not None:
            target = self.context.message_ledger.get_in_scope(
                scope_from_event(event),
                canonical_message_id,
            )
            if target is not None and target.native_message_id:
                try:
                    return int(target.native_message_id)
                except (TypeError, ValueError):
                    pass
        return event.message_id

    async def _set_message_reaction(self,
        bot: Bot,
        event: MessageEvent,
        face_id: int,
        *,
        added: bool,
        canonical_message_id: int | None = None,
    ) -> bool:
        target = self._reaction_target_message_id(event, canonical_message_id)
        if target is None:
            return False
        try:
            await bot.call_api(
                "set_msg_emoji_like",
                message_id=target,
                emoji_id=int(face_id),
                set=added,
            )
        except (ActionFailed, RuntimeError, TypeError, ValueError) as exc:
            self.context.logger.debug(f"QQ message reaction is unavailable: {exc}")
            return False
        return True

    def _make_retry_message(self, message: Message | str) -> Message | str:
        if isinstance(message, str):
            return self._make_retry_text(message)

        reply_segments = [
            segment for segment in message if segment.type in {"reply", "at"}
        ]
        plain_text = message.extract_plain_text()
        if reply_segments:
            plain_text = plain_text.lstrip()
        text = self._make_retry_text(plain_text)
        if reply_segments:
            reply_segments.append(MessageSegment.text(" "))
        return Message([*reply_segments, MessageSegment.text(text)])

    async def _finish_safely(self,
        matcher,
        message: Message | MessageSegment | str,
        label: str = "message",
        retry_on_timeout: bool = False,
        on_sent=None,
        on_attempt=None,
        on_outcome_unknown=None,
        on_failed=None,
        finish: bool = True,
    ) -> bool:
        async def notify(callback, *args: Any) -> None:
            if callback is None:
                return
            try:
                await callback(*args)
            except Exception as exc:
                self.context.logger.warning(f"Post-send journal callback failed for {label}: {exc}")

        attempt = 1
        await notify(on_attempt, attempt, message)
        try:
            with telemetry.delivery("onebot-v11"):
                response = await matcher.send(message)
            await notify(on_sent, response, message, attempt)
            if finish:
                raise FinishedException
            return True
        except ActionFailed as exc:
            if not self._is_napcat_send_timeout(exc):
                await notify(on_failed, attempt, message, str(exc))
                raise

            await notify(on_outcome_unknown, attempt, message, str(exc))

            if retry_on_timeout and isinstance(message, (Message, str)):
                self.context.logger.warning(
                    f"NapCat timed out waiting for the {label} receipt; "
                    "retrying once with a shorter QQ-safe reply."
                )
                await asyncio.sleep(SEND_RETRY_DELAY_SECONDS)
                retry_message = self._make_retry_message(message)
                attempt += 1
                await notify(on_attempt, attempt, retry_message)
                try:
                    with telemetry.delivery("onebot-v11"):
                        response = await matcher.send(retry_message)
                    await notify(on_sent, response, retry_message, attempt)
                except ActionFailed as retry_exc:
                    if not self._is_napcat_send_timeout(retry_exc):
                        await notify(
                            on_failed,
                            attempt,
                            retry_message,
                            str(retry_exc),
                        )
                        raise
                    await notify(
                        on_outcome_unknown,
                        attempt,
                        retry_message,
                        str(retry_exc),
                    )
                    self.context.logger.error(f"NapCat timed out again while sending the {label}.")
                    if finish:
                        raise FinishedException
                    return False
                except Exception as retry_exc:
                    await notify(
                        on_failed,
                        attempt,
                        retry_message,
                        str(retry_exc),
                    )
                    raise
                if finish:
                    raise FinishedException
                return True

            self.context.logger.warning(
                f"NapCat timed out waiting for the {label} receipt; "
                "the message may already have been sent, so it will not be retried."
            )
            if finish:
                raise FinishedException
            return False
        except FinishedException:
            raise
        except Exception as exc:
            await notify(on_failed, attempt, message, str(exc))
            raise

    async def _finish_sticker(self, matcher, event: MessageEvent) -> None:
        try:
            await self._finish_safely(
                matcher,
                self._reply_message(event, random_sticker_message()),
                "sticker",
            )
        except ActionFailed as exc:
            self.context.logger.warning(f"Learned sticker send failed, fallback to local sticker: {exc}")
            await self._finish_safely(
                matcher,
                self._reply_message(event, random_local_sticker_message()),
                "local sticker",
            )

    async def _finish_qq_face(self,
        matcher, event: MessageEvent, text: str = ""
    ) -> None:
        try:
            await self._finish_safely(
                matcher,
                self._reply_message(event, qq_face_message(text)),
                "QQ face",
            )
        except ActionFailed as exc:
            self.context.logger.warning(f"QQ builtin face send failed: {exc}")
            await self._finish_safely(
                matcher,
                self._reply_message(
                    event,
                    "这个 QQ 自带表情发不出去，换个 ID 试试。",
                ),
                "QQ face fallback",
            )

    async def _finish_tracked_ai(self,
        matcher,
        bot: Bot,
        event: MessageEvent,
        user_text: str,
        *,
        label: str,
        retry_on_timeout: bool = False,
        **kwargs: Any,
    ) -> None:
        processing_added = await self._set_message_reaction(
            bot,
            event,
            PROCESSING_FACE_ID,
            added=True,
        )
        result: ChatTurnResult | None = None
        send_index = 0
        delivery_ids: dict[int, int] = {}
        stream_context: dict[str, int | None] = {"turn_id": None}
        final_stream_state = FinalStreamState()
        stream_message_count = 0
        output_resolver = OneBotModelOutputResolver(bot, event, self.context.message_ledger)

        async def send_stream_fragment(fragment: str) -> None:
            nonlocal stream_message_count
            plan = plan_reply(fragment)
            if plan.silence:
                return
            for chunk in plan.chunks:
                stream_index = stream_message_count
                outgoing = await self._render_planned_chunk_message(
                    event,
                    chunk,
                    first=stream_index == 0,
                    output_resolver=output_resolver,
                )
                if not outgoing:
                    continue
                target_scope = scope_from_event(event)
                source_scope = self.services.chat._conversation_scope(event)
                turn_id = stream_context.get("turn_id")
                delivery: Delivery | None = None
                if self.context.delivery_store is not None:
                    decoded = decode_onebot_message(outgoing)
                    delivery, created = self.context.delivery_store.enqueue(
                        idempotency_key=(
                            f"onebot:{event.self_id}:{target_scope.key}:"
                            f"trigger:{event.message_id}:turn:{turn_id or 0}:"
                            f"stream:{stream_index}"
                        ),
                        source_scope_key=source_scope.key,
                        source_canonical_message_id=(
                            self.context.message_ledger.canonical_id_for_native(
                                source_scope,
                                event.message_id,
                            )
                            if self.context.message_ledger is not None
                            else None
                        ),
                        turn_id=turn_id,
                        target_scope=target_scope,
                        body=decoded.body,
                        reply_to_native_message_id=decoded.reply_to_native_message_id,
                    )
                    if not created and delivery.status in {
                        "sending",
                        "ambiguous",
                        "committed",
                        "cancelled",
                    }:
                        stream_message_count += 1
                        continue
                    self.context.delivery_store.begin_direct_attempt(delivery.delivery_id)
                if turn_id is not None and self.context.turn_journal is not None:
                    self.context.turn_journal.record_send_started(turn_id, 100000 + stream_index)
                try:
                    with telemetry.delivery("onebot-v11"):
                        response = await bot.send(event, outgoing)
                except ActionFailed as exc:
                    if self.context.delivery_store is not None and delivery is not None:
                        if self._is_napcat_send_timeout(exc):
                            self.context.delivery_store.mark_ambiguous(delivery.delivery_id, str(exc))
                        else:
                            self.context.delivery_store.mark_failed(
                                delivery.delivery_id,
                                str(exc),
                                retryable=False,
                            )
                    if turn_id is not None and self.context.turn_journal is not None:
                        self.context.turn_journal.record_send_finished(
                            turn_id,
                            100000 + stream_index,
                            (
                                "outcome-unknown"
                                if self._is_napcat_send_timeout(exc)
                                else "failed"
                            ),
                            {"ok": False, "error": str(exc)},
                        )
                    if not self._is_napcat_send_timeout(exc):
                        raise
                    self.context.logger.warning(
                        "A streamed paragraph timed out; it will wait for echo "
                        "instead of being sent twice."
                    )
                else:
                    native_message_id = self._sent_message_id(response)
                    if self.context.delivery_store is not None and delivery is not None:
                        self.context.delivery_store.mark_committed(
                            delivery.delivery_id,
                            native_message_id=native_message_id or "",
                        )
                    canonical_message_id: int | None = None
                    if native_message_id is not None and self.context.message_ledger is not None:
                        stored = record_onebot_outgoing(
                            self.context.message_ledger,
                            source_scope,
                            native_message_id=native_message_id,
                            message=outgoing,
                            occurred_at=int(time.time()),
                        )
                        canonical_message_id = stored.canonical_message_id
                        if turn_id is not None and self.context.turn_journal is not None:
                            self.context.turn_journal.link_send(
                                turn_id,
                                canonical_message_id,
                                node_id=f"stream:{stream_index}",
                            )
                        if self.context.bridge_manager is not None:
                            decoded = decode_onebot_message(outgoing)
                            self.context.bridge_manager.mirror_local_outgoing(
                                source_scope=target_scope,
                                source_native_event_id=str(native_message_id),
                                canonical_message_id=canonical_message_id,
                                body=decoded.body,
                                occurred_at=int(time.time()),
                                reply_to_native_message_id=(
                                    decoded.reply_to_native_message_id
                                ),
                            )
                    if turn_id is not None and self.context.turn_journal is not None:
                        self.context.turn_journal.record_send_finished(
                            turn_id,
                            100000 + stream_index,
                            "committed",
                            {
                                "ok": True,
                                "delivery_id": (
                                    delivery.delivery_id if delivery is not None else None
                                ),
                                "canonical_message_id": canonical_message_id,
                            },
                        )
                stream_message_count += 1
                if self.context.settings.reply_chunk_delay_seconds:
                    await asyncio.sleep(self.context.settings.reply_chunk_delay_seconds)

        if self.context.settings.stream_enabled:
            kwargs.setdefault("final_stream_sink", send_stream_fragment)
            kwargs.setdefault("final_stream_state", final_stream_state)
            kwargs.setdefault("_stream_context", stream_context)

        async def record_send_attempt(
            attempt: int,
            sent_message: Message | str,
        ) -> None:
            if result is not None and self.context.delivery_store is not None:
                target_scope = scope_from_event(event)
                source_scope = self.services.chat._conversation_scope(event)
                decoded = decode_onebot_message(sent_message)
                delivery, _created = self.context.delivery_store.enqueue(
                    idempotency_key=(
                        f"onebot:{event.self_id}:{target_scope.key}:"
                        f"trigger:{event.message_id}:turn:{result.turn_id or 0}:"
                        f"chunk:{send_index}"
                    ),
                    source_scope_key=source_scope.key,
                    source_canonical_message_id=(
                        self.context.message_ledger.canonical_id_for_native(
                            source_scope,
                            event.message_id,
                        )
                        if self.context.message_ledger is not None
                        else None
                    ),
                    turn_id=result.turn_id,
                    target_scope=target_scope,
                    body=decoded.body,
                    reply_to_native_message_id=(
                        decoded.reply_to_native_message_id
                    ),
                )
                delivery_ids[send_index] = delivery.delivery_id
                self.context.delivery_store.begin_direct_attempt(delivery.delivery_id)
            if result is None or result.turn_id is None or self.context.turn_journal is None:
                return
            self.context.turn_journal.record_send_started(
                result.turn_id,
                send_index * 10 + attempt,
            )

        async def link_sent_reply(
            response: Any,
            sent_message: Message | str,
            attempt: int,
        ) -> None:
            if result is None:
                return
            canonical_message_id = None
            native_message_id = self._sent_message_id(response)
            if native_message_id is not None and self.context.message_ledger is not None:
                canonical_scope = self.services.chat._conversation_scope(event)
                stored = record_onebot_outgoing(
                    self.context.message_ledger,
                    canonical_scope,
                    native_message_id=native_message_id,
                    message=sent_message,
                    occurred_at=int(time.time()),
                )
                canonical_message_id = stored.canonical_message_id
                if result.turn_id is not None and self.context.turn_journal is not None:
                    self.context.turn_journal.link_send(
                        result.turn_id,
                        canonical_message_id,
                        node_id=f"final:{send_index}",
                    )
                if self.context.bridge_manager is not None:
                    decoded = decode_onebot_message(sent_message)
                    self.context.bridge_manager.mirror_local_outgoing(
                        source_scope=scope_from_event(event),
                        source_native_event_id=str(native_message_id),
                        canonical_message_id=canonical_message_id,
                        body=decoded.body,
                        occurred_at=int(time.time()),
                        reply_to_native_message_id=(
                            decoded.reply_to_native_message_id
                        ),
                    )
            delivery_id = delivery_ids.get(send_index)
            if self.context.delivery_store is not None and delivery_id is not None:
                self.context.delivery_store.mark_committed(
                    delivery_id,
                    native_message_id=native_message_id or "",
                )
            if result.turn_id is not None and self.context.turn_journal is not None:
                self.context.turn_journal.record_send_finished(
                    result.turn_id,
                    send_index * 10 + attempt,
                    "committed",
                    {
                        "ok": True,
                        "delivery_id": delivery_id,
                        "canonical_message_id": canonical_message_id,
                        "receipt_message_id_present": native_message_id is not None,
                    },
                )

        async def record_unknown_send(
            attempt: int,
            sent_message: Message | str,
            detail: str,
        ) -> None:
            del sent_message
            delivery_id = delivery_ids.get(send_index)
            if self.context.delivery_store is not None and delivery_id is not None:
                self.context.delivery_store.mark_ambiguous(delivery_id, detail)
            if result is not None and result.turn_id is not None and self.context.turn_journal is not None:
                self.context.turn_journal.record_send_finished(
                    result.turn_id,
                    send_index * 10 + attempt,
                    "outcome-unknown",
                    {"ok": False, "error": detail},
                )

        async def record_failed_send(
            attempt: int,
            sent_message: Message | str,
            detail: str,
        ) -> None:
            del sent_message
            delivery_id = delivery_ids.get(send_index)
            if self.context.delivery_store is not None and delivery_id is not None:
                self.context.delivery_store.mark_failed(delivery_id, detail, retryable=False)
            if result is not None and result.turn_id is not None and self.context.turn_journal is not None:
                self.context.turn_journal.record_send_finished(
                    result.turn_id,
                    send_index * 10 + attempt,
                    "failed",
                    {"ok": False, "error": detail},
                )

        try:
            result = await self.services.chat._run_tracked_ai(bot, event, user_text, **kwargs)
            if result is None:
                return

            if isinstance(result.reply, str):
                reply_plan = plan_reply(result.reply)
                if reply_plan.silence:
                    if processing_added:
                        await self._set_message_reaction(
                            bot,
                            event,
                            PROCESSING_FACE_ID,
                            added=False,
                        )
                        processing_added = False
                    await self._set_message_reaction(
                        bot,
                        event,
                        reply_plan.silence_face_id or 7,
                        added=True,
                        canonical_message_id=(
                            reply_plan.silence_reply_message_id
                        ),
                    )
                    raise FinishedException
                outgoing_messages = []
                for index, chunk in enumerate(reply_plan.chunks):
                    outgoing = await self._render_planned_chunk_message(
                        event,
                        chunk,
                        first=index == 0 and stream_message_count == 0,
                        output_resolver=output_resolver,
                    )
                    if outgoing:
                        outgoing_messages.append(outgoing)
            else:
                decoded_reply = decode_onebot_message(result.reply)
                resolved_reply = Message()
                for node in decoded_reply.body.nodes:
                    if isinstance(node, TextNode):
                        resolved_reply.extend(await output_resolver.render(node.text))
                    else:
                        resolved_reply.extend(render_onebot_body(MessageBody((node,))))
                outgoing_messages = (
                    [self._reply_message(event, resolved_reply)] if resolved_reply else []
                )

            if not outgoing_messages:
                raise FinishedException

            for index, outgoing in enumerate(outgoing_messages):
                send_index = index
                is_last = index == len(outgoing_messages) - 1
                committed = await self._finish_safely(
                    matcher,
                    outgoing,
                    f"{label} chunk {index + 1}/{len(outgoing_messages)}",
                    retry_on_timeout=(
                        retry_on_timeout and self.context.delivery_store is None
                    ),
                    on_sent=link_sent_reply,
                    on_attempt=record_send_attempt,
                    on_outcome_unknown=record_unknown_send,
                    on_failed=record_failed_send,
                    finish=is_last,
                )
                if not committed:
                    raise FinishedException
                if not is_last and self.context.settings.reply_chunk_delay_seconds:
                    await asyncio.sleep(self.context.settings.reply_chunk_delay_seconds)
        except FinishedException:
            raise
        except Exception:
            if isinstance(event, GroupMessageEvent):
                await self._set_message_reaction(
                    bot,
                    event,
                    FAILURE_FACE_ID,
                    added=True,
                )
            raise
        finally:
            if processing_added:
                await self._set_message_reaction(
                    bot,
                    event,
                    PROCESSING_FACE_ID,
                    added=False,
                )

    def _journal_reply_text(self, reply: Message | str) -> str:
        if isinstance(reply, Message):
            return self.services.chat._render_message_text(reply)
        return str(reply)

    def _sent_message_id(self, response: Any) -> int | None:
        if isinstance(response, dict):
            raw_message_id = response.get("message_id")
        elif isinstance(response, int):
            raw_message_id = response
        else:
            raw_message_id = getattr(response, "message_id", None)
        try:
            return int(raw_message_id) if raw_message_id is not None else None
        except (TypeError, ValueError):
            return None
