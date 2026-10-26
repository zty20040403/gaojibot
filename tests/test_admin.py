from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
import nonebot
from fastapi import FastAPI

nonebot.init()

from src.plugins.ai_chat.admin import (
    AdminEventBroker,
    AdminServices,
    _changed_database_resources,
)
from tests.admin_session_fixture import ApprovedClient, register_admin
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.alert_notifier import AlertNotificationPreferences
from src.plugins.ai_chat.model_catalog import ModelCatalog
from src.plugins.ai_chat.quota import UsageStore
from src.plugins.ai_chat.tool_policy import configure_tool_overrides


class EmptyTasks:
    def list_all(self):
        return []

    def cancel_any(self, _task_id):
        return None


class BackgroundTasks:
    def running(self):
        return ("delivery",)

    def failures(self):
        return {"historian": "upstream timeout"}


class SandboxManager:
    def __init__(self):
        self.started = []
        self.stopped = []
        self.destroyed = []

    async def admin_snapshot(self):
        return {
            "items": [
                {
                    "sandbox_id": "s123abc",
                    "owner": "group:930690526:user:3526452465",
                    "activities": [
                        {
                            "activity_id": "exec-1",
                            "command": "python main.py",
                            "elapsed_seconds": 2,
                        }
                    ],
                    "status": "Up 2 minutes",
                    "running": True,
                },
                {
                    "sandbox_id": "s456def",
                    "owner": "group:930690526:user:3526452465",
                    "activities": [],
                    "status": "Exited (0) 1 minute ago",
                    "running": False,
                    "purpose": "task",
                    "workspace_size_bytes": 1024,
                    "workspace_file_count": 1,
                },
            ],
            "active_commands": 1,
        }

    async def start_owned(self, owner, sandbox_id):
        self.started.append((owner, sandbox_id))

    async def stop_owned(self, owner, sandbox_id):
        self.stopped.append((owner, sandbox_id))

    async def destroy(self, owner, sandbox_id):
        self.destroyed.append((owner, sandbox_id))


class ModelPreferences:
    def __init__(self):
        self.models = {"group:930690526:user:3526452465": "main"}

    def items(self):
        return sorted(self.models.items())

    def get_group_default(self, group_id):
        return self.models.get(f"group:{group_id}:default")

    def get_group_member_default(self, group_id):
        return self.models.get(f"group:{group_id}:member-default")

    def set_group_member_default(self, group_id, value):
        self.models[f"group:{group_id}:member-default"] = value

    def clear_group_member_default(self, group_id):
        return self.clear(f"group:{group_id}:member-default")

    def set_group_default(self, group_id, profile):
        self.models[f"group:{group_id}:default"] = profile

    def clear_group_default(self, group_id):
        return self.clear(f"group:{group_id}:default")

    def get_group_enabled_override(self, group_id):
        stored = self.models.get(f"group:{group_id}:enabled")
        if stored == "enabled":
            return True
        if stored == "disabled":
            return False
        return None

    def set_group_enabled(self, group_id, enabled):
        self.models[f"group:{group_id}:enabled"] = (
            "enabled" if enabled else "disabled"
        )

    def get_group_vision_auto_describe_override(self, group_id):
        stored = self.models.get(f"group:{group_id}:vision-auto-describe")
        if stored == "enabled":
            return True
        if stored == "disabled":
            return False
        return None

    def set_group_vision_auto_describe(self, group_id, enabled):
        self.models[f"group:{group_id}:vision-auto-describe"] = (
            "enabled" if enabled else "disabled"
        )

    def set(self, conversation_id, profile):
        self.models[conversation_id] = profile

    def get_explicit(self, conversation_id):
        return self.models.get(conversation_id)

    def clear(self, conversation_id):
        return self.models.pop(conversation_id, None) is not None


class UserProfiles:
    def group_ids(self):
        return (930690526,)

    def members(self, group_id):
        if group_id != 930690526:
            return []
        return [
            {
                "user_id": 3526452465,
                "display_name": "Kenneth",
                "nickname": "Kenneth",
                "card": "",
                "last_seen": 200,
            },
            {
                "user_id": 2291939848,
                "display_name": "群友",
                "nickname": "群友",
                "card": "",
                "last_seen": 100,
            },
        ]


