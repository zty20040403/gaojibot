from __future__ import annotations

from pathlib import Path
from typing import Any


from src.bot_security.service import MobileAuthorization, approval_command
from src.bot_security.store import SecurityError, SecurityStore
from src.bot_security.cli import read_secret


def build_mobile_authorization(settings: Any, database: Any, *, onebot_enabled: bool = True) -> MobileAuthorization | None:
    if not onebot_enabled:
        from .application_security import build_console_authorization
        return build_console_authorization(settings, database)
    from nonebot import get_bots, get_plugin_config
    from nonebot.adapters.onebot.v11 import Bot
    from nonebot.adapters.onebot.v11.config import Config as OneBotConfig
    secret_file = str(getattr(settings, "admin_secret_file", "") or "")
    if not secret_file:
        return None
    if database is None:
        raise RuntimeError("管理员账户和一次性口令必须使用 PostgreSQL，请先配置 AI_POSTGRES_DSN")
    transport = get_plugin_config(OneBotConfig)
    if not transport.onebot_access_token or not transport.onebot_secret:
        raise RuntimeError("手机授权需要配置 ONEBOT_ACCESS_TOKEN 和 ONEBOT_SECRET，以认证 QQ 事件来源")
    path = Path(secret_file)
    secret = read_secret(path)
    store = SecurityStore(database, secret)

    def select_bot() -> str:
        bots = {key: value for key, value in get_bots().items() if isinstance(value, Bot)}
        configured = str(getattr(settings, "admin_bot_id", "") or "")
        if configured and configured in bots:
            return configured
        if not configured and len(bots) == 1:
            return next(iter(bots))
        raise SecurityError("QQ 机器人未连接，或存在多个机器人；请配置 AI_ADMIN_BOT_ID", 503)

    async def send_private(bot_id: str, qq_id: str, text: str) -> None:
        bot = get_bots().get(bot_id)
        if not isinstance(bot, Bot):
            raise SecurityError("QQ 机器人未连接", 503)
        await bot.send_private_msg(user_id=int(qq_id), message=text, auto_escape=True)

    return MobileAuthorization(store, sender=send_private, bot_selector=select_bot)


def redact_approval_event_logs() -> None:
    """NoneBot logs events before preprocessors; suppress the command at that boundary."""
    from nonebot.adapters.onebot.v11 import MessageEvent, PrivateMessageEvent
    original = MessageEvent.get_log_string
    if getattr(original, "_approval_redacted", False):
        return

    def get_log_string(event: MessageEvent) -> str:
        if approval_command(event.get_plaintext(), private=isinstance(event, PrivateMessageEvent)):
            return f"Message from {event.user_id}: [手机授权消息已隐藏]"
        return original(event)

    get_log_string._approval_redacted = True
    MessageEvent.get_log_string = get_log_string


async def handle_approval_event(mobile: MobileAuthorization | None, bot: Any, event: Any) -> bool:
    from nonebot.adapters.onebot.v11 import PrivateMessageEvent
    text = event.get_plaintext()
    private = isinstance(event, PrivateMessageEvent)
    if not approval_command(text, private=private):
        return False
    if mobile is None:
        response = "手机授权尚未初始化，操作不会执行。"
    else:
        try:
            response = await mobile.handle_message(qq_id=str(event.user_id), bot_id=str(bot.self_id), text=text, private=private)
        except Exception:
            response = "授权服务暂时不可用，请稍后查询审批状态；不要重复发起操作。"
    # Never quote the original confirmation (including its code) in a reply.
    try:
        if private:
            await bot.send_private_msg(user_id=event.user_id, message=response, auto_escape=True)
        else:
            await bot.send_group_msg(group_id=event.group_id, message=response, auto_escape=True)
    except Exception:
        # Never let a transport traceback serialize the original code-bearing event.
        pass
    return True
