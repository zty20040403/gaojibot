from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import nonebot
from nonebot.adapters.onebot.v11.exception import ActionFailed

nonebot.init()

from src.plugins.ai_chat.alert_notifier import (
    ActivityAlert,
    AlertNotificationPreferences,
    AlertNotificationService,
    format_alert_notification,
    group_alert_incidents,
)


class Logger:
    def info(self, _message: object, *args: object, **kwargs: object) -> None:
        pass

    def warning(self, _message: object, *args: object, **kwargs: object) -> None:
        pass


class Bot:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.online = True

    async def call_api(self, api: str, **data: Any) -> dict[str, bool]:
        assert api == "get_status"
        return {"online": self.online, "good": True}

    async def send_group_msg(self, **data: Any) -> dict[str, int]:
        self.calls.append(data)
        return {"message_id": 1}


def alert(
    fingerprint: str,
    *,
    name: str = "TailnetLinkPacketLoss",
    severity: str = "warning",
    instance: str = "h610",
    peer: str = "tank",
    summary: str = "h610 到 tank 丢包 30%",
    starts_at: str = "2026-08-28T02:20:55.317Z",
) -> ActivityAlert:
    return ActivityAlert(
        name=name,
        severity=severity,
        instance=instance,
        peer=peer,
        summary=summary,
        description="链路连续丢包，请检查网络路径。",
        starts_at=starts_at,
        fingerprint=fingerprint,
    )


class AlertNotificationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_offline_account_defers_without_marking_alert_delivered(self) -> None:
        current: list[ActivityAlert] = []
        bot = Bot()
        with tempfile.TemporaryDirectory() as directory:
            service = AlertNotificationService(
                alertmanager_url="http://alertmanager", group_id=1,
                check_seconds=30, state_path=Path(directory) / "alerts.json",
                logger=Logger(), fetcher=lambda: _result(current),
                bot_provider=lambda: [bot],
            )
            await service.run_once()
            current.append(alert("new"))
            bot.online = False
            with patch("src.plugins.ai_chat.alert_notifier.monotonic", return_value=100):
                self.assertEqual(await service.run_once(), 0)
                self.assertEqual(service._retry_after, 130)
                self.assertEqual(await service.run_once(), 0)
            self.assertEqual(bot.calls, [])
            self.assertEqual(service._notified_incidents, {})
            bot.online = True
            with patch("src.plugins.ai_chat.alert_notifier.monotonic", return_value=131):
                self.assertEqual(await service.run_once(), 1)
            self.assertEqual(len(bot.calls), 1)
            self.assertEqual(service._send_failures, 0)

    async def test_transport_failure_backs_off_while_polling_history(self) -> None:
        current: list[ActivityAlert] = []
        bot = Bot()
        with tempfile.TemporaryDirectory() as directory:
            service = AlertNotificationService(
                alertmanager_url="http://alertmanager", group_id=1,
                check_seconds=30, state_path=Path(directory) / "alerts.json",
                logger=Logger(), fetcher=lambda: _result(current),
                bot_provider=lambda: [bot],
            )
            await service.run_once()
            current.append(alert("new"))
            with patch.object(bot, "send_group_msg", side_effect=OSError("offline")) as send:
                with patch("src.plugins.ai_chat.alert_notifier.monotonic", return_value=100):
                    await service.run_once()
                    await service.run_once()
                self.assertEqual(send.call_count, 1)
                with patch("src.plugins.ai_chat.alert_notifier.monotonic", return_value=131):
                    await service.run_once()
                self.assertEqual(service._retry_after, 191)
            self.assertEqual(service._notified_incidents, {})

    async def test_qq_network_rejection_uses_exception_info_and_defers(self) -> None:
        current: list[ActivityAlert] = []
        bot = Bot()
        with tempfile.TemporaryDirectory() as directory:
            service = AlertNotificationService(
                alertmanager_url="http://alertmanager", group_id=1,
                check_seconds=30, state_path=Path(directory) / "alerts.json",
                logger=Logger(), fetcher=lambda: _result(current),
                bot_provider=lambda: [bot],
            )
            await service.run_once()
            current.append(alert("new"))
            with patch.object(bot, "send_group_msg", side_effect=ActionFailed(retcode=1200, message="network error")):
                self.assertEqual(await service.run_once(), 0)
            self.assertIn("1200", service._delivery_blocker)
            self.assertEqual(service._send_failures, 1)
            self.assertEqual(service._notified_incidents, {})

    def test_failed_save_keeps_previous_preference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "notification-preferences.json"
            preferences = AlertNotificationPreferences(state_path)
            preferences.set_enabled(False)
            with patch.object(preferences._state, "save", side_effect=RuntimeError("storage unavailable")):
                with self.assertRaises(RuntimeError):
                    preferences.set_enabled(True)

            self.assertFalse(preferences.effective_enabled(True))
            self.assertFalse(AlertNotificationPreferences(state_path).effective_enabled(True))

    def test_notification_preference_is_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "notification-preferences.json"
            preferences = AlertNotificationPreferences(state_path)
            self.assertTrue(preferences.effective_enabled(True))
            self.assertIsNone(preferences.enabled_override())

            preferences.set_enabled(False)
            restarted = AlertNotificationPreferences(state_path)

            self.assertFalse(restarted.effective_enabled(True))
            self.assertFalse(restarted.enabled_override())

    async def test_disabled_delivery_keeps_baseline_without_replaying_backlog(self) -> None:
        current: list[ActivityAlert] = []
        enabled = False
        bot = Bot()
        with tempfile.TemporaryDirectory() as directory:
            service = AlertNotificationService(
                alertmanager_url="http://alertmanager",
                group_id=611798505,
                check_seconds=30,
                state_path=Path(directory) / "alerts.json",
                logger=Logger(),
                fetcher=lambda: _result(current),
                bot_provider=lambda: [bot],
                enabled_provider=lambda: enabled,
            )
            await service.run_once()
            current.append(alert("suppressed-while-disabled"))
            suppressed = await service.run_once()
            enabled = True
            replayed = await service.run_once()
            current.append(alert("new-after-enabled", peer="r2s"))
            delivered = await service.run_once()

        self.assertEqual((suppressed, replayed, delivered), (0, 0, 1))
        self.assertEqual(len(bot.calls), 1)

    async def test_first_poll_only_establishes_baseline(self) -> None:
        current = [alert("existing")]
        bot = Bot()
        with tempfile.TemporaryDirectory() as directory:
            service = AlertNotificationService(
                alertmanager_url="http://alertmanager",
                group_id=611798505,
                check_seconds=30,
                state_path=Path(directory) / "alerts.json",
                logger=Logger(),
                fetcher=lambda: _result(current),
                bot_provider=lambda: [bot],
            )

            delivered = await service.run_once()

        self.assertEqual(delivered, 0)
        self.assertEqual(bot.calls, [])

    async def test_new_alert_does_not_mention_all_and_is_not_repeated(self) -> None:
        current = [alert("existing")]
        bot = Bot()
        with tempfile.TemporaryDirectory() as directory:
            service = AlertNotificationService(
                alertmanager_url="http://alertmanager",
                group_id=611798505,
                check_seconds=30,
                state_path=Path(directory) / "alerts.json",
                logger=Logger(),
                fetcher=lambda: _result(current),
                bot_provider=lambda: [bot],
            )
            await service.run_once()
            current.append(
                alert(
                    "down",
                    name="HostUnreachable",
                    severity="critical",
                    instance="tank",
                    summary="tank 抓不到了",
                    starts_at="2026-08-28T03:00:00Z",
                )
            )

            delivered = await service.run_once()
            duplicate = await service.run_once()

        self.assertEqual(delivered, 1)
        self.assertEqual(duplicate, 0)
        self.assertEqual(len(bot.calls), 1)
        self.assertEqual(bot.calls[0]["group_id"], 611798505)
        message = bot.calls[0]["message"]
        self.assertTrue(all(segment.type != "at" for segment in message))
        self.assertIn("严重｜tank｜服务器寄了", str(message))
        self.assertIn("2026-08-28 11:00:00", str(message))

    async def test_persisted_state_prevents_replay_after_restart(self) -> None:
        current = [alert("existing")]
        bot = Bot()
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "alerts.json"
            first = AlertNotificationService(
                alertmanager_url="http://alertmanager",
                group_id=611798505,
                check_seconds=30,
                state_path=state_path,
                logger=Logger(),
                fetcher=lambda: _result(current),
                bot_provider=lambda: [bot],
            )
            await first.run_once()
            restarted = AlertNotificationService(
                alertmanager_url="http://alertmanager",
                group_id=611798505,
                check_seconds=30,
                state_path=state_path,
                logger=Logger(),
                fetcher=lambda: _result(current),
                bot_provider=lambda: [bot],
            )

            delivered = await restarted.run_once()

        self.assertEqual(delivered, 0)
        self.assertEqual(bot.calls, [])

    async def test_related_link_alerts_are_sent_as_one_root_incident(self) -> None:
        current: list[ActivityAlert] = []
        bot = Bot()
        with tempfile.TemporaryDirectory() as directory:
            service = AlertNotificationService(
                alertmanager_url="http://alertmanager",
                group_id=611798505,
                check_seconds=30,
                state_path=Path(directory) / "alerts.json",
                logger=Logger(),
                fetcher=lambda: _result(current),
                bot_provider=lambda: [bot],
            )
            await service.run_once()
            current.extend(
                [
                    alert("loss-a", peer="r2s"),
                    alert(
                        "loss-b",
                        name="TailscalePathDegraded",
                        instance="h310",
                        peer="r2s",
                    ),
                    alert(
                        "down",
                        name="HostUnreachable",
                        severity="critical",
                        instance="r2s",
                        peer="",
                        summary="r2s 抓不到了",
                    ),
                ]
            )

            delivered = await service.run_once()
            duplicate = await service.run_once()

        self.assertEqual(delivered, 1)
        self.assertEqual(duplicate, 0)
        self.assertEqual(len(bot.calls), 1)
        self.assertIn("严重｜r2s｜服务器寄了", str(bot.calls[0]["message"]))
        self.assertIn("合并3条", str(bot.calls[0]["message"]))

    async def test_derivative_alert_does_not_repeat_existing_incident(self) -> None:
        current: list[ActivityAlert] = []
        bot = Bot()
        with tempfile.TemporaryDirectory() as directory:
            service = AlertNotificationService(
                alertmanager_url="http://alertmanager",
                group_id=611798505,
                check_seconds=30,
                state_path=Path(directory) / "alerts.json",
                logger=Logger(),
                fetcher=lambda: _result(current),
                bot_provider=lambda: [bot],
            )
            await service.run_once()
            current.append(alert("loss-a", peer="r2s"))
            first = await service.run_once()
            current.append(
                alert(
                    "loss-b",
                    name="TailscalePathDegraded",
                    instance="h310",
                    peer="r2s",
                )
            )
            derivative = await service.run_once()

        self.assertEqual(first, 1)
        self.assertEqual(derivative, 0)
        self.assertEqual(len(bot.calls), 1)

    async def test_notified_incident_sends_one_full_recovery(self) -> None:
        current: list[ActivityAlert] = []
        bot = Bot()
        with tempfile.TemporaryDirectory() as directory:
            service = AlertNotificationService(
                alertmanager_url="http://alertmanager",
                group_id=611798505,
                check_seconds=30,
                state_path=Path(directory) / "alerts.json",
                logger=Logger(),
                fetcher=lambda: _result(current),
                bot_provider=lambda: [bot],
            )
            await service.run_once()
            current.append(alert("loss", peer="r2s"))
            await service.run_once()
            current.clear()

            recovered = await service.run_once()
            duplicate = await service.run_once()

        self.assertEqual(recovered, 1)
        self.assertEqual(duplicate, 0)
        self.assertEqual(len(bot.calls), 2)
        self.assertIn("【告警恢复】", str(bot.calls[1]["message"]))
        self.assertIn("r2s｜已恢复", str(bot.calls[1]["message"]))

    def test_grouping_marks_non_representative_events_as_suppressed(self) -> None:
        first = alert("loss-a", peer="r2s")
        second = alert(
            "down",
            name="HostUnreachable",
            severity="critical",
            instance="r2s",
            peer="",
        )

        incidents, suppressed = group_alert_incidents([first, second])

        self.assertEqual(list(incidents), ["host:r2s"])
        self.assertEqual(incidents["host:r2s"].representative, second)
        self.assertEqual(suppressed, {first.identity})

    def test_warning_notification_is_compact(self) -> None:
        text = format_alert_notification([alert("warning")])

        self.assertEqual(
            text,
            "【活动告警】\n"
            "1. 警告｜tank｜h610 到 tank 丢包 30%"
            "｜2026-08-28 10:20:55",
        )


async def _result(value: list[ActivityAlert]) -> list[ActivityAlert]:
    return list(value)


if __name__ == "__main__":
    unittest.main()
