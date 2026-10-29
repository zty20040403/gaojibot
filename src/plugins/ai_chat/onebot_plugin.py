"""Register QQ matchers and driver lifecycle hooks without opening runtime stores."""

from __future__ import annotations

from nonebot import get_app, get_driver, logger
from nonebot.adapters.onebot.v11 import Bot, MessageEvent
from nonebot.message import event_preprocessor

from .config import settings
from .matcher_handlers import register_matcher_handlers
from .mobile_authorization import redact_approval_event_logs
from .onebot_runtime import OneBotRuntime
from .paths import CACHE_DIR, PROJECT_ROOT, STATE_DIR

redact_approval_event_logs()
_runtime = OneBotRuntime(
    settings,
    state_dir=STATE_DIR,
    cache_dir=CACHE_DIR,
    project_root=PROJECT_ROOT,
    logger=logger,
)
driver = get_driver()


@event_preprocessor
async def ignore_disabled_group_event(bot: Bot, event: MessageEvent) -> None:
    await _runtime.preprocess(bot, event)


@driver.on_startup
async def startup_app_context() -> None:
    await _runtime.start(get_app())


@driver.on_shutdown
async def shutdown_app_context() -> None:
    await _runtime.shutdown()


@driver.on_bot_connect
async def connect_migration_baseline(bot: Bot) -> None:
    await _runtime.connected(bot)


register_matcher_handlers(_runtime)
