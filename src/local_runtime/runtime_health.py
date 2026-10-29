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


class SDKFreeHealthWriter:
    """One host lifecycle writer for official-only or dual mode; no SDK import."""
    def __init__(self, path, official_getter, *, legacy_getter=None, clock=time.time, interval=3):
        self.path=Path(path)
        if not self.path.is_absolute() or not callable(official_getter) or not 0.1<=interval<=60:
            raise ValueError('invalid_health_writer')
        self.official_getter,self.legacy_getter=official_getter,legacy_getter
        self.clock,self.interval=clock,interval
        self._task=None
        self._closed=False

    def record(self, *, stopping=False):
        try:
            service=self.official_getter()
            value=service.snapshot() if service is not None else {}
        except Exception:value={}
        official={'healthy':not stopping and value.get('healthy') is True,
            'lease_current':not stopping and value.get('lease',{}).get('current') is True,
            'gateway_ready':not stopping and value.get('gateway',{}).get('state')=='ready'
                and value.get('gateway',{}).get('message_gap') is not True}
        document={'updated_at':int(self.clock()),'official':official}
        if self.legacy_getter is not None:
            try:legacy=self.legacy_getter()
            except Exception:legacy={}
            # Preserve the legacy desktop contract only for an actual legacy host getter.
            document['qq']=str(legacy.get('qq',''))
            document['connected']=not stopping and legacy.get('connected') is True
        self.path.parent.mkdir(parents=True,exist_ok=True)
        write_json(self.path,document)

    async def _run(self):
        while True:
            self.record()
            await asyncio.sleep(self.interval)

    async def start(self):
        if self._closed:raise RuntimeError('health_writer_closed')
        if self._task is None:
            self.record();self._task=asyncio.create_task(self._run())

    async def close(self):
        self._closed=True
        if self._task is not None:
            self._task.cancel();await asyncio.gather(self._task,return_exceptions=True);self._task=None
        self.record(stopping=True)
