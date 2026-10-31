"""Nightly memory tidy, request-log pruning and proactive feedback settlement."""
from __future__ import annotations

import asyncio
from datetime import datetime

from .agent_loop import AgentTrace, ask_deepseek
from .handler_constants import SHANGHAI_TZ

TIDY_HOUR = 5
TIDY_PROMPT = """你在帮一个群聊机器人整理它自己的记忆笔记。下面是笔记全文，原样输出整理后的全文，不要任何解释。
- 分「## 群里」「## 人」两节，人按「### 名字」分，一行一条。末尾追加的零散条目归进该去的节。
- 说同一件事的合成一条；前后矛盾的留新的；写了到哪天为止、已经过期的删掉。
- 保留每条结尾的（日期 #消息编号）。
- 不改写意思，不新增内容；超过 3000 token 时删最不要紧的。"""


class ApplicationMaintenance:
    def __init__(self):
        self.context = None
        self._last_tidy_day = ""

    def bind(self, context):
        if self.context is not None:
            raise RuntimeError("application_maintenance_already_bound")
        self.context = context

    async def tidy_memory(self, scope_key: str, content: str) -> bool:
        """Rewrite one memory file; keep the original if the result looks wrong."""
        trace = AgentTrace()
        profile = self.context.model_catalog.resolve_runtime(
            profile=self.context.settings.dream_profile or None, model=None)
        tidied = (await ask_deepseek(content, [], profile=profile, trace=trace, system_prompt=TIDY_PROMPT)).strip()
        if self.context.usage_store is not None and (trace.input_tokens or trace.output_tokens):
            await self.context.db_call(self.context.usage_store.record, scope_key=scope_key, source="memory-tidy",
                provider=trace.provider, model=trace.model, input_tokens=trace.input_tokens,
                output_tokens=trace.output_tokens, cached_tokens=trace.cached_tokens)
        if "##" not in tidied or len(tidied) < len(content) / 3:
            self.context.logger.warning("Memory tidy output rejected; the original memory is kept.")
            return False
        return await self.context.db_call(self.context.conversations.save_tidied, scope_key, content, tidied)

    async def _nightly_loop(self) -> None:
        while True:
            now = datetime.now(SHANGHAI_TZ)
            day = now.date().isoformat()
            if now.hour >= TIDY_HOUR and day != self._last_tidy_day and self.context.conversations is not None:
                self._last_tidy_day = day
                store = self.context.conversations
                for scope_key, content in await self.context.db_call(store.memories_to_tidy):
                    try:
                        await self.tidy_memory(scope_key, content)
                    except Exception as exc:
                        # One bad scope never stops the others; it is retried tomorrow.
                        self.context.logger.warning(f"Memory tidy failed: {type(exc).__name__}")
                pruned = await self.context.db_call(store.prune_requests)
                if pruned:
                    self.context.logger.info(f"Pruned {pruned} saved model request(s).")
            await asyncio.sleep(600)

    async def _proactive_feedback_loop(self) -> None:
        while True:
            try:
                await self.context.db_call(self.context.proactive_learning.settle_due)
            except (OSError, RuntimeError, ValueError) as exc:
                self.context.logger.warning(f"Proactive feedback settlement deferred: {type(exc).__name__}")
            await asyncio.sleep(30)
