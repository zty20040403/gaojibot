"""Deliver new Alertmanager alerts to one QQ group without LLM involvement."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from time import monotonic
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx
from nonebot import get_bots
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
from nonebot.adapters.onebot.v11.exception import ActionFailed

from src.bot_storage import StateSource, open_json_state
from .onebot_availability import delivery_blocker


class AlertLogger(Protocol):
    def info(self, message: object, *args: object, **kwargs: object) -> object: ...

    def warning(self, message: object, *args: object, **kwargs: object) -> object: ...


class GroupMessageBot(Protocol):
    async def call_api(self, api: str, **data: Any) -> Any: ...

    async def send_group_msg(self, **data: Any) -> Any: ...


@dataclass(frozen=True)
class ActivityAlert:
    name: str
    severity: str
    instance: str
    peer: str
    summary: str
    description: str
    starts_at: str
    fingerprint: str
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def identity(self) -> str:
        raw = self.fingerprint or "|".join(
            (self.name, self.instance, self.peer, self.summary)
        )
        return f"{raw}|{self.starts_at}"

    @property
    def is_server_down(self) -> bool:
        normalized = self.name.casefold().replace("_", "").replace("-", "")
        return normalized in {
            "hostdown",
            "hostunreachable",
            "instancedown",
            "nodedown",
            "serverdown",
            "targetdown",
        }


AlertFetcher = Callable[[], Awaitable[list[ActivityAlert]]]
BotProvider = Callable[[], Sequence[GroupMessageBot]]
NotificationEnabledProvider = Callable[[], bool]


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


class AlertHistoryStore(Protocol):
    def record_snapshot(
        self,
        alerts: Sequence[ActivityAlert],
        *,
        incident_keys: dict[str, str],
        suppressed_ids: set[str],
        observed_at: int | None = None,
    ) -> None: ...

    def record_notifications(
        self,
        incidents: Sequence[AlertIncident],
        *,
        kind: str,
        message: str,
        created_at: int | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class AlertIncident:
    key: str
    representative: ActivityAlert
    alerts: tuple[ActivityAlert, ...]

    @property
    def severity_rank(self) -> int:
        return _severity_rank(self.representative.severity)


@dataclass(frozen=True)
class _NotifierState:
    active: dict[str, ActivityAlert]
    notified_incidents: dict[str, int]
    legacy_active_ids: set[str] = field(default_factory=set)


class AlertNotificationService:
    def __init__(
        self,
        *,
        alertmanager_url: str,
        group_id: int,
        check_seconds: int,
        state_path: Path,
        logger: AlertLogger,
        fetcher: AlertFetcher | None = None,
        bot_provider: BotProvider | None = None,
        history_store: AlertHistoryStore | None = None,
        enabled_provider: NotificationEnabledProvider | None = None,
    ) -> None:
        self._alertmanager_url = alertmanager_url.rstrip("/")
        self._group_id = group_id
        self._check_seconds = max(check_seconds, 10)
        self._state_path = state_path
        self._logger = logger
        self._fetcher = fetcher or self._fetch_active_alerts
        self._bot_provider = bot_provider or _onebot_bots
        self._history_store = history_store
        self._enabled_provider = enabled_provider or (lambda: True)
        self._loaded = False
        self._seen_alerts: dict[str, ActivityAlert] = {}
        self._notified_incidents: dict[str, int] = {}
        self._retry_after = 0.0
        self._send_failures = 0
        self._delivery_blocker: str | None = None

    def _defer_send(self, reason: str) -> None:
        self._send_failures += 1
        delay = min(300, self._check_seconds * 2 ** min(self._send_failures - 1, 5))
        self._retry_after = monotonic() + delay
        if self._delivery_blocker != reason:
            self._logger.warning(f"Alert delivery deferred: {reason}")
        self._delivery_blocker = reason

    async def run_forever(self) -> None:
        while True:
            await self.run_once()
            await asyncio.sleep(self._check_seconds)

    async def run_once(self) -> int:
        try:
            alerts = await self._fetcher()
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, RuntimeError, TypeError, ValueError) as exc:
            self._logger.warning(f"Alertmanager notification poll failed: {exc}")
            return 0

        current_alerts = {alert.identity: alert for alert in alerts}
        if not self._loaded:
            stored = await asyncio.to_thread(self._read_state)
            self._loaded = True
            if stored is None:
                self._seen_alerts = current_alerts
                await self._record_history(alerts)
                await asyncio.to_thread(self._write_state)
                self._logger.info(
                    "Alert notifier established its initial active-alert baseline "
                    f"with {len(current_alerts)} alert(s)."
                )
                return 0
            self._seen_alerts = dict(stored.active)
            self._notified_incidents = dict(stored.notified_incidents)
            for identity in stored.legacy_active_ids:
                if identity in current_alerts:
                    self._seen_alerts[identity] = current_alerts[identity]

        previous_incidents, _ = group_alert_incidents(self._seen_alerts.values())
        current_incidents, _ = group_alert_incidents(alerts)
        await self._record_history(alerts)
        if not self.notifications_enabled:
            previous_alerts = self._seen_alerts
            previous_notified = self._notified_incidents
            retained_notified = {
                key: severity
                for key, severity in previous_notified.items()
                if key in current_incidents
            }
            self._seen_alerts = current_alerts
            self._notified_incidents = retained_notified
            if (
                current_alerts != previous_alerts
                or retained_notified != previous_notified
            ):
                await asyncio.to_thread(self._write_state)
            return 0
        firing: list[AlertIncident] = []
        escalations: list[AlertIncident] = []
        for key, incident in current_incidents.items():
            previous = previous_incidents.get(key)
            if previous is None:
                firing.append(incident)
            elif incident.severity_rank > previous.severity_rank:
                escalations.append(incident)
        recoveries = [
            incident
            for key, incident in previous_incidents.items()
            if key not in current_incidents and key in self._notified_incidents
        ]

        if not firing and not escalations and not recoveries:
            if current_alerts != self._seen_alerts:
                self._seen_alerts = current_alerts
                await asyncio.to_thread(self._write_state)
            return 0

        if monotonic() < self._retry_after:
            return 0

        bots = list(self._bot_provider())
        if not bots:
            self._defer_send("No OneBot connection is available.")
            return 0

        selected_bot = None
        blocker = "QQ status unavailable"
        for bot in bots:
            blocker = await delivery_blocker(bot, pending_notice="告警等待恢复后发送")
            if blocker is None:
                selected_bot = bot
                break
        if selected_bot is None:
            self._defer_send(blocker or "QQ status unavailable")
            return 0

        notification_text = format_incident_notification(
            [*firing, *escalations],
            recoveries,
        )
        message = Message([MessageSegment.text(notification_text)])
        try:
            await selected_bot.send_group_msg(
                group_id=self._group_id,
                message=message,
            )
        except ActionFailed as exc:
            if "timeout" not in str(exc).casefold():
                self._defer_send(f"QQ rejected alert delivery (retcode={exc.info.get('retcode')}).")
                return 0
            self._logger.warning(
                "Activity alert notification receipt timed out; treating the "
                "outcome as delivered to avoid duplicate alert messages."
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._defer_send(f"Alert transport error: {type(exc).__name__}.")
            return 0

        self._retry_after = 0.0
        self._send_failures = 0
        self._delivery_blocker = None
        for incident in (*firing, *escalations):
            self._notified_incidents[incident.key] = incident.severity_rank
        for incident in recoveries:
            self._notified_incidents.pop(incident.key, None)
        self._seen_alerts = current_alerts
        await asyncio.to_thread(self._write_state)
        await self._record_notifications(firing, "firing", notification_text)
        await self._record_notifications(
            escalations, "escalation", notification_text
        )
        await self._record_notifications(recoveries, "recovery", notification_text)
        delivered = len(firing) + len(escalations) + len(recoveries)
        self._logger.info(
            f"Delivered {delivered} alert incident transition(s) to QQ group "
            f"{self._group_id}."
        )
        return delivered

    @property
    def notifications_enabled(self) -> bool:
        try:
            return bool(self._enabled_provider())
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._logger.warning(
                f"Alert notification preference could not be read: {exc}"
            )
            return False

    async def _fetch_active_alerts(self) -> list[ActivityAlert]:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{self._alertmanager_url}/api/v2/alerts",
                params={
                    "active": "true",
                    "silenced": "false",
                    "inhibited": "false",
                },
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError("Alertmanager returned a non-list response")
        return [
            _parse_alert(item)
            for item in payload
            if isinstance(item, dict)
        ]

    async def _record_history(self, alerts: Sequence[ActivityAlert]) -> None:
        if self._history_store is None:
            return
        incidents, suppressed_ids = group_alert_incidents(alerts)
        incident_keys = {
            alert.identity: incident.key
            for incident in incidents.values()
            for alert in incident.alerts
        }
        try:
            await asyncio.to_thread(
                self._history_store.record_snapshot,
                alerts,
                incident_keys=incident_keys,
                suppressed_ids=suppressed_ids,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._logger.warning(f"Alert event history could not be updated: {exc}")

    async def _record_notifications(
        self,
        incidents: Sequence[AlertIncident],
        kind: str,
        message: str,
    ) -> None:
        if self._history_store is None or not incidents:
            return
        try:
            await asyncio.to_thread(
                self._history_store.record_notifications,
                incidents,
                kind=kind,
                message=message,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._logger.warning(
                f"Alert notification history could not be updated: {exc}"
            )

    def _read_state(self) -> _NotifierState | None:
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            self._logger.warning(f"Alert notifier state could not be read: {exc}")
            return None
        if not isinstance(payload, dict):
            return None
        active_payload = payload.get("active_alerts", [])
        active: dict[str, ActivityAlert] = {}
        if isinstance(active_payload, list):
            for item in active_payload:
                alert = _state_alert(item)
                if alert is not None:
                    active[alert.identity] = alert
        notified_payload = payload.get("notified_incidents", {})
        notified = (
            {
                str(key): max(int(value), 1)
                for key, value in notified_payload.items()
                if str(key)
            }
            if isinstance(notified_payload, dict)
            else {}
        )
        legacy_payload = payload.get("active_alert_ids", [])
        legacy = (
            {str(value) for value in legacy_payload if str(value)}
            if isinstance(legacy_payload, list)
            else set()
        )
        return _NotifierState(active, notified, legacy)

    def _write_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "version": 2,
                    "active_alerts": [
                        asdict(self._seen_alerts[key])
                        for key in sorted(self._seen_alerts)
                    ],
                    "notified_incidents": dict(
                        sorted(self._notified_incidents.items())
                    ),
                },
                ensure_ascii=True,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(self._state_path)


def format_alert_notification(alerts: Sequence[ActivityAlert]) -> str:
    incidents, _ = group_alert_incidents(alerts)
    return format_incident_notification(list(incidents.values()), [])


def format_incident_notification(
    firing: Sequence[AlertIncident],
    recoveries: Sequence[AlertIncident],
) -> str:
    ordered = sorted(
        firing,
        key=lambda incident: (
            incident.severity_rank,
            incident.representative.starts_at,
        ),
        reverse=True,
    )
    highest = max((incident.severity_rank for incident in ordered), default=0)
    if highest >= 3:
        heading = "【紧急告警】"
    elif highest == 2:
        heading = "【活动告警】"
    else:
        heading = "【监控提示】"

    lines: list[str] = [heading] if ordered else []
    for index, incident in enumerate(ordered, start=1):
        alert = incident.representative
        level = _severity_label(alert.severity)
        if alert.is_server_down and alert.instance:
            target = alert.instance
            problem = "服务器寄了"
        else:
            target = _incident_target(incident)
            problem = (_compact(alert.summary) or alert.name)[:120]
        merged = f"｜合并{len(incident.alerts)}条" if len(incident.alerts) > 1 else ""
        lines.append(
            f"{index}. {level}｜{target}｜{problem}｜{_format_time(alert.starts_at)}"
            f"{merged}"
        )
    if recoveries:
        if lines:
            lines.append("")
        lines.append("【告警恢复】")
        for index, incident in enumerate(recoveries, start=1):
            lines.append(
                f"{index}. {_incident_target(incident)}｜已恢复｜"
                f"{_format_time(datetime.now(tz=ZoneInfo('Asia/Shanghai')).isoformat())}"
            )
    return "\n".join(lines)


def group_alert_incidents(
    alerts: Iterable[ActivityAlert],
) -> tuple[dict[str, AlertIncident], set[str]]:
    grouped: dict[str, list[ActivityAlert]] = {}
    for alert in alerts:
        grouped.setdefault(_incident_key(alert), []).append(alert)
    incidents: dict[str, AlertIncident] = {}
    suppressed: set[str] = set()
    for key, members in grouped.items():
        representative = max(
            members,
            key=lambda alert: (
                _severity_rank(alert.severity),
                int(alert.is_server_down),
                alert.starts_at,
            ),
        )
        ordered = tuple(
            sorted(
                members,
                key=lambda alert: (
                    _severity_rank(alert.severity),
                    alert.starts_at,
                ),
                reverse=True,
            )
        )
        incidents[key] = AlertIncident(key, representative, ordered)
        suppressed.update(
            alert.identity for alert in ordered if alert.identity != representative.identity
        )
    return incidents, suppressed


def _parse_alert(raw: dict[str, Any]) -> ActivityAlert:
    labels = raw.get("labels") if isinstance(raw.get("labels"), dict) else {}
    annotations = (
        raw.get("annotations") if isinstance(raw.get("annotations"), dict) else {}
    )
    return ActivityAlert(
        name=str(labels.get("alertname") or "Alert")[:160],
        severity=str(labels.get("severity") or "warning")[:32],
        instance=str(labels.get("instance") or labels.get("host") or "")[:160],
        peer=str(labels.get("peer") or "")[:160],
        summary=str(annotations.get("summary") or "")[:500],
        description=str(annotations.get("description") or "")[:1000],
        starts_at=str(raw.get("startsAt") or "")[:64],
        fingerprint=str(raw.get("fingerprint") or _fallback_fingerprint(raw))[:128],
        labels={
            str(key)[:120]: str(value)[:300]
            for key, value in labels.items()
        },
    )


def _incident_key(alert: ActivityAlert) -> str:
    normalized = alert.name.casefold().replace("_", "").replace("-", "")
    if alert.is_server_down or normalized in {"hostrebooted", "noderebooted"}:
        return f"host:{alert.instance or alert.peer or 'unknown'}"
    if normalized in {
        "tailnetlinkpacketloss",
        "tailscalepathdegraded",
        "linkpacketloss",
        "networkpathdegraded",
    }:
        return f"host:{alert.peer or alert.instance or 'unknown'}"
    if normalized in {"systemdunitfailed", "servicefailed"}:
        unit = alert.labels.get("name") or alert.labels.get("unit") or alert.name
        return f"service:{alert.instance or 'unknown'}:{unit}"
    return ":".join(
        ("alert", alert.name, alert.instance or "unknown", alert.peer)
    )[:500]


def _incident_target(incident: AlertIncident) -> str:
    if incident.key.startswith("host:"):
        return incident.key.removeprefix("host:")
    alert = incident.representative
    return (
        " → ".join(value for value in (alert.instance, alert.peer) if value)
        or alert.name
    )


def _state_alert(value: object) -> ActivityAlert | None:
    if not isinstance(value, dict):
        return None
    try:
        labels = value.get("labels", {})
        return ActivityAlert(
            name=str(value.get("name") or "Alert")[:160],
            severity=str(value.get("severity") or "warning")[:32],
            instance=str(value.get("instance") or "")[:160],
            peer=str(value.get("peer") or "")[:160],
            summary=str(value.get("summary") or "")[:500],
            description=str(value.get("description") or "")[:1000],
            starts_at=str(value.get("starts_at") or "")[:64],
            fingerprint=str(value.get("fingerprint") or "")[:128],
            labels=(
                {str(key): str(item) for key, item in labels.items()}
                if isinstance(labels, dict)
                else {}
            ),
        )
    except (TypeError, ValueError):
        return None


def _severity_rank(severity: str) -> int:
    normalized = severity.casefold()
    if normalized in {"critical", "emergency", "fatal", "page"}:
        return 3
    if normalized in {"warning", "warn"}:
        return 2
    return 1


def _severity_label(severity: str) -> str:
    rank = _severity_rank(severity)
    if rank == 3:
        return "严重"
    if rank == 2:
        return "警告"
    return "提示"


def _format_time(value: str) -> str:
    if not value:
        return "时间未知"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(ZoneInfo("Asia/Shanghai")).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except ValueError:
        return value


def _compact(value: str) -> str:
    return " ".join(value.replace("`", "").split())


def _fallback_fingerprint(raw: dict[str, Any]) -> str:
    payload = json.dumps(raw, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _onebot_bots() -> list[Bot]:
    return [bot for bot in get_bots().values() if isinstance(bot, Bot)]
