"""OneBot boundary for opt-in, group-local wiki generation and lookup."""

from __future__ import annotations

import asyncio

from nonebot import get_bot
from nonebot.adapters.onebot.v11 import Bot, GroupIncreaseNoticeEvent, GroupMessageEvent, MessageEvent

from .agent_loop import AgentTrace, ask_deepseek_json
from .conversation_scope import ConversationScope
from .bridges import BridgePermanentError, BridgeRetryableError
from .delivery import Delivery
from .group_wiki import NEWCOMER_JOB_KIND, WIKI_JOB_KIND, WikiLeaseLost, WikiSuperseded
from .group_wiki_worker import GroupWikiWorker, WIKI_SYSTEM_PROMPT, generation_evidence
from .handler_services import HandlerService
from .onebot_codec import scope_from_event
from .storage.jobs import DurableJob
from .workers.durable_jobs import JobDeferred


class GroupWikiHandlers(HandlerService):
    def __init__(self, services):
        super().__init__(services)
        settings = self.context.settings
        self.worker = GroupWikiWorker(
            self.context.group_wiki_store, db_call=self.context.db_call,
            generate=self.generate, permitted=self.permitted, logger=self.context.logger,
            hour=settings.group_wiki_hour, timezone=settings.group_wiki_timezone,
            check_seconds=settings.group_wiki_check_seconds,
        )
        self.context.job_worker.register(WIKI_JOB_KIND, self.worker.handle_job, timeout_seconds=210)
        self.context.job_worker.register(NEWCOMER_JOB_KIND, self.handle_newcomer_job, timeout_seconds=45)

    async def newcomer_notice(self, event: GroupIncreaseNoticeEvent) -> None:
        scope = ConversationScope("onebot-v11", "group", str(event.group_id))
        if not await self.permitted(scope, str(event.self_id)):
            return
        await self.context.db_call(self.context.group_wiki_store.enqueue_newcomer, scope,
            bot_id=str(event.self_id), user_id=str(event.user_id), noticed_at=event.time)

    async def check_member(self, bot: Bot, payload: dict) -> None:
        scope = ConversationScope(*payload["scope_key"].split(":", 2))
        if str(bot.self_id) != payload["bot_id"]:
            raise BridgePermanentError("新人简报发送账号不匹配")
        if not await self.context.db_call(self.services.group_enabled, int(scope.native_conversation_id)):
            raise BridgePermanentError("新人简报来源群已停用")
        try:
            async with asyncio.timeout(10):
                members = await bot.get_group_member_list(group_id=int(scope.native_conversation_id), no_cache=True)
            if not isinstance(members, list) or any(
                not isinstance(member, dict) or not str(member.get("user_id", "")).isdigit() for member in members
            ):
                raise ValueError("Invalid group membership response")
        except Exception as exc:
            raise BridgeRetryableError("暂时无法确认新人是否仍在群内") from exc
        if payload["user_id"] not in {str(member["user_id"]) for member in members}:
            raise BridgePermanentError("新人已经不在来源群，取消私聊简报")

    async def handle_newcomer_job(self, job: DurableJob) -> dict:
        try:
            payload = await self.context.db_call(self.context.group_wiki_store.newcomer_execution, job)
            try:
                bot = get_bot(payload["bot_id"])
            except (KeyError, ValueError) as exc:
                raise JobDeferred("新人简报的原 QQ 账号尚未连接", delay_seconds=60) from exc
            await self.check_member(bot, payload)
            delivery = await self.context.db_call(self.context.group_wiki_store.queue_newcomer_delivery,
                                                  job, self.context.delivery_store)
            return {"status": "queued", "delivery_id": delivery.delivery_id}
        except (WikiLeaseLost, WikiSuperseded, BridgePermanentError) as exc:
            return {"status": "skipped", "reason": str(exc)}
        except BridgeRetryableError as exc:
            raise JobDeferred(str(exc), delay_seconds=60) from exc

    async def validate_newcomer_delivery(self, bot: Bot, delivery: Delivery) -> dict:
        try:
            payload = await self.context.db_call(self.context.group_wiki_store.newcomer_delivery, delivery)
            await self.check_member(bot, payload)
            # Membership lookup is a network round trip; settings/evidence may have changed meanwhile.
            return await self.context.db_call(self.context.group_wiki_store.newcomer_delivery, delivery)
        except WikiSuperseded as exc:
            raise BridgePermanentError(str(exc)) from exc

    async def permitted(self, scope: ConversationScope, bot_id: str) -> bool:
        if scope.platform != "onebot-v11" or scope.kind != "group":
            return False
        if not await self.context.db_call(self.services.group_enabled, int(scope.native_conversation_id)):
            return False
        try:
            bot = get_bot(bot_id)
        except (KeyError, ValueError):
            return False
        return str(bot.self_id) == bot_id

    async def generate(self, payload: dict) -> dict:
        profile = self.services.chat._background_model_profile(self.context.settings.group_wiki_profile, "")
        trace = AgentTrace(provider=profile.provider_identity, model=profile.model, profile=profile.name)
        try:
            return await ask_deepseek_json(WIKI_SYSTEM_PROMPT, generation_evidence(payload),
                                           profile=profile, trace=trace, recover_empty_response=False)
        finally:
            await self.context.db_call(self.services.triggers._record_background_usage,
                                       payload["scope_key"], "group-wiki", trace)

    async def command(self, event: MessageEvent, text: str) -> str:
        if not isinstance(event, GroupMessageEvent):
            return "请在对应群里使用 /群百科，不读取其他群的内容。"
        if not await self.context.db_call(self.services.group_enabled, event.group_id):
            return "当前群已停用。"
        scope = scope_from_event(event)
        store = self.context.group_wiki_store
        config = await self.context.db_call(store.settings, scope)
        action = text.strip()
        if action in {"开启", "关闭", "新人简报 开启", "新人简报 关闭"}:
            if event.user_id not in self.context.settings.admin_user_ids and event.sender.role not in {"admin", "owner"}:
                return "只有本群管理员或机器人管理员可以开关群百科和新人简报。"
            newcomer_action = action.startswith("新人简报 ")
            if newcomer_action and not config["enabled"]:
                return "请先开启本群百科，再配置新人简报。"
            try:
                await self.context.db_call(store.configure, scope, bot_id=str(event.self_id),
                    enabled=config["enabled"] if newcomer_action else action == "开启",
                    newcomer_enabled=action.endswith("开启") if newcomer_action else config["newcomer_enabled"],
                    expected_version=config["version"])
            except ValueError:
                return "配置刚被修改，请重新操作。"
            if newcomer_action:
                return "已开启新人私聊简报。" if action.endswith("开启") else "已关闭新人私聊简报。"
            return "已开启群百科，将在夜间整理已有章节。" if action == "开启" else "已关闭群百科，并取消待执行的整理任务。"
        if not config["enabled"]:
            return "本群百科尚未开启；管理员可发送 /群百科 开启。"
        page = await self.context.db_call(store.page, scope, query=action)
        items = page["items"][:10]
        if not items:
            return "暂时没有匹配的百科条目。" if action else "群百科尚无可展示条目，等待夜间整理。"
        labels = {"decision": "决定", "faq": "问答", "jargon": "术语", "expertise": "擅长领域"}
        lines = [f"群百科 · v{page['revision']}"]
        for item in items:
            refs = ", ".join(f"msg#{value}" for value in item["evidence_ids"])
            line = f"[{labels[item['category']]}] {item['title']}\n{item['content']}\n依据：{refs}"
            if sum(len(value) for value in lines) + len(line) > 5000:
                break
            lines.append(line)
        displayed = len(lines) - 1
        if len(page["items"]) > displayed:
            lines.append(f"共 {len(page['items'])} 条，显示前 {displayed} 条；可用 /群百科 关键词 筛选。")
        return "\n\n".join(lines)
