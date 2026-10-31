import asyncio
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.plugins.ai_chat.vm_sandbox import VmSandboxManager
from src.sandbox_runtime import vm_network


class VmNetworkPolicyTests(unittest.TestCase):
    def test_rules_filter_only_the_vm_cgroup(self):
        rules = vm_network.render("/system.slice/TEST-vm.service", [18641, 18640],
                                  vm_network.nameservers("nameserver 127.0.0.53\n"))
        self.assertIn('socket cgroupv2 level 2 "system.slice/TEST-vm.service" jump guest', rules)
        self.assertIn("policy accept;", rules)
        guest = rules.split("chain guest", 1)[1].splitlines()
        accepts = [line.strip() for line in guest if line.strip().endswith("accept")]
        # Relay ports and the host resolver are the only host or private exceptions.
        self.assertEqual(accepts, [
            "ip daddr 127.0.0.1 tcp dport { 18640, 18641 } accept",
            "ip daddr { 127.0.0.53 } meta l4proto { tcp, udp } th dport 53 accept",
        ])
        rejects = "\n".join(line for line in guest if "reject" in line)
        self.assertIn("fib daddr type local reject", rejects)
        for cidr in ("10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
                     "172.16.0.0/12", "192.168.0.0/16", "fc00::/7", "fe80::/10"):
            self.assertIn(cidr, rejects)

    def test_nameservers_follow_resolv_conf(self):
        found = vm_network.nameservers(
            "# generated\nsearch TEST.example\nnameserver 10.0.2.3\nnameserver fe80::1%eth0\n")
        self.assertEqual([str(item) for item in found], ["10.0.2.3", "fe80::1"])
        rules = vm_network.render("/a/b.service", [18640], found)
        self.assertIn("ip6 daddr { fe80::1 } meta l4proto { tcp, udp } th dport 53 accept", rules)

    def test_rejects_unusable_cgroup_or_ports(self):
        for cgroup in ("", "/", '/a/b"; flush ruleset; "', "/a//b"):
            with self.assertRaises(ValueError):
                vm_network.render(cgroup, [18640])
        for ports in ([], [0], [70000], ["18640"]):
            with self.assertRaises(ValueError):
                vm_network.render("/a/b.service", ports)

    def test_provision_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            systemctl = self._script(state / "systemctl", f'echo "$@" >> "{root}/calls"\n'
                                     f'[ "$1" = show ] && cat "{root}/cgroup" || true')
            nft = self._script(state / "nft", f'cat > "{root}/rules"; exit "$(cat "{root}/nft-exit")"')
            (state / "resolv.conf").write_text("nameserver 127.0.0.53\n")
            (state / "nft-exit").write_text("0")
            options = dict(unit="TEST-vm.service", ports=[18640], nft=nft,
                           systemctl=systemctl, resolv_conf=str(state / "resolv.conf"))

            (state / "cgroup").write_text("")
            vm_network.provision(if_running=True, **options)  # stopped: nothing to protect
            with self.assertRaisesRegex(RuntimeError, "refusing to start VMs unfiltered"):
                vm_network.provision(if_running=False, **options)

            (state / "cgroup").write_text("/system.slice/TEST-vm.service\n")
            vm_network.provision(if_running=False, **options)
            self.assertIn('"system.slice/TEST-vm.service"', (state / "rules").read_text())

            (state / "nft-exit").write_text("1")
            with self.assertRaises(RuntimeError):
                vm_network.provision(if_running=True, **options)
            self.assertIn("stop --no-block TEST-vm.service", (state / "calls").read_text())

    @staticmethod
    def _script(path, body):
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)


class VmSandboxDaemonTests(unittest.IsolatedAsyncioTestCase):
    async def test_host_commands_never_autospawn_libvirt(self):
        captured = {}

        async def spawn(*args, env, **kwargs):
            captured.update(env)
            raise OSError("stop here")

        manager = VmSandboxManager(image="/TEST.qcow2", root="/nonexistent")
        with patch.object(asyncio, "create_subprocess_exec", spawn):
            with self.assertRaises(OSError):
                await manager._run("virsh", "-c", "qemu:///session", "list")
        self.assertEqual(captured["LIBVIRT_AUTOSTART"], "0")
        self.assertEqual(captured.get("PATH"), os.environ.get("PATH"))


if __name__ == "__main__":
    unittest.main()
