"""Exercise the VM agent bridge with real loopback sockets and child processes."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

BRIDGE = Path(__file__).resolve().parents[1] / 'src/plugins/ai_chat/sandbox_agent_bridge.py'


class SandboxAgentBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.peer = asyncio.get_running_loop().create_future()
        self.release = asyncio.Event()
        async def connected(reader, writer):
            token = await reader.readline()
            if not self.peer.done():
                self.peer.set_result((reader, writer, token))
            await self.release.wait()
            writer.close()
            await writer.wait_closed()
        self.server = await asyncio.start_server(connected, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.processes = []

    async def asyncTearDown(self):
        self.release.set()
        self.server.close()
        await self.server.wait_closed()
        for process in self.processes:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def start_bridge(self, source):
        child = Path(self.tmp.name) / 'fake_agent.py'
        child.write_text(source, encoding='utf-8')
        env = {**os.environ, 'GAOJI_JOB_TOKEN': 'TEST_TOKEN', 'GAOJI_PI_CWD': self.tmp.name,
               'GAOJI_AGENT_ERR': str(Path(self.tmp.name) / 'agent.err')}
        process = await asyncio.create_subprocess_exec(sys.executable, str(BRIDGE), '127.0.0.1', str(self.port), '--',
            sys.executable, str(child), cwd=self.tmp.name, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self.processes.append(process)
        return process

    async def test_authenticates_then_relays_and_closes_child(self):
        process = await self.start_bridge(
            "import json,sys\n"
            "sys.stderr.write('TEST child error\\n'); sys.stderr.flush()\n"
            "for line in sys.stdin:\n"
            "    message=json.loads(line); print(json.dumps({'echo':message['value']}),flush=True)\n")
        reader, writer, token = await asyncio.wait_for(self.peer, 3)
        self.assertEqual(token, b'TEST_TOKEN\n')
        writer.write(b'{"value":"hello"}\n')
        await writer.drain()
        self.assertEqual(json.loads(await asyncio.wait_for(reader.readline(), 3)), {'echo': 'hello'})
        writer.close()
        await writer.wait_closed()
        out, err = await asyncio.wait_for(process.communicate(), 3)
        self.assertEqual(process.returncode, 0)
        self.assertEqual((out, err), (b'', b''))
        self.assertIn('TEST child error', (Path(self.tmp.name) / 'agent.err').read_text())

    async def test_child_exit_closes_host_tunnel(self):
        process = await self.start_bridge("import sys\nsys.stdout.write('done\\n'); sys.stdout.flush()\n")
        reader, _writer, token = await asyncio.wait_for(self.peer, 3)
        self.assertEqual(token, b'TEST_TOKEN\n')
        self.assertEqual(await asyncio.wait_for(reader.readline(), 3), b'done\n')
        self.assertEqual(await asyncio.wait_for(reader.readline(), 3), b'')
        await asyncio.wait_for(process.wait(), 3)
        self.assertEqual(process.returncode, 0)