class MessageLedger:
    def list_scopes(self):
        return [
            SimpleNamespace(
                platform="onebot-v11",
                kind="group",
                native_conversation_id="930690526",
            )
        ]

    def visible_messages_by_ids(self, scope, message_ids):
        if scope.key != "onebot-v11:group:930690526":
            return []
        messages = {
            10: SimpleNamespace(
                canonical_message_id=10,
                native_message_id="native-10",
                sender_native_user_id="2291939848",
                sender_principal_id=2,
                sender_display="群友",
                direction="inbound",
                prompt_text="这个部署方案怎么样？",
                occurred_at=120,
                reply_to_canonical_message_id=None,
            ),
            12: SimpleNamespace(
                canonical_message_id=12,
                native_message_id="native-12",
                sender_native_user_id="3526452465",
                sender_principal_id=3,
                sender_display="Kenneth",
                direction="inbound",
                prompt_text="你觉得呢",
                occurred_at=123,
                reply_to_canonical_message_id=None,
            ),
        }
        return [messages[item] for item in message_ids if item in messages]


class TurnJournal:
    def __init__(self):
        self.feedback = None

    def recent_context_plans(self, limit=100, turn_id=None, negative_only=False):
        del limit
        if turn_id is not None and turn_id != 3:
            return []
        if negative_only and self.feedback is None:
            return []
        return [
            {
                "turn_id": 3,
                "votes": {"asker": 1, "others_up": 2, "others_down": 0},
                "turn_handle": "t#3",
                "scope_key": "onebot-v11:group:930690526",
                "current_message_id": 12,
                "focus_message_id": 10,
                "confidence": 0.87,
                "reason_codes": ["recent_question", "same_scope"],
                "related_message_ids": [11],
                "candidates": [{"message_id": 10, "score": 89.0}],
                "recall_route": {"mode": "follow_up", "confidence": 0.95},
                "adaptive_budget": {
                    "focus": 330,
                    "used": {"focus": 72, "total": 72},
                },
                "evidence_guard": {"sufficient": True, "confidence": 0.87},
                "topic_id": 10,
                "topic_message_ids": [10],
                "topic_query": "这个部署方案",
                "recall_candidates": [
                    {
                        "handle": "msg#10",
                        "source": "relation_graph",
                        "selected": True,
                        "raw_score": 0.91,
                        "adjusted_score": 0.91,
                        "decision_codes": ["selected"],
                        "evidence_ids": [10],
                    }
                ],
                "resolver_version": "reference-rules-v1",
                "context_hash": "abc123",
                "objective": "你觉得呢",
                "status": "succeeded",
                "model": "model-a",
                "profile": "main",
                "feedback": self.feedback,
                "created_at": 123,
            }
        ]

    def set_context_feedback(
        self,
        turn_id,
        *,
        verdict,
        note,
        actor,
        resource_version,
    ):
        self.feedback = {
            "turn_id": turn_id,
            "verdict": verdict,
            "note": note,
            "actor": actor,
            "resource_version": resource_version,
            "updated_at": 130,
        }
        return dict(self.feedback)

    def recent_trace_summaries(self, limit=50):
        del limit
        return [
            {
                "trace_id": "a" * 32,
                "turn_id": 3,
                "turn_handle": "t#3",
                "status": "succeeded",
                "provider": "test",
                "model": "model-a",
                "profile": "main",
                "started_at": 123,
                "finished_at": 125,
                "duration_seconds": 2,
                "tool_call_count": 1,
                "tool_failures": 0,
                "tools": ["web_search"],
                "input_tokens": 20,
                "output_tokens": 10,
                "total_tokens": 30,
                "model_routes": [],
                "fallback": False,
            }
        ]


class Telemetry:
    def admin_snapshot(self, **_kwargs):
        return {
            "generated_at": 123,
            "window": "process",
            "totals": {"turns": 1, "model_requests": 1},
            "models": [],
            "tools": [],
            "deliveries": [],
            "stages": [],
            "fallback_routes": [],
        }


