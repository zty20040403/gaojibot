"""Actual SSH catalog + OpsManagement contract; no SSH process is launched."""
import copy
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from src.bot_security.store import SecurityError
from src.cluster_control.adapters.ssh import SSHOperationsClient
from src.cluster_control.api import create_app
from src.cluster_control.execution_contracts import content_hash
from src.cluster_control.ops_management import OpsManagementService
from src.plugins.ai_chat.adapters.qq_official.approval_transport import FrozenSource
from src.plugins.ai_chat.adapters.qq_official.host_authority import AllowedService, OfficialHostAuthority
from src.plugins.ai_chat.fleet_client import FleetControlClient, FleetControlError
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey
from tests import qq_official_host_fixture as fixtures
from tests import test_ops_management as management_tests
from tests import test_qq_official_host_background as background_tests


def backend(directory):
    known = directory / "TEST_KNOWN_HOSTS"
    known.write_text("TEST_SYNTHETIC_HOST_KEY")
    return SSHOperationsClient({"testhost": {"destination": "operator@test.invalid",
        "helper": "/test/ssh-ops"}}, known_hosts_file=str(known), writable=True)


class SSHContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1800000000
        self.clock = patch("src.cluster_control.ops_management.time", SimpleNamespace(time=lambda: self.now))
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.backend = backend(Path(self.tmp.name))
        self.store = management_tests.MemoryStore()
        self.manager = OpsManagementService(self.backend, self.store,
            hosts=("testhost",), actors=("admin:test_admin",))
        account = AccountKey("qq-official", "TEST_SSH_CONTRACT", "test")
        endpoint = EndpointKey(account, "private", "TEST_ACTOR")
        actor = ActorKey(account, "c2c_user", "", "TEST_ACTOR")
        self.source = FrozenSource(endpoint, actor, endpoint.scope,
            execution_id="a" * 32, deadline_at=self.now + 180)
        transport = SimpleNamespace(now=lambda: self.now)
        mobile = SimpleNamespace(store=SimpleNamespace(transport=transport))
        self.host = OfficialHostAuthority(transport=transport, mobile=mobile,
            services=(AllowedService("testhost", "test.service", "ssh-management-v1"),))
        self.account = {"username": "test_admin"}

    async def prepare(self, operation="units.restart", **changes):
        return (await self.manager.call(operation, {"host": "testhost", "unit": "test.service"},
            actor="admin:test_admin", origin=self.host.origin(self.source),
            **({"idempotency_key": "TEST_SSH_" + operation, "deadline_at": self.now + 120} | changes)))["operation"]

    async def test_real_catalog_three_services_exact_hash_and_original_deadline(self):
        for action in ("start", "stop", "restart"):
            record = await self.prepare("units." + action)
            contract = self.host.contract(record, self.source, self.account)
            self.assertEqual(contract["operation"], "ssh.execute")
            self.assertEqual(record["deadline_at"], self.now + 120)
            self.assertEqual(record["resource_budget"], {"wall_seconds": 120})
            self.assertEqual(record["arguments"]["params"], {"host": "testhost", "unit": "test.service"})
            definition = next(d for d in await self.manager.definitions() if d["name"] == "units." + action)
            self.assertEqual(record["arguments"]["binding_hash"], self.manager.binding_hash(definition))
            self.assertEqual(record["contract_hash"], content_hash({"actor": record["actor_id"],
                "origin": record["origin_scope"], "arguments": record["arguments"]}))

    async def test_rehashed_shell_other_unit_and_parent_cannot_be_service_authority(self):
        record = await self.prepare()
        for changes in ({"op": "exec.run"}, {"params": {"host": "testhost", "unit": "other.service"}},
                        {"params": {"host": "testhost", "unit": "test.service", "command": "TEST"}},
                        {"guardian_id": "TEST"}, {"upstream_idempotency": "none"}, {"binding_hash": "invalid"}):
            altered = copy.deepcopy(record)
            altered["arguments"].update(changes)
            altered["contract_hash"] = content_hash({"actor": altered["actor_id"],
                "origin": altered["origin_scope"], "arguments": altered["arguments"]})
            with self.subTest(changes=changes), self.assertRaises(SecurityError):
                self.host.contract(altered, self.source, self.account)

    async def test_same_key_does_not_refresh_and_legacy_default_is_unchanged(self):
        record = await self.prepare()
        self.now += 30
        again = await self.prepare(deadline_at=self.now + 150)
        self.assertEqual(again, record)
        with self.assertRaises(ValueError):
            await self.prepare(deadline_at=self.now + 10)
        legacy = await self.prepare(idempotency_key="TEST_LEGACY", deadline_at=None)
        self.assertEqual(legacy["deadline_at"], self.now + 1800)
        self.assertEqual(legacy["resource_budget"], {"wall_seconds": 1800})
        with self.assertRaises(SecurityError):
            self.host.contract(legacy, self.source, self.account)

    async def test_binding_rotation_is_rejected_by_actual_management_approval(self):
        record = await self.prepare()
        Path(self.backend.known_hosts_file).write_text("TEST_ROTATED_HOST_KEY")
        with self.assertRaises(PermissionError):
            await self.manager.approve(record["operation_id"], actor="admin:test_admin",
                expected_hash=record["contract_hash"], expected_version=record["resource_version"])
        self.assertEqual(self.store.get_operation(record["operation_id"])["status"], "awaiting_approval")

    async def test_expired_and_noninteger_deadlines_never_create_intent(self):
        for value in (self.now, self.now - 1, True, float(self.now + 1)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                await self.prepare(deadline_at=value)
        self.assertFalse(self.store.records)


async def install_management(case):
    case.ssh_backend = backend(Path(case.tmp.name))
    case.management = OpsManagementService(case.ssh_backend, case.remote_store,
        hosts=("testhost",), actors=("admin:test_admin_a", "admin:test_admin_b"))
    clock = patch("src.cluster_control.ops_management.time", SimpleNamespace(time=lambda: case.now))
    clock.start()
    case.addCleanup(clock.stop)
    case.host.services = {("testhost", "test.service"): AllowedService("testhost", "test.service", "ssh-management-v1")}
    case.app = create_app(case.fleet_service, api_token_file=case.fleet_token,
                          execution=case.execution, management=case.management)
    old_authorization = case.client.authorization
    await case.client.close()

    class Observed(httpx.ASGITransport):
        async def handle_async_request(self, request):
            case.http_calls.append((request.method, request.url.path, None))
            return await super().handle_async_request(request)

    case.client = FleetControlClient("http://test-control.invalid", case.fleet_token,
                                    transport=Observed(app=case.app))
    case.client.authorization = old_authorization
    old_authorization.client = case.client


class SSHHostPostgresTests(fixtures.HostFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await install_management(self)

    async def test_real_asgi_management_quorum_queues_exact_operation_once(self):
        record = (await self.prepare())["operation"]
        self.assertEqual(record["operation"], "ssh.execute")
        self.assertEqual(record["deadline_at"], self.body["deadline_at"])
        self.assertEqual(record["arguments"]["params"], {"host": "testhost", "unit": "test.service"})
        self.assertIn("units.restart", self.fake.received[-1]["payload"]["content"])
        await self.quorum()
        result = await self.approve(record)
        self.assertTrue(result["submitted"])
        self.assertFalse(result["executed"])
        self.assertTrue((await self.approve(record))["submitted"])
        self.assertEqual(self.remote_store.get_operation(record["operation_id"])["status"], "queued")
        self.assertEqual(len([c for c in self.http_calls if c[0] == "POST" and c[1].endswith("/approve")]), 1)

    async def test_actual_remote_rotation_after_quorum_never_queues(self):
        record = (await self.prepare())["operation"]
        await self.quorum()
        Path(self.ssh_backend.known_hosts_file).write_text("TEST_ROTATED_AFTER_QUORUM")
        with self.assertRaises(FleetControlError):
            await self.approve(record)
        self.assertEqual(self.remote_store.get_operation(record["operation_id"])["status"], "awaiting_approval")
        self.assertIsNone((await self.approve(record))["executed"])
        self.assertEqual(len([c for c in self.http_calls if c[0] == "POST" and c[1].endswith("/approve")]), 1)

    async def test_source_revoke_still_blocks_actual_ssh_contract(self):
        record = (await self.prepare())["operation"]
        await self.quorum()
        self.source_live = False
        with self.assertRaises((FleetControlError, PermissionError)):
            await self.approve(record)
        self.assertFalse([c for c in self.http_calls if c[0] == "POST" and c[1].endswith("/approve")])


class SSHBackgroundPostgresTests(fixtures.HostFixture):
    # Reuse setup/helpers only, so discovery does not duplicate another suite.
    metadata = background_tests.BackgroundTests.metadata
    bound = background_tests.BackgroundTests.bound
    prepare = background_tests.BackgroundTests.prepare
    approve = background_tests.BackgroundTests.approve

    async def asyncSetUp(self):
        await background_tests.BackgroundTests.asyncSetUp(self)
        await install_management(self)

    async def test_actual_external_journal_quorum_resume_and_once_approval(self):
        await background_tests.BackgroundTests.test_pending_quorum_resume_is_fact_not_execution_and_approve_is_once(self)
        calls = self.task_store.external_calls(self.task.task_id)
        self.assertTrue(calls)
        self.assertTrue(all(item["response"]["operation"]["operation"] == "ssh.execute" for item in calls))
