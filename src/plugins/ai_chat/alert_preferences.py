"""Shared persistent alert delivery preferences; no transport imports."""
from __future__ import annotations
import threading
from src.bot_storage import StateSource, open_json_state

class AlertNotificationPreferences:
    """Persistent operator override for QQ alert delivery."""

    def __init__(self, state_source: StateSource) -> None:
        self._state = open_json_state(
            state_source,
            "alert_notification_preferences",
        )
        self._lock = threading.RLock()
        payload = self._state.load()
        stored = payload.get("enabled") if isinstance(payload, dict) else None
        self._enabled_override = stored if isinstance(stored, bool) else None

    def effective_enabled(self, configured_default: bool) -> bool:
        with self._lock:
            if self._enabled_override is None:
                return bool(configured_default)
            return self._enabled_override

    def enabled_override(self) -> bool | None:
        with self._lock:
            return self._enabled_override

    def set_enabled(self, enabled: bool) -> bool:
        normalized = bool(enabled)
        with self._lock:
            self._state.save({"version": 1, "enabled": normalized})
            self._enabled_override = normalized
        return normalized
