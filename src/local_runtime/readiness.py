"""Bounded, read-only heartbeat inspection for desktop diagnostics."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import stat
import time


def official_connection(directory: Path) -> str:
    filename = directory / "state/desktop-health.json"
    try:
        descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 4096:
                return "unavailable"
            record = json.loads(stream.read(4097))
        stamp = record.get("updated_at")
        if type(stamp) not in (int, float) or not math.isfinite(stamp):
            return "unavailable"
        if not 0 <= time.time() - stamp <= 15:
            return "stale"
        value = record.get("official")
        if not isinstance(value, dict) or any(type(value.get(key)) is not bool for key in ("healthy", "lease_current", "gateway_ready")):
            return "unavailable"
        return "connected" if all(value[key] for key in ("healthy", "lease_current", "gateway_ready")) else "disconnected"
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return "unavailable"


def connection_status(directory: Path, config: dict, services: list[dict]) -> str:
    if not any(row.get("name") == "bot" and row.get("state") == "running" for row in services):
        return "not_running"
    selected = config["settings"].get("platforms", {"onebot": True, "qq_official": False})
    if selected["qq_official"]:
        official = official_connection(directory)
        if official != "connected" or not selected["onebot"]:
            return official
    filename = directory / "state/desktop-health.json"
    try:
        descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 4096:
                return "unavailable"
            record = json.loads(stream.read(4097))
        if not isinstance(record, dict):
            return "unavailable"
        timestamp = record.get("updated_at")
        if type(timestamp) not in (int, float):
            return "unavailable"
        if not 0 <= time.time() - timestamp <= 15:
            return "stale"
        if record.get("qq") != config["settings"]["bot_qq"]:
            return "wrong_account"
        if type(record.get("connected")) is not bool:
            return "unavailable"
        return "connected" if record["connected"] else "disconnected"
    except (OSError, ValueError, KeyError, TypeError):
        return "unavailable"