class AlertHistory:
    def snapshot(self, *, days=1, limit=200):
        return {
            "configured": True,
            "available": True,
            "range_start": 100,
            "generated_at": 200,
            "summary": {
                "current_active": 2,
                "current_incidents": 1,
                "triggered": 9,
                "resolved": 7,
                "incidents": 3,
                "firing_notifications": 3,
                "recovery_notifications": 2,
            },
            "events": [
                {
                    "event_id": 1,
                    "name": "HostUnreachable",
                    "incident_key": "host:r2s",
                    "status": "firing",
                }
            ][:limit],
            "incidents": [
                {
                    "incident_key": "host:r2s",
                    "status": "firing",
                    "event_count": 2,
                }
            ],
            "notifications": [],
            "days": days,
        }


class Settings:
    enabled_groups = {930690526}
    disabled_groups = {201644592}
    group_model_profiles = {930690526: "main"}
    admin_user_ids = {3526452465}
    vision_auto_describe = True
    prometheus_url = ""
    alertmanager_url = ""

    @staticmethod
    def is_group_enabled(group_id):
        return group_id != 201644592


class Database:
    def topology_snapshot(self):
        return {
            "available": True,
            "overall": "healthy",
            "checked_at": 123,
            "writable_node": "h610",
            "nodes": [
                {
                    "name": "h610",
                    "host": "100.64.0.3",
                    "port": 55432,
                    "status": "online",
                    "role": "primary",
                    "writable": True,
                    "latency_ms": 4.2,
                    "database_size_bytes": 1024,
                    "replication_lag_seconds": 0,
                    "replication_lag_bytes": 0,
                    "server_version": "17.5",
                    "error": None,
                },
                {
                    "name": "tank",
                    "host": "100.64.0.4",
                    "port": 55432,
                    "status": "online",
                    "role": "secondary",
                    "writable": False,
                    "latency_ms": 8.1,
                    "database_size_bytes": 1024,
                    "replication_lag_seconds": 0,
                    "replication_lag_bytes": 0,
                    "server_version": "17.5",
                    "error": None,
                },
            ],
            "pool": {
                "size": 2,
                "available": 1,
                "waiting": 0,
                "min_size": 1,
                "max_size": 10,
            },
        }


class FleetClient:
    async def fleet(self):
        return {
            "ok": True,
            "status": "fresh",
            "data": {"hosts": [{"host": "h610"}]},
            "inventory": [{"host_id": "h610", "role": "control"}],
        }

    async def backends(self):
        return {"items": [{"backend_name": "ops", "state": "online"}]}

    async def capabilities(self):
        return {"capabilities": [{"name": "fleet.read", "available": True}]}

    async def observations(self, *, limit=50):
        return {"items": [{"observation_id": 1}][:limit]}

    async def host(self, host_id):
        return {"host": host_id, "status": "fresh"}

    async def unit(self, host_id, unit):
        return {"host": host_id, "unit": unit, "status": "fresh"}

    async def logs(self, host_id, unit, *, lines=50, since_seconds=3600):
        return {
            "host": host_id,
            "unit": unit,
            "lines": lines,
            "since_seconds": since_seconds,
            "status": "fresh",
        }


