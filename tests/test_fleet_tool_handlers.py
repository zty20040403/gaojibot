from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import nonebot

nonebot.init()

from src.plugins.ai_chat.fleet_client import FleetControlError
from src.plugins.ai_chat.tools.fleet import HANDLERS
from src.plugins.ai_chat.tools.registry import ToolRegistry
from src.plugins.ai_chat.tools.state import ToolContext, TurnState


class FleetToolHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.client = SimpleNamespace(
            **{
                name: AsyncMock(return_value={"ok": True})
                for name in (
                    "ops_catalog",
                    "ops_call",
                    "prepare_operation",
                    "operation",
                    "cancel_operation",
                    "upload_artifact",
                    "submit_job",
                    "job",
                    "runbook_cases",
                    "search_runbook_cases",
                    "host",
                    "unit",
                    "logs",
                    "guardian",
                    "create_guardian",
                    "run_diagnostic",
                )
            }
        )
        self.account_store = Mock()
        self.account_store.account_for_qq.return_value = {"id": "admin"}
        self.app = SimpleNamespace(
            fleet_client=self.client,
            mobile_authorization=SimpleNamespace(store=self.account_store),
            semantic_recall=Mock(),
            semantic_index_state=Mock(),
            logger=Mock(),
        )
        self.event = SimpleNamespace(user_id=7, group_id=100)
        self.scope = "onebot-v11:group:100"
        services = SimpleNamespace(
            context=self.app,
            chat=SimpleNamespace(
                _conversation_scope=lambda event: SimpleNamespace(
                    key=f"onebot-v11:group:{event.group_id}"
                )
            ),
        )
        self.turn = TurnState(
            ToolContext(services, self.event, frozenset({"fleet", "fleet_logs", "ops"}))
        )
        self.registry = ToolRegistry(HANDLERS)

    async def run_tool(self, tool_name, **arguments):
        return json.loads(await self.registry.execute(self.turn, tool_name, arguments))

    async def test_capabilities_and_current_admin_registration_are_rechecked(self):
        for capability, tool_name in (
            ("fleet", "host_inspect"),
            ("fleet_logs", "service_logs"),
            ("ops", "ops_call"),
        ):
            with self.subTest(capability=capability):
                self.turn.context = replace(
                    self.turn.context,
                    capabilities=frozenset({"fleet", "fleet_logs", "ops"})
                    - {capability},
                )
                self.assertNotIn(
                    tool_name,
                    {
                        item["function"]["name"]
                        for item in self.registry.definitions(self.turn.context)
                    },
                )
                result = await self.run_tool(
                    tool_name, host_id="node", unit="gaoji.service", approval="granted"
                )
                self.assertEqual(result["code"], "tool_unavailable")
        self.turn.context = replace(
            self.turn.context, capabilities=frozenset({"fleet", "fleet_logs", "ops"})
        )
        self.account_store.account_for_qq.return_value = None
        for name in (
            "ops_catalog",
            "ops_call",
            "host_reboot",
            "service_control",
            "cluster_guardian_create",
            "cluster_guardian_status",
        ):
            self.assertFalse((await self.run_tool(name, host_id="node"))["ok"])
        for method in vars(self.client).values():
            method.assert_not_awaited()

    async def test_operation_identity_is_host_derived_and_verification_is_preserved(
        self,
    ):
        await self.run_tool(
            "operation_prepare",
            host_id="node",
            resource_ref="gaoji.service",
            operation="restart",
            expected_state={"invocation_id": "before"},
            verification={"healthy": True},
            idempotency_key="task:1",
            actor="qq:999",
            origin="foreign",
            compensation={"delete": True},
        )
        call = self.client.prepare_operation.call_args
        self.assertEqual(call.kwargs, {"actor": "qq:7", "origin": self.scope})
        self.assertEqual(call.args[0]["expected_state"], {"invocation_id": "before"})
        self.assertEqual(call.args[0]["verification"], {"healthy": True})
        self.assertEqual(call.args[0]["idempotency_key"], "task:1")
        self.assertEqual(call.args[0]["compensation"], {})
        await self.run_tool(
            "service_control",
            host_id="node",
            unit="gaoji.service",
            action="restart",
            idempotency_key="task:2",
            expected_invocation_id="before",
            actor="qq:999",
        )
        call = self.client.ops_call.call_args
        self.assertEqual(call.kwargs, {"actor": "qq:7", "origin": self.scope})
        self.assertEqual(
            call.args[0],
            {
                "operation": "units.restart",
                "params": {
                    "host": "node",
                    "unit": "gaoji.service",
                    "expected_invocation_id": "before",
                },
                "idempotency_key": "task:2",
            },
        )
        for tool, method, field in (
            ("operation_status", "operation", "operation_id"),
            ("operation_cancel", "cancel_operation", "operation_id"),
            ("cluster_job_status", "job", "job_id"),
        ):
            await self.run_tool(
                tool, **{field: "id", "actor": "qq:999", "origin": "foreign"}
            )
            getattr(self.client, method).assert_awaited_once_with(
                "id", actor="qq:7", origin=self.scope
            )

    async def test_upload_reads_only_the_current_sandbox_owner_with_bounded_size(self):
        self.assertFalse(
            (
                await self.run_tool(
                    "cluster_artifact_upload", path="/workspace/site.zip"
                )
            )["ok"]
        )
        manager = SimpleNamespace(read_file=AsyncMock(return_value=b"zip-data"))
        self.turn.agent_executor = SimpleNamespace(
            owner="group:100", sandbox_manager=manager
        )
        await self.run_tool(
            "cluster_artifact_upload",
            sandbox_id="box",
            path="/workspace/site.zip",
            name="site.zip",
            media_type="application/zip",
            owner="group:999",
        )
        manager.read_file.assert_awaited_once_with(
            "group:100", "box", "site.zip", max_bytes=25 * 1024 * 1024
        )
        self.client.upload_artifact.assert_awaited_once_with(
            name="site.zip",
            media_type="application/zip",
            content=b"zip-data",
            actor="qq:7",
            origin=self.scope,
        )

    async def test_guardian_is_observe_only_even_if_model_requests_actions(self):
        await self.run_tool(
            "cluster_guardian_create",
            host_id="node",
            target_id="health",
            mode="act",
            max_actions=100,
            authorized_action={"command": "reboot"},
            expires_at=2000,
        )
        call = self.client.create_guardian.call_args
        payload = call.args[0]
        self.assertEqual(payload["mode"], "observe")
        self.assertEqual(payload["max_actions"], 0)
        self.assertEqual(payload["authorized_action"], {})
        self.assertEqual(payload["expires_at"], 2000)
        self.assertEqual(call.kwargs, {"actor": "qq:7", "origin": self.scope})

    async def test_validation_and_log_bounds_run_before_remote_access(self):
        for tool, arguments in (
            ("service_logs", {"host_id": "node;reboot", "unit": "gaoji.service"}),
            ("service_inspect", {"host_id": "node", "unit": "gaoji.service;reboot"}),
            ("service_inspect", {"host_id": "node"}),
            ("operation_prepare", {"resource_ref": "gaoji"}),
            ("diagnose_incident", {"host_id": "node", "template": "shell"}),
            (
                "diagnose_incident",
                {"host_id": "node", "template": "admin_502", "target_id": "bad/url"},
            ),
        ):
            self.assertFalse((await self.run_tool(tool, **arguments))["ok"])
        for method in vars(self.client).values():
            method.assert_not_awaited()
        await self.run_tool(
            "service_logs",
            host_id="node",
            unit="gaoji.service",
            lines=999999,
            since_seconds=999999,
        )
        self.client.logs.assert_awaited_with(
            "node", "gaoji.service", lines=200, since_seconds=86400
        )
        await self.run_tool(
            "service_logs",
            host_id="node",
            unit="gaoji.service",
            lines="bad",
            since_seconds="bad",
        )
        self.client.logs.assert_awaited_with(
            "node", "gaoji.service", lines=50, since_seconds=3600
        )

    async def test_runbook_recall_retains_lexical_fallback_and_applicability(self):
        self.turn.user_text = "service failed"
        self.client.runbook_cases.return_value = {"items": [{"case_id": "old"}]}
        self.client.host.side_effect = FleetControlError("offline", "offline")
        self.client.search_runbook_cases.return_value = {
            "items": [
                {"case_id": "old", "retrieval_score": 5, "applicable": False},
                {"case_id": "current", "retrieval_score": 2, "applicable": True},
            ]
        }
        with patch(
            "src.plugins.ai_chat.tools.fleet.semantic_runbook_scores",
            new=AsyncMock(side_effect=RuntimeError("index unavailable")),
        ):
            result = await self.run_tool(
                "cluster_case_search", host_id="node", actor="foreign"
            )
        self.assertEqual(result["retrieval"], "lexical")
        self.assertEqual(
            [item["case_id"] for item in result["items"]], ["current", "old"]
        )
        payload = self.client.search_runbook_cases.call_args.args[0]
        self.assertEqual(payload["query"], self.turn.user_text)
        self.assertEqual(payload["current_facts"]["host"], {"status": "unavailable"})
        self.assertEqual(
            self.client.search_runbook_cases.call_args.kwargs,
            {"actor": "qq:7", "origin": self.scope},
        )

    async def test_remote_errors_are_structured_and_cancellation_propagates(self):
        self.client.job.side_effect = FleetControlError(
            "offline", "worker unavailable", retryable=True
        )
        result = await self.run_tool("cluster_job_status", job_id="j1")
        self.assertEqual(
            result,
            {
                "ok": False,
                "status": "unavailable",
                "error_code": "offline",
                "error": "worker unavailable",
                "retryable": True,
            },
        )
        self.client.job.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.run_tool("cluster_job_status", job_id="j1")
