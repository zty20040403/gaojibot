"""Policy tests run against an in-memory firewall, never the host rules."""
from __future__ import annotations

import copy
import json
import subprocess
import unittest
from unittest.mock import AsyncMock, patch

from src.sandbox_runtime import network
from src.sandbox_runtime.docker import DockerSandboxManager, SandboxError, SandboxResult


def inspected():
    return [{"Name": network.NETWORK, "Driver": "bridge", "EnableIPv6": False, "Internal": False,
             "Labels": {network.LABEL: network.VERSION}, "Options": dict(network.OPTIONS),
             "IPAM": {"Config": [{"Subnet": "172.25.0.0/16"}]}}]


class FakeHost:
    def __init__(self):
        self.calls = []
        self.row = inspected()
        self.chains = {(binary, name): [] for binary in ("iptables", "ip6tables")
                       for name in ("INPUT", "FORWARD", "DOCKER-USER")}
        self.chains["iptables", "DOCKER-USER"] = [("-j", "RETURN")]

    def __call__(self, command, *, check=True):
        self.calls.append(command)
        code, output = 0, ""
        if command[:3] == ["docker", "network", "inspect"]:
            output = json.dumps(self.row)
        else:
            binary, _wait, _seconds, action, name, *args = command
            key = binary, name
            rule = tuple(args)
            if action == "-S":
                code = int(key not in self.chains)
            elif action == "-N":
                self.chains[key] = []
            elif action == "-F":
                self.chains[key].clear()
            elif action == "-C":
                code = int(rule not in self.chains[key])
            elif action == "-D":
                self.chains[key].remove(rule)
            elif action == "-I":
                self.chains[key].insert(int(args[0]) - 1, tuple(args[1:]))
            elif action == "-A":
                self.chains[key].append(rule)
            else:
                raise AssertionError(command)
        result = subprocess.CompletedProcess(command, code, output, "")
        if code and check:
            result.check_returncode()
        return result


class NetworkPolicyTests(unittest.TestCase):
    def test_rejects_impostor_network_and_ipv6_or_public_subnets(self):
        network.validate_network(inspected())
        invalid = [[], {}, [{"Name": network.NETWORK}]]
        for key, value in (("Driver", "host"), ("EnableIPv6", True), ("Internal", True),
                           ("Labels", {}), ("Options", {}),
                           ("IPAM", {"Config": [{"Subnet": "8.8.8.0/24"}]})):
            row = inspected()
            row[0][key] = value
            invalid.append(row)
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(ValueError):
                network.validate_network(row)

    def test_policy_is_idempotent_and_never_flushes_other_traffic(self):
        host = FakeHost()
        with patch.object(network, "run", host):
            network.provision()
            first = copy.deepcopy(host.chains)
            network.provision()
        self.assertEqual(first, host.chains)
        policy = host.chains["iptables", network.CHAIN]
        for cidr in network.BLOCKED:
            self.assertIn(("-d", cidr, "-j", "DROP"), policy)
        self.assertIn(("-m", "addrtype", "--dst-type", "LOCAL", "-j", "DROP"), policy)
        self.assertIn(("-m", "conntrack", "--ctstatus", "DNAT", "-j", "DROP"), policy)
        self.assertEqual(policy[-1], ("-j", "RETURN"))
        self.assertEqual(host.chains["iptables", "DOCKER-USER"][0], ("-i", network.BRIDGE, "-j", network.CHAIN))
        self.assertIn(("-j", "RETURN"), host.chains["iptables", "DOCKER-USER"])
        self.assertEqual(host.chains["iptables", "FORWARD"], [("-i", network.BRIDGE, "-j", network.CHAIN)])
        for binary, chain in (("iptables", "INPUT"), ("ip6tables", "INPUT"), ("ip6tables", "FORWARD")):
            self.assertEqual(host.chains[binary, chain][0][-2:], ("-j", "DROP"))
        self.assertTrue(all(command[-1] == network.CHAIN for command in host.calls if "-F" in command))

    def test_setup_failure_leaves_only_sandbox_bridge_fail_closed(self):
        host = FakeHost()
        del host.chains["iptables", "DOCKER-USER"]
        with patch.object(network, "run", host), self.assertRaises(subprocess.CalledProcessError):
            network.provision()
        for binary in ("iptables", "ip6tables"):
            for name in ("INPUT", "FORWARD"):
                self.assertEqual(host.chains[binary, name][0][:2], ("-i", network.BRIDGE))
                self.assertEqual(host.chains[binary, name][0][-2:], ("-j", "DROP"))
        self.assertFalse(any("-F" in call for call in host.calls))

    def test_foreign_network_is_rejected_before_any_firewall_changes(self):
        host = FakeHost()
        host.row[0]["Labels"] = {}
        with patch.object(network, "run", host), self.assertRaises(ValueError):
            network.provision()
        self.assertEqual(len(host.calls), 1)
        self.assertEqual(host.calls[0], ["docker", "network", "inspect", network.NETWORK])

    def test_modes_and_helper_network_cannot_bypass_policy(self):
        for mode, name in (("isolated", network.NETWORK), ("none", "none"), ("bridge", "bridge")):
            manager = DockerSandboxManager(network=mode)
            command = manager._docker_command(("docker", "run", "image"))
            self.assertEqual(command[command.index("--network") + 1], name)
            self.assertIn("net.ipv6.conf.all.disable_ipv6=1", command)
        with self.assertRaises(ValueError):
            DockerSandboxManager(network="host")
        for mode in ("host", "bridge", "container:other"):
            with self.assertRaises(SandboxError):
                DockerSandboxManager()._docker_command(("docker", "run", "--network", mode, "image"))


class NetworkEnforcementTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_network_never_starts_a_workload(self):
        manager = DockerSandboxManager()
        manager._run = AsyncMock(return_value=SandboxResult("", "not found", 1))
        manager._run_process = AsyncMock()
        with self.assertRaisesRegex(SandboxError, "不会退回"):
            await manager._run_bytes("docker", "run", "image", timeout=5)
        manager._run_process.assert_not_awaited()

    async def test_existing_unrestricted_workspace_cannot_execute(self):
        manager = DockerSandboxManager()
        manager._run = AsyncMock(return_value=SandboxResult("bridge", "", 0))
        with self.assertRaisesRegex(SandboxError, "重建"):
            await manager._network_mode("qqbot-test")

    async def test_valid_policy_checked_before_helper_or_exec(self):
        manager = DockerSandboxManager()
        manager._run = AsyncMock(return_value=SandboxResult(json.dumps(inspected()), "", 0))
        manager._run_process = AsyncMock(return_value=(b"", b"", 0))
        for command in (("docker", "run", "image"), ("docker", "exec", "sandbox", "true")):
            await manager._run_bytes(*command, timeout=5)
        self.assertEqual(manager._run.await_count, 2)
        self.assertEqual(manager._run_process.await_count, 2)

    async def test_network_none_helpers_need_no_firewall_setup(self):
        manager = DockerSandboxManager()
        manager._run = AsyncMock()
        manager._run_process = AsyncMock(return_value=(b"", b"", 0))
        await manager._run_bytes("docker", "run", "--network", "none", "image", timeout=5)
        manager._run.assert_not_awaited()


class DesktopPolicyInstallerTests(unittest.TestCase):
    def test_fixed_helper_has_no_user_input_or_host_filesystem(self):
        from src.local_runtime.desktop_sandbox import ensure_desktop_network
        deployment = {"checksum": "a" * 64}
        identity = "sha256:" + "b" * 64
        def docker(*args, **kwargs):
            if args[:2] == ("volume", "ls"):
                return ""
            if args[:2] == ("volume", "inspect"):
                return json.dumps([{"Labels": {"gaoji.sandbox.network-policy": "1"}}])
            return ""
        with patch("src.local_runtime.desktop_updates.image", return_value=("fixed", identity)), \
                patch("src.local_runtime.desktop_updates.docker", side_effect=docker) as cli:
            ensure_desktop_network(deployment)
        command = cli.call_args.args
        self.assertIn(identity, command)
        self.assertEqual(command[command.index("--cap-add") + 1], "NET_ADMIN")
        self.assertNotIn("--privileged", command)
        self.assertIn("src.sandbox_runtime.network", command)
        self.assertTrue(all("/Users/" not in value and "/data" not in value for value in command))

    def test_foreign_policy_volume_stops_before_running_helper(self):
        from src.local_runtime.desktop_sandbox import ensure_desktop_network
        from src.local_runtime.setup_clients import SetupError
        with patch("src.local_runtime.desktop_updates.image", return_value=("fixed", "sha256:" + "b" * 64)), \
                patch("src.local_runtime.desktop_updates.docker", side_effect=["gaoji-sandbox-network-policy\n", '[{"Labels":{}}]']) as cli:
            with self.assertRaises(SetupError):
                ensure_desktop_network({"checksum": "a" * 64})
        self.assertFalse(any(call.args[0] == "run" for call in cli.call_args_list))


if __name__ == "__main__":
    unittest.main()
