from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from src.cluster_control.ops_management import OpsManagementService
from src.cluster_control.service_verification import receipt
from tests import test_ops_management as fixtures


class ServiceVerificationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ManagementTests.asyncSetUp
    asyncTearDown = fixtures.ManagementTests.asyncTearDown
    propose = fixtures.ManagementTests.propose
    approve = fixtures.ManagementTests.approve
    posts = fixtures.ManagementTests.posts
    finish_verification = fixtures.ManagementTests.finish_verification

    async def start(self, action="restart"):
        if action != "restart":
            self.definitions.append({**self.definitions[1], "name": "units." + action})
        proposal = await self.manager.call("units." + action, {"host": "h610", "unit": "test.service"},
            actor="qq:3526452465", origin="group:123", idempotency_key="unit-verification-test")
        self.key = proposal["operation"]["operation_id"]
        await self.approve(proposal)
        await self.manager.run_once()
        self.state = "succeeded"

    def record(self):
        return self.store.get_operation(self.key)

    async def test_restart_needs_changed_instance_and_two_fresh_observations(self):
        await self.start()
        await self.manager.run_once()
        self.assertEqual(self.record()["status"], "reconciling")
        self.assertTrue(self.record()["result"]["verification"]["restart_confirmed"])
        self.assertFalse(self.record()["result"]["verification"]["verified"])
        await self.finish_verification()
        record = self.record()
        self.assertEqual(record["status"], "succeeded")
        self.assertIn("当前 PID 234", record["result"]["summary"])
        self.assertEqual(record["result"]["verification"]["application_health"], "not_checked")
        self.assertTrue(self.manager.proposal_result(record)["effect_verified"])
        self.assertEqual(len(self.posts("units.restart")), 1)

    async def test_operation_and_receipt_share_the_controlled_clock(self):
        self.now = 1_700_000_000.75
        self.finished_at = self.now
        await self.start()
        self.assertEqual(self.record()["created_at"], int(self.now))
        await self.manager.run_once()
        self.assertEqual(self.record()["status"], "reconciling")
        await self.finish_verification()
        self.assertEqual(self.record()["status"], "succeeded")

    async def test_receipt_before_operation_remains_rejected(self):
        await self.start()
        self.finished_at = self.record()["created_at"] - 1
        await self.manager.run_once()
        self.assertEqual(self.record()["status"], "needs_attention")
        self.assertEqual(self.record()["error_code"], "service_receipt_unverified")
        self.assertFalse(self.posts("units.status"))

    async def test_job_success_without_instance_change_is_not_restart_success(self):
        await self.start()
        self.after["invocation_id"] = self.before["invocation_id"]
        await self.manager.run_once()
        self.assertEqual(self.record()["status"], "needs_attention")
        self.assertEqual(self.record()["error_code"], "service_restart_unverified")
        self.assertFalse(self.posts("units.status"))

    async def test_stale_wrong_target_future_and_cached_observations_do_not_verify(self):
        await self.start()
        for age, host, unit in [(120, None, "test.service"), (0, "tank", "test.service"),
                                (-60, None, "test.service"), (0, None, "another.service")]:
            with self.subTest(age=age, host=host, unit=unit):
                self.live_age, self.live_host, self.live["unit"] = age, host, unit
                await self.manager.run_once()
                self.assertEqual(self.record()["status"], "reconciling")
                self.assertFalse(self.record()["result"]["verification"]["verified"])
        self.live_age, self.live_host, self.live["unit"] = 0, None, "test.service"
        await self.manager.run_once()
        await self.manager.run_once()
        self.assertEqual(self.record()["status"], "reconciling", "Re-reading one cached observation is not independent evidence")
        await self.finish_verification()
        self.assertEqual(self.record()["status"], "succeeded")

    async def test_service_that_dies_after_command_success_is_failed(self):
        await self.start()
        await self.manager.run_once()
        self.live.update(active_state="failed", sub_state="failed")
        await self.finish_verification()
        self.assertEqual(self.record()["status"], "failed")
        self.assertEqual(self.record()["error_code"], "service_unhealthy_after_action")
        self.assertIn("failed", self.record()["result"]["summary"])

    async def test_another_instance_after_receipt_never_verifies_this_action(self):
        await self.start()
        await self.manager.run_once()
        self.live["details"]["invocation_id"] = "c" * 32
        await self.finish_verification()
        self.assertEqual(self.record()["status"], "reconciling")
        self.now += 121
        await self.manager.run_once()
        self.assertEqual(self.record()["status"], "needs_attention")
        self.assertEqual(self.record()["error_code"], "service_verification_timeout")
        self.assertEqual(len(self.posts("units.restart")), 1)

    async def test_outage_then_controller_takeover_preserves_evidence(self):
        await self.start()
        await self.manager.run_once()
        first = self.record()["result"]["verification"]["stable_since"]
        self.fail_unit_read = True
        await self.finish_verification()
        self.assertEqual(self.record()["status"], "reconciling")
        self.assertEqual(self.record()["result"]["verification"]["stable_since"], first)
        previous = self.manager
        self.manager = OpsManagementService(previous.client, self.store, hosts=tuple(previous.hosts), actors=tuple(previous.actors))
        self.fail_unit_read = False
        await self.finish_verification()
        self.assertEqual(self.record()["status"], "succeeded")
        self.assertEqual(self.record()["backend_operation_id"], "job_test")
        self.assertEqual(len(self.posts("units.restart")), 1)

    async def test_job_status_outage_keeps_verification_progress(self):
        await self.start()
        await self.manager.run_once()
        first = self.record()["result"]["verification"]["stable_since"]
        self.fail_poll = True
        await self.finish_verification()
        self.assertEqual(self.record()["result"]["verification"]["stable_since"], first)
        self.fail_poll = False
        await self.finish_verification()
        self.assertEqual(self.record()["status"], "succeeded")
        self.assertEqual(len(self.posts("jobs.status")), 1, "Persisted completion needs only fresh unit observations")

    async def test_start_of_running_service_is_reported_as_noop_not_restart(self):
        self.before["invocation_id"] = self.after["invocation_id"]
        await self.start("start")
        await self.manager.run_once()
        await self.finish_verification()
        self.assertEqual(self.record()["status"], "succeeded")
        self.assertIn("本次未重启", self.record()["result"]["summary"])

    async def test_stop_requires_inactive_target(self):
        self.after.update(active_state="inactive", sub_state="dead", invocation_id="")
        await self.start("stop")
        await self.manager.run_once()
        self.assertEqual(self.record()["status"], "reconciling")
        self.live.update(active_state="inactive", sub_state="dead")
        self.live["details"].update(invocation_id="", main_pid=0)
        await self.finish_verification()
        self.assertEqual(self.record()["status"], "reconciling")
        await self.finish_verification()
        self.assertEqual(self.record()["status"], "succeeded")
        self.assertIn("已停止", self.record()["result"]["summary"])

    async def test_reload_does_not_require_a_new_process_but_requires_reload_success(self):
        self.before["invocation_id"] = self.after["invocation_id"]
        await self.start("reload")
        await self.manager.run_once()
        await self.finish_verification()
        self.assertEqual(self.record()["status"], "succeeded")
        self.assertIn("已重载", self.record()["result"]["summary"])

    async def test_failed_reload_receipt_is_failure(self):
        await self.start("reload")
        self.after["reload_result"] = "exit-code"
        await self.manager.run_once()
        self.assertEqual(self.record()["status"], "failed")

    async def test_restart_counter_change_resets_stability_window(self):
        await self.start()
        await self.manager.run_once()
        self.live["details"]["restarts"] += 1
        await self.finish_verification()
        self.assertEqual(self.record()["status"], "reconciling")
        await self.finish_verification()
        self.assertEqual(self.record()["status"], "succeeded")

    async def test_no_unit_is_rejected_before_preparing_effect(self):
        with self.assertRaises(ValueError):
            await self.manager.call("units.restart", {"host": "h610"}, actor="admin:kenneth",
                origin="test", idempotency_key="missing-unit-test")
        self.assertFalse(self.store.records)

    async def test_missing_or_malformed_baseline_does_not_mean_previously_stopped(self):
        await self.start()
        self.before.clear()
        await self.manager.run_once()
        self.assertEqual(self.record()["status"], "needs_attention")
        self.assertFalse(self.record()["result"]["verification"]["verified"])
        self.assertNotIn("原本就在运行", self.record()["result"]["summary"])

    async def test_verifier_exception_cannot_leave_success_status(self):
        await self.start()
        with patch("src.cluster_control.ops_management.verify_service", side_effect=TypeError("invalid receipt")):
            await self.manager.run_once()
        self.assertEqual(self.record()["status"], "needs_attention")
        self.assertFalse(self.manager.proposal_result(self.record())["effect_verified"])
        self.assertIn("invalid receipt", self.record()["result"]["summary"])

    async def test_malformed_after_state_is_not_success(self):
        await self.start()
        self.after["sub_state"] = []
        await self.manager.run_once()
        self.assertEqual(self.record()["status"], "needs_attention")
        self.assertFalse(self.record()["result"]["verification"]["verified"])

    async def test_cached_receipt_cannot_verify_after_deadline(self):
        await self.start()
        await self.manager.run_once()
        count = len(self.posts("units.status"))
        self.now += 130
        self.store.records[self.key]["deadline_at"] = self.now - 1
        await self.manager.run_once()
        self.assertEqual(self.record()["status"], "needs_attention")
        self.assertEqual(self.record()["error_code"], "service_verification_timeout")
        self.assertFalse(self.record()["result"]["verification"]["verified"])
        self.assertEqual(len(self.posts("units.status")), count)

    async def test_observation_crossing_deadline_cannot_confirm_success(self):
        await self.start()
        await self.manager.run_once()
        perform = self.manager.client._request
        async def slow_read(*args, **kwargs):
            if json.loads(kwargs.get("body") or b"{}").get("op") == "units.status":
                self.now += 121
            return await perform(*args, **kwargs)
        with patch.object(self.manager.client, "_request", side_effect=slow_read):
            await self.finish_verification()
        self.assertEqual(self.record()["status"], "needs_attention")
        self.assertEqual(self.record()["error_code"], "service_verification_timeout")
        self.assertFalse(self.record()["result"]["verification"]["verified"])
        self.assertEqual(len(self.posts("units.restart")), 1)

    async def test_expected_baseline_must_match_actual_receipt_not_just_echoed_spec(self):
        await self.start()
        self.store.records[self.key]["arguments"]["params"]["expected_invocation_id"] = "c" * 32
        self.params["expected_invocation_id"] = "c" * 32
        await self.manager.run_once()
        self.assertEqual(self.record()["status"], "needs_attention")
        self.assertEqual(self.record()["error_code"], "service_receipt_unverified")

    async def test_unrelated_incomplete_or_forged_receipt_is_not_accepted(self):
        await self.start()
        await self.manager.run_once()
        record = self.record()
        original = record["result"]
        changes = [("handle", "host", "tank"), ("handle", "operation", "units.start"),
                   ("handle", "job_id", "other-job"), ("spec", "unit", "wrong.service"),
                   ("spec", "expected_invocation_id", "different"), ("result", "success", False),
                   ("result", "attribution", "exit_zero"), ("result", "action", "start"),
                   ("result", "unit", "wrong.service"), ("result", "before", None),
                   ("result", "before", {}), ("result", "manager_job", "/org/freedesktop/systemd1/job/not-a-job")]
        for section, key, value in changes:
            with self.subTest(section=section, key=key):
                wrong = copy.deepcopy(original)
                wrong[section][key] = value
                with self.assertRaises(ValueError):
                    receipt(record, wrong, self.now)
        for value in ["2020-01-01T00:00:00Z", "2030-01-01T00:00:00Z", "2026-09-10T00:00:00"]:
            with self.assertRaises(ValueError):
                receipt(record, {**original, "updated_at": value}, self.now)


if __name__ == "__main__":
    unittest.main()
