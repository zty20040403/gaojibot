"""Safe desktop liveness record; no tokens, messages or provider settings."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import time

from .files import write_json


def install_health_writer() -> None:
    import nonebot
    driver = nonebot.get_driver()
    filename = Path(os.environ["AI_STATE_DIR"]) / "desktop-health.json"
    qq = os.environ["AI_ADMIN_BOT_ID"]
    task = None

    def record(connected: bool) -> None:
        write_json(filename, {"qq": qq, "connected": connected, "updated_at": int(time.time())})

    async def loop() -> None:
        while True:
            record(qq in nonebot.get_bots())
            await asyncio.sleep(3)

    @driver.on_startup
    async def start() -> None:
        nonlocal task
        filename.parent.mkdir(parents=True, exist_ok=True)
        task = asyncio.create_task(loop())

    @driver.on_shutdown
    async def stop() -> None:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        record(False)