class AdminTests(unittest.TestCase):
    def test_admin_event_broker_coalesces_resource_names(self) -> None:
        broker = AdminEventBroker()
        queue = broker.subscribe()
        broker.publish("groups", "overview", "groups")
        event = queue.get_nowait()
        broker.unsubscribe(queue)

        self.assertEqual(event["type"], "resources.changed")
        self.assertEqual(event["sequence"], 1)
        self.assertEqual(event["resources"], ["groups", "overview"])

    def test_runtime_event_omits_control_versions(self) -> None:
        broker = AdminEventBroker(lambda: {"subagents": 7})
        queue = broker.subscribe()
        broker.publish_runtime("subagents", "tasks")
        event = queue.get_nowait()
        broker.unsubscribe(queue)

        self.assertEqual(event["resources"], ["subagents", "tasks"])
        self.assertNotIn("versions", event)

    def test_database_change_feed_maps_only_changed_resources(self) -> None:
        self.assertEqual(
            _changed_database_resources({"turn_reaction_votes": (1, 0, 0)},
                                        {"turn_reaction_votes": (1, 1, 0)}),
            {"context-debug"},
        )
        previous = {
            "usage_events": (3, 0, 0),
            "vision_jobs": (4, 2, 0),
        }
        current = {
            "usage_events": (4, 0, 0),
            "vision_jobs": (4, 2, 0),
        }

        self.assertEqual(
            _changed_database_resources(previous, current),
            {"usage", "overview", "observability"},
        )

    def test_task_evidence_changes_refresh_task_details(self) -> None:
        self.assertEqual(
            _changed_database_resources(
                {"subagent_evidence": (1, 0, 0)},
                {"subagent_evidence": (2, 0, 0)},
            ),
            {"subagents", "tasks"},
        )

    def test_alert_notification_control_is_independent_and_versioned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            preferences = AlertNotificationPreferences(
                Path(directory) / "alert-preferences.json"
            )
            settings = SimpleNamespace(
                alert_notify_enabled=False,
                alert_notify_group_id=611798505,
                alert_notify_check_seconds=30,
                alertmanager_url="http://alertmanager",
            )
            app = FastAPI()
            register_admin(
                app,
                AdminServices(
                    version="test",
                    started_at=1,
                    settings=settings,
                    alert_preferences=preferences,
                ),
                token="secret",
            )

            async def run():
                transport = httpx.ASGITransport(app=app)
                headers = {"X-Test-Login": "admin"}
                async with ApprovedClient(
                    transport=transport,
                    base_url="http://test",
                ) as client:
                    before = await client.get(
                        "/bot-admin/api/alerts",
                        headers=headers,
                    )
                    changed = await client.put(
                        "/bot-admin/api/alert-notifications/control",
                        headers=headers,
                        json={"enabled": True},
                    )
                    after = await client.get(
                        "/bot-admin/api/alerts",
                        headers=headers,
                    )
                    return before, changed, after

            before, changed, after = asyncio.run(run())

        self.assertFalse(before.json()["notification_control"]["enabled"])
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json()["resource"], "alerts")
        self.assertEqual(changed.json()["resource_version"], 1)
        self.assertTrue(after.json()["notification_control"]["enabled"])
        self.assertTrue(
            after.json()["notification_control"]["enabled_override"]
        )

    def test_dashboard_api_requires_account_and_returns_runtime_state(self) -> None:
        deliveries = DeliveryStore(":memory:")
        usage = UsageStore(":memory:")
        models = ModelCatalog.from_json(
            json.dumps(
                {
                    "main": {
                        "provider": "test",
                        "model": "model-a",
                        "api_key_env": "TEST_MODEL_KEY",
                    }
                }
            ),
            default_profile="main",
            environ={"TEST_MODEL_KEY": "never-return-this-secret"},
        )
        self.addCleanup(deliveries.close)
        self.addCleanup(usage.close)
        app = FastAPI()
        sandbox_manager = SandboxManager()
        register_admin(
            app,
            AdminServices(
                version="test",
                started_at=1,
                delivery_store=deliveries,
                usage_store=usage,
                running_tasks=EmptyTasks(),
                background_tasks=BackgroundTasks(),
                model_catalog=models,
                model_preferences=ModelPreferences(),
                user_profiles=UserProfiles(),
                message_ledger=MessageLedger(),
                settings=Settings(),
                sandbox_manager=sandbox_manager,
                sticker_inventory=lambda: {
                    "counts": {"total": 1, "learned_images": 1},
                    "items": [
                        {
                            "inventory_id": "learned-1",
                            "source": "learned",
                            "kind": "image",
                            "name": "sticker.png",
                            "reference": "https://example.test/sticker.png",
                            "size_bytes": None,
                        }
                    ],
                },
                media_library=SimpleNamespace(
                    admin_snapshot=lambda limit=100: {
                        "counts": {"total": 1, "bytes": 123},
                        "items": [{"media_id": 1, "summary": "测试图片"}],
                        "jobs": [],
                        "vision_profile": "gpt-5.6-luna",
                        "root": "/var/lib/gaoji/media",
                    }
                ),
                source_store=SimpleNamespace(
                    admin_snapshot=lambda limit=100: {
                        "counts": {"total": 1, "ready": 1, "failed": 0},
                        "platforms": [{"platform": "bilibili", "total": 1}],
                        "items": [
                            {
                                "source_id": 3,
                                "platform": "bilibili",
                                "content_kind": "video",
                                "title": "测试视频",
                                "status": "ready",
                            }
                        ],
                    }
                ),
                turn_journal=TurnJournal(),
                database=Database(),
                telemetry=Telemetry(),
                alert_store=AlertHistory(),
                fleet_client=FleetClient(),
            ),
            token="secret",
        )

        async def run():
            transport = httpx.ASGITransport(app=app)
            async with ApprovedClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                page = await client.get("/bot-admin")
                favicon = await client.get("/bot-admin/favicon.svg")
                denied = await client.get("/bot-admin/api/overview")
                allowed = await client.get(
                    "/bot-admin/api/overview",
                    headers={"X-Test-Login": "admin"},
                )
                v1_allowed = await client.get(
                    "/bot-admin/api/v1/overview",
                    headers={"X-Test-Login": "admin"},
                )
                sandboxes = await client.get(
                    "/bot-admin/api/sandboxes",
                    headers={"X-Test-Login": "admin"},
                )
                sandbox_start = await client.post(
                    "/bot-admin/api/v1/sandboxes/s456def/action",
                    headers={
                        "X-Test-Login": "admin",
                        "If-Match": '"0"',
                        "X-Admin-Actor": "Kenneth",
                    },
                    json={"action": "start"},
                )
                sandbox_destroy_denied = await client.post(
                    "/bot-admin/api/v1/sandboxes/s456def/action",
                    headers={
                        "X-Test-Login": "admin",
                        "If-Match": '"0"',
                    },
                    json={"action": "destroy"},
                )
                sandbox_destroy = await client.post(
                    "/bot-admin/api/v1/sandboxes/s456def/action",
                    headers={
                        "X-Test-Login": "admin",
                        "If-Match": '"1"',
                        "X-Admin-Actor": "Kenneth",
                    },
                    json={"action": "destroy"},
                )
                stickers = await client.get(
                    "/bot-admin/api/stickers",
                    headers={"X-Test-Login": "admin"},
                )
                group_models = await client.get(
                    "/bot-admin/api/group-models",
                    headers={"X-Test-Login": "admin"},
                )
                media = await client.get(
                    "/bot-admin/api/media",
                    headers={"X-Test-Login": "admin"},
                )
                sources = await client.get(
                    "/bot-admin/api/sources",
                    headers={"X-Test-Login": "admin"},
                )
                context_plans = await client.get(
                    "/bot-admin/api/context-plans",
                    headers={"X-Test-Login": "admin"},
                )
                context_debug = await client.get(
                    "/bot-admin/api/v1/context-debug",
                    headers={"X-Test-Login": "admin"},
                )
                context_detail = await client.get(
                    "/bot-admin/api/v1/context-debug/3",
                    headers={"X-Test-Login": "admin"},
                )
                context_feedback = await client.put(
                    "/bot-admin/api/v1/context-debug/3/feedback",
                    headers={
                        "X-Test-Login": "admin",
                        "If-Match": '"0"',
                        "X-Admin-Actor": "Kenneth",
                    },
                    json={"verdict": "off_topic", "note": "应该关联上一条"},
                )
                negative_feedback = await client.get(
                    "/bot-admin/api/v1/context-debug?negative_only=true",
                    headers={"X-Test-Login": "admin"},
                )
                self.assertEqual(negative_feedback.status_code, 200)
                self.assertEqual(negative_feedback.json()["items"][0]["votes"]["others_up"], 2)
                self.assertEqual(context_detail.json()["item"]["votes"]["asker"], 1)
                databases = await client.get(
                    "/bot-admin/api/databases",
                    headers={"X-Test-Login": "admin"},
                )
                fleet = await client.get(
                    "/bot-admin/api/v1/fleet",
                    headers={"X-Test-Login": "admin"},
                )
                observability = await client.get(
                    "/bot-admin/api/observability",
                    headers={"X-Test-Login": "admin"},
                )
                alerts = await client.get(
                    "/bot-admin/api/v1/alerts?days=30&limit=50",
                    headers={"X-Test-Login": "admin"},
                )
                return (
                    page,
                    favicon,
                    denied,
                    allowed,
                    v1_allowed,
                    sandboxes,
                    sandbox_start,
                    sandbox_destroy_denied,
                    sandbox_destroy,
                    stickers,
                    group_models,
                    media,
                    sources,
                    context_plans,
                    context_debug,
                    context_detail,
                    context_feedback,
                    databases,
                    fleet,
                    observability,
                    alerts,
                )

        (
            page,
            favicon,
            denied,
            allowed,
            v1_allowed,
            sandboxes,
            sandbox_start,
            sandbox_destroy_denied,
            sandbox_destroy,
            stickers,
            group_models,
            media,
            sources,
            context_plans,
            context_debug,
            context_detail,
            context_feedback,
            databases,
            fleet,
            observability,
            alerts,
        ) = asyncio.run(run())
        self.assertEqual(page.status_code, 200)
        self.assertIn("window.__GAOJI_ADMIN__", page.text)
        self.assertIn('"apiBase":"/bot-admin/api/v1"', page.text)
        self.assertIn('id="root"', page.text)
        self.assertEqual(favicon.status_code, 200)
        self.assertEqual(fleet.status_code, 200)
        self.assertEqual(fleet.json()["fleet"]["status"], "fresh")
        self.assertTrue(favicon.headers["content-type"].startswith("image/svg+xml"))
        self.assertIn("<svg", favicon.text)
        self.assertIn("#22c55e", favicon.text)
        self.assertIn("gaoji", page.text)
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(v1_allowed.status_code, 200)
        self.assertEqual(v1_allowed.json()["version"], "test")
        self.assertEqual(allowed.json()["version"], "test")
        self.assertEqual(
            allowed.json()["background_tasks"],
            {
                "running": ["delivery"],
                "failures": {"historian": "upstream timeout"},
            },
        )
        self.assertEqual(allowed.json()["models"]["default"], "main")
        self.assertTrue(allowed.json()["models"]["profiles"][0]["configured"])
        self.assertNotIn("never-return-this-secret", allowed.text)
        self.assertIn("gaoji Control", page.text)
        self.assertIn('/bot-admin/favicon.svg?v=test', page.text)
        self.assertNotIn("unpkg.com", page.text)
        self.assertEqual(sandboxes.json()["active_commands"], 1)
        self.assertEqual(sandboxes.json()["resource"], "sandboxes")
        self.assertEqual(sandbox_start.status_code, 200)
        self.assertEqual(sandbox_start.json()["resource_version"], 1)
        self.assertEqual(sandbox_destroy_denied.status_code, 409)
        self.assertEqual(sandbox_destroy.status_code, 200)
        self.assertEqual(sandbox_destroy.json()["resource_version"], 2)
        self.assertEqual(
            sandbox_manager.started,
            [("group:930690526:user:3526452465", "s456def")],
        )
        self.assertEqual(
            sandbox_manager.destroyed,
            [("group:930690526:user:3526452465", "s456def")],
        )
        self.assertEqual(
            sandboxes.json()["items"][0]["agent_tasks"],
            [],
        )
        self.assertEqual(stickers.json()["counts"]["total"], 1)
        self.assertEqual(media.json()["counts"]["total"], 1)
        self.assertEqual(media.json()["vision_profile"], "gpt-5.6-luna")
        self.assertEqual(sources.json()["items"][0]["source_id"], 3)
        self.assertEqual(context_plans.json()["items"][0]["focus_message_id"], 10)
        self.assertEqual(
            context_debug.json()["items"][0]["current_topic"],
            "这个部署方案",
        )
        self.assertEqual(context_debug.json()["items"][0]["context_tokens"], 72)
        self.assertEqual(
            context_detail.json()["item"]["evidence_messages"][0]["message_id"],
            10,
        )
        self.assertEqual(context_feedback.status_code, 200)
        self.assertEqual(
            context_feedback.json()["feedback"]["verdict"],
            "off_topic",
        )
        self.assertEqual(databases.json()["writable_node"], "h610")
        self.assertEqual(
            [node["name"] for node in databases.json()["nodes"]],
            ["h610", "tank"],
        )
        self.assertNotIn("password", databases.text.lower())
        self.assertNotIn("rendered_context", context_plans.text)
        self.assertEqual(observability.status_code, 200)
        self.assertEqual(observability.json()["process"]["totals"]["turns"], 1)
        self.assertEqual(
            observability.json()["traces"]["items"][0]["trace_id"],
            "a" * 32,
        )
        self.assertNotIn("scope_key", observability.text)
        self.assertNotIn("objective", observability.text)
        self.assertEqual(
            observability.json()["alert_history"]["summary"]["triggered"],
            9,
        )
        self.assertEqual(alerts.status_code, 200)
        self.assertEqual(alerts.json()["summary"]["resolved"], 7)
        self.assertEqual(alerts.json()["days"], 30)
        self.assertEqual(alerts.json()["resource"], "alerts")
        group_rows = group_models.json()["items"]
        self.assertEqual(
            [row["group_id"] for row in group_rows],
            [201644592, 930690526],
        )
        self.assertFalse(group_rows[0]["enabled"])
        self.assertTrue(group_rows[1]["group_override"])
        self.assertEqual(group_rows[1]["overrides"][0]["profile"], "main")
        self.assertEqual(group_rows[1]["admins"][0]["user_id"], 3526452465)
        self.assertEqual(group_rows[1]["members"][0]["user_id"], 2291939848)
        self.assertNotIn("never-return-this-secret", group_models.text)

    def test_admin_can_change_group_and_member_models(self) -> None:
        models = ModelCatalog.from_json(
            json.dumps(
                {
                    "main": {
                        "provider": "cliproxy",
                        "model": "model-a",
                        "api_key_required": False,
                    },
                    "alternate": {
                        "provider": "cliproxy",
                        "model": "model-b",
                        "api_key_required": False,
                    }
                }
            ),
            default_profile="main",
            environ={},
        )
        preferences = ModelPreferences()
        preferences.models.clear()
        reasoning = ModelPreferences()
        reasoning.models.clear()
        app = FastAPI()
        register_admin(
            app,
            AdminServices(
                version="test",
                started_at=1,
                model_catalog=models,
                model_preferences=preferences,
                reasoning_preferences=reasoning,
                user_profiles=UserProfiles(),
                message_ledger=MessageLedger(),
                settings=Settings(),
            ),
            token="secret",
        )

        async def run():
            transport = httpx.ASGITransport(app=app)
            headers = {"X-Test-Login": "admin"}
            async with ApprovedClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                group = await client.put(
                    "/bot-admin/api/group-models/930690526/default",
                    headers=headers,
                    json={"profile": "alternate"},
                )
                member = await client.put(
                    "/bot-admin/api/group-models/930690526/users/2291939848",
                    headers=headers,
                    json={"profile": "main"},
                )
                toggle = await client.put(
                    "/bot-admin/api/group-models/930690526/enabled",
                    headers=headers,
                    json={"enabled": False},
                )
                vision_toggle = await client.put(
                    "/bot-admin/api/group-models/930690526/vision-auto-describe",
                    headers=headers,
                    json={"enabled": False},
                )
                group_effort = await client.put(
                    "/bot-admin/api/group-models/930690526/reasoning-effort",
                    headers=headers,
                    json={"effort": "high"},
                )
                member_effort = await client.put(
                    "/bot-admin/api/group-models/930690526/users/2291939848/reasoning-effort",
                    headers=headers,
                    json={"effort": "xhigh"},
                )
                snapshot = await client.get(
                    "/bot-admin/api/group-models",
                    headers=headers,
                )
                reset = await client.put(
                    "/bot-admin/api/group-models/930690526/users/2291939848",
                    headers=headers,
                    json={"profile": None},
                )
                reset_effort = await client.put(
                    "/bot-admin/api/group-models/930690526/users/2291939848/reasoning-effort",
                    headers=headers,
                    json={"effort": None},
                )
                invalid = await client.put(
                    "/bot-admin/api/group-models/930690526/default",
                    headers=headers,
                    json={"profile": "missing"},
                )
                invalid_effort = await client.put(
                    "/bot-admin/api/group-models/930690526/reasoning-effort",
                    headers=headers,
                    json={"effort": "absurd"},
                )
                return (
                    group,
                    member,
                    toggle,
                    vision_toggle,
                    group_effort,
                    member_effort,
                    snapshot,
                    reset,
                    reset_effort,
                    invalid,
                    invalid_effort,
                )

        (
            group,
            member,
            toggle,
            vision_toggle,
            group_effort,
            member_effort,
            snapshot,
            reset,
            reset_effort,
            invalid,
            invalid_effort,
        ) = asyncio.run(run())
        self.assertEqual(group.status_code, 200)
        self.assertEqual(member.status_code, 200)
        self.assertEqual(toggle.status_code, 200)
        self.assertEqual(vision_toggle.status_code, 200)
        self.assertEqual(group_effort.status_code, 200)
        self.assertEqual(member_effort.status_code, 200)
        row = next(
            item
            for item in snapshot.json()["items"]
            if item["group_id"] == 930690526
        )
        self.assertEqual(row["dynamic_group_profile"], "alternate")
        self.assertFalse(row["enabled"])
        self.assertFalse(row["enabled_override"])
        self.assertEqual(row["enabled_source"], "dashboard")
        self.assertFalse(row["vision_auto_describe"])
        self.assertEqual(row["vision_auto_describe_source"], "dashboard")
        selected_admin = next(
            item for item in row["admins"] if item["user_id"] == 3526452465
        )
        self.assertEqual(selected_admin["explicit_profile"], "main")
        self.assertEqual(selected_admin["effective_profile"], "main")
        self.assertIsNone(selected_admin["effective_reasoning_effort"])
        self.assertEqual(selected_admin["reasoning_effort_source"], "server")
        selected_member = next(
            item for item in row["members"] if item["user_id"] == 2291939848
        )
        self.assertEqual(selected_member["explicit_profile"], "main")
        self.assertEqual(row["member_reasoning_effort"], "high")
        self.assertEqual(selected_member["explicit_reasoning_effort"], "xhigh")
        self.assertEqual(selected_member["effective_reasoning_effort"], "xhigh")
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(reset_effort.status_code, 200)
        self.assertNotIn(
            "group:930690526:user:2291939848",
            preferences.models,
        )
        self.assertNotIn(
            "group:930690526:user:2291939848",
            reasoning.models,
        )
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid_effort.status_code, 422)

    def test_admin_v1_versions_audits_and_rejects_stale_tool_updates(self) -> None:
        self.addCleanup(configure_tool_overrides, {})
        app = FastAPI()
        register_admin(
            app,
            AdminServices(version="test", started_at=1),
            token="secret",
        )

        async def run():
            transport = httpx.ASGITransport(app=app)
            headers = {
                "X-Test-Login": "admin",
                "If-Match": '"0"',
                "X-Admin-Actor": "Kenneth",
            }
            async with ApprovedClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                before = await client.get(
                    "/bot-admin/api/v1/tools",
                    headers={"X-Test-Login": "admin"},
                )
                changed = await client.put(
                    "/bot-admin/api/v1/tools/web_search/enabled",
                    headers=headers,
                    json={"enabled": False},
                )
                stale = await client.put(
                    "/bot-admin/api/v1/tools/web_search/enabled",
                    headers=headers,
                    json={"enabled": True},
                )
                audit = await client.get(
                    "/bot-admin/api/v1/audit",
                    headers={"X-Test-Login": "admin"},
                )
                versions = await client.get(
                    "/bot-admin/api/v1/resource-versions",
                    headers={"X-Test-Login": "admin"},
                )
                return before, changed, stale, audit, versions

        before, changed, stale, audit, versions = asyncio.run(run())
        self.assertEqual(before.json()["resource_version"], 0)
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json()["resource_version"], 1)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(
            stale.json()["detail"]["code"],
            "resource_version_conflict",
        )
        self.assertEqual(versions.json()["versions"]["tools"], 1)
        self.assertTrue(audit.json()["items"][0]["actor"].startswith("account:"))
        self.assertTrue(audit.json()["items"][0]["actor"].endswith(":kenneth"))
        self.assertEqual(audit.json()["items"][0]["action"], "tool.enabled.set")


if __name__ == "__main__":
    unittest.main()
