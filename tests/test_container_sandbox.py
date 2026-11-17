"""NixOS container backend: the root broker's checks and the manager's primitives."""
import asyncio
import json
import os
import stat
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.plugins.ai_chat.container_sandbox import ContainerSandboxManager
from src.plugins.ai_chat.sandbox import SandboxError
from src.sandbox_runtime import container_broker as broker

OPS = "qqo:conversation:" + "1" * 64


def script(directory, name, body):
    path = Path(directory) / name
    path.write_text("#!" + sys.executable + "\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


class BrokerParseTests(unittest.TestCase):
    def test_rejects_bad_ids_ops_and_argv(self):
        for header in ({"op": "exec", "sid": "s12345"}, {"op": "rm", "sid": "s123456"},
                       {"op": "status", "sid": "../s123456"}, {"op": "start", "sid": "s123456", "ops": "yes"},
                       {"op": "exec", "sid": "s123456", "argv": ["sh"]},
                       {"op": "exec", "sid": "s123456", "argv": ["/bin/sh", "a\0b"]},
                       {"op": "exec", "sid": "s123456", "argv": ["/bin/sh"], "timeout": 99999}):
            with self.assertRaises(broker.BrokerError):
                broker.parse(header)
        self.assertEqual(broker.parse({"op": "start", "sid": "s0a1b2c", "ops": True}),
                         {"op": "start", "sid": "s0a1b2c", "ops": True})


class BrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.state = self.dir / "state"; self.state.mkdir()
        self.log = self.dir / "log"
        systemctl = script(self.dir, "systemctl", f"""import sys,pathlib
log=pathlib.Path({str(self.log)!r}); active=pathlib.Path({str(self.dir)!r})/'active'
args=sys.argv[1:]; log.open('a').write(' '.join(args)+'\\n')
units=set(active.read_text().split()) if active.exists() else set()
if args[0]=='is-active':
    print('active' if args[1] in units else 'inactive'); sys.exit(0 if args[1] in units else 3)
if args[0]=='start': units.add(args[1])
if args[0]=='stop': units-=set(args[1:])
active.write_text(' '.join(units))
""")
        run = script(self.dir, "systemd-run", f"""import sys,subprocess,pathlib
pathlib.Path({str(self.log)!r}).open('a').write('run '+' '.join(sys.argv[1:sys.argv.index('--')])+'\\n')
sys.exit(subprocess.run(sys.argv[sys.argv.index('--')+1:]).returncode)
""")
        self.broker = broker.Broker(systemctl=systemctl, systemd_run=run, state=self.state)
        path = str(self.dir / "b.sock")
        self.server = await asyncio.start_unix_server(self.broker.serve_connection, path=path)
        self.addAsyncCleanup(self.close)
        self.manager = ContainerSandboxManager(root=str(self.dir / "meta"), broker=path,
                                               operations_owners=frozenset({OPS}))

    async def close(self):
        self.server.close()
        await self.server.wait_closed()

    async def test_exec_requires_running_container_and_returns_streams(self):
        with self.assertRaisesRegex(SandboxError, "container_not_running"):
            await self.manager._guest_exec_raw("s123456", "/bin/sh", ["-c", "true"])
        await self.manager._start("s123456")
        code, out, err = await self.manager._guest_exec_raw(
            "s123456", "/bin/sh", ["-c", "cat; echo E >&2; exit 3"], input_bytes=b"x" * (11 << 20))
        self.assertEqual((code, len(out), err), (3, 11 << 20, b"E\n"))
        self.assertIn("run --machine=gct-s123456", self.log.read_text())

    async def test_ops_start_switches_unit_and_remove_refuses_running(self):
        (self.dir / "meta" / "s111111").mkdir(parents=True)
        (self.dir / "meta" / "s111111" / "metadata.json").write_text(
            json.dumps({"id": "s111111", "owner_hash": self.manager._owner_hash(OPS)}))
        await self.manager._start("s111111")
        self.assertIn("start gaoji-ct-ops@s111111.service", self.log.read_text())
        (self.state / "s111111").mkdir()
        with self.assertRaisesRegex(SandboxError, "container_running"):
            await self.manager._broker({"op": "remove", "sid": "s111111"})
        await self.manager._virsh("destroy", "gaoji-vm-s111111")
        self.assertFalse(await self.manager._running("s111111"))
        await self.manager._broker({"op": "remove", "sid": "s111111"})
        self.assertFalse((self.state / "s111111").exists())

    async def test_bad_request_is_reported_not_crashing(self):
        with self.assertRaisesRegex(SandboxError, "invalid_sandbox_id"):
            await self.manager._broker({"op": "status", "sid": "nope"})
        self.assertTrue(await asyncio.wait_for(self.manager._running("s123456"), 10) is False)


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.manager = ContainerSandboxManager(root=directory.name, broker="/nonexistent")
        self.requests = []

        async def fake(request, stdin=b"", *, timeout=60):
            self.requests.append((request, stdin))
            return {"code": 0, "active": True}, b"", b""
        self.manager._broker = fake

    async def test_packages_are_refused_and_virsh_verbs_map(self):
        with self.assertRaisesRegex(SandboxError, "预装"):
            await self.manager.exec("TEST_OWNER", "s123456", "true", packages=["curl"])
        await self.manager._virsh("define", "/x/s123456/domain.xml")
        await self.manager._virsh("start", "gaoji-vm-s123456")
        self.assertEqual(self.requests[-1][0], {"op": "start", "sid": "s123456", "ops": False})
        with self.assertRaises(SandboxError):
            await self.manager._virsh("qemu-agent-command", "gaoji-vm-s123456")

    async def test_unreachable_broker_is_an_error(self):
        manager = ContainerSandboxManager(root=self.manager.root, broker=os.devnull + ".sock")
        with self.assertRaisesRegex(SandboxError, "容器管理服务不可用"):
            await manager._running("s123456")
