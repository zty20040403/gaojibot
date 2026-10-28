"""Opt-in real OCI pipe/process smoke; NOT full Nix/loop-quota acceptance.

Uses an already installed image with Python/Playwright/Chromium, no pulls, real
accounts, published ports or external network. The parent-project identity is
a fixture; the worker, browser, filesystem and Docker stop observation are real.
"""

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import uuid

from src.sandbox_runtime.docker import DockerSandboxManager
from src.sandbox_runtime.project_browser_bundle import build_bundle
from src.sandbox_runtime.project_browser_journal import ProjectBrowserJournal
from src.sandbox_runtime.project_browser_oci import OciProjectBrowser
from src.sandbox_runtime.project_workspaces import ProjectWorkspaces


@unittest.skipUnless(os.environ.get("GAOJI_TEST_OCI_BROWSER_IMAGE"), "needs an installed browser image")
class OciBrowserProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_pipes_replay_and_confirmed_container_shutdown(self):
        instance = uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix="gj-oci-process-") as temp:
            root = Path(temp).resolve()
            workspace = root / "workspace"
            workspace.mkdir(mode=0o777)
            workspace.chmod(0o777)
            manager = DockerSandboxManager(namespace=instance,
                image="gaoji-app-sandbox:" + "b" * 64, network="none",
                docker_host=os.environ["GAOJI_TEST_OCI_DOCKER_HOST"])
            image = os.environ["GAOJI_TEST_OCI_BROWSER_IMAGE"]
            python = os.environ.get("GAOJI_TEST_OCI_PYTHON", "/opt/gaoji/.venv/bin/python")
            chromium = os.environ.get("GAOJI_TEST_OCI_CHROMIUM", "/usr/bin/chromium")
            inspected = await manager._run("docker", "image", "inspect", image, timeout=20)
            self.assertEqual(inspected.returncode, 0, inspected.stderr)
            image = json.loads(inspected.stdout)[0]["Id"]
            journal = ProjectBrowserJournal(root / "journal", instance)
            workspaces = SimpleNamespace(manager=manager, volumes=SimpleNamespace(browsers=journal),
                mount_spec=ProjectWorkspaces.mount_spec,
                _workspace_mount_matches=lambda *args: ProjectWorkspaces._workspace_mount_matches(None, *args))
            driver = OciProjectBrowser(workspaces)
            identity = dict(instance_id=instance, owner="fixture", project_id=1, generation=1, fence=1)
            runtime = dict(backend="oci", sandbox_id="s123abc", runtime_id="a" * 64)
            mount = f"type=bind,source={workspace},target=/workspace"
            # Docker Desktop's device/inode values differ from the Mac host.
            probe = await manager._run("docker", "run", "--pull=never", "--rm", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--memory", "256m",
                "--memory-swap", "256m", "--mount", mount, "--entrypoint", "/bin/sh", image,
                "-c", "stat -c '%d:%i' /workspace", timeout=30)
            self.assertEqual(probe.returncode, 0, probe.stderr)
            binding = {**identity, "path": str(workspace), "workspace": {
                "image": image, "root_identity": probe.stdout.strip()}}
            config = {"schema": 1, "binding": identity, "workspace": "/workspace",
                "executable_path": chromium, "allow_private_network": True,
                "max_profile_bytes": 256 * 1024**2, "cache_bytes": 32 * 1024**2}
            spec = None

            async def launch(record):
                nonlocal spec
                spec = driver._spec(record, binding, python, build_bundle())
                driver._save(record, spec)
                result = await manager._run(*driver._create_command(spec), timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                row = await driver._inspect(spec["name"])
                driver._validate(spec, row)
                spec.update(container_id=row["Id"], phase="starting")
                driver._save(record, spec)
                return await driver._spawn(record, spec)

            try:
                record = await driver.host.open(config, runtime, launch)
                sid = record["session_id"]
                page = '<button onclick="this.textContent=String(Number(this.textContent)+1)">0</button>'
                server = ("from http.server import HTTPServer,BaseHTTPRequestHandler\n"
                    "class H(BaseHTTPRequestHandler):\n"
                    " def do_GET(self):\n"
                    f"  body={page.encode()!r}; self.send_response(200); self.end_headers(); self.wfile.write(body)\n"
                    " def log_message(self,*a): pass\n"
                    "HTTPServer(('127.0.0.1',8765),H).serve_forever()\n")
                served = await manager._run("docker", "exec", "-d", spec["container_id"],
                                           python, "-I", "-c", server, timeout=20)
                self.assertEqual(served.returncode, 0, served.stderr)
                ready = await manager._run("docker", "exec", spec["container_id"], python, "-I", "-c",
                    "import time,urllib.request\nfor i in range(50):\n try:\n"
                    "  urllib.request.urlopen('http://127.0.0.1:8765',timeout=1).close(); break\n"
                    " except OSError: time.sleep(.1)\nelse: raise SystemExit(1)", timeout=15)
                self.assertEqual(ready.returncode, 0, ready.stderr)
                published = {}

                async def persist(session, sequence, reply):
                    self.assertEqual(published.setdefault((session, sequence), deepcopy(reply)), reply)

                async def call(sequence, method, args):
                    reply = await driver.host.execute(identity, sid, sequence, method, args, persist=persist)
                    self.assertEqual(reply["state"], "completed", reply)
                    return reply

                initial = await call(1, "browser_navigate", {"url": "http://127.0.0.1:8765"})
                self.assertIn('0', json.dumps(initial["result"]))
                clicked = await call(2, "browser_click", {"ref": "b1"})
                self.assertEqual(clicked, await call(2, "browser_click", {"ref": "b1"}))
                snapshot = await call(3, "browser_snapshot", {})
                self.assertIn('"1"', json.dumps(snapshot["result"]))
                await driver.drain(identity, sid)
                self.assertIsNone(await driver._inspect(spec["name"]))
                self.assertTrue(journal.maintenance_ready(identity))
                self.assertEqual(driver.processes, {})
                self.assertTrue((workspace / ".gaoji/browser").is_dir())
            finally:
                if spec:
                    # Test owns a fresh random name, never a user container.
                    await manager._run("docker", "rm", "-f", spec["name"], timeout=20)
                for process in driver.processes.values():
                    await manager._terminate_process(process)
                await manager._run("docker", "volume", "rm", manager.nix_cache_volume, timeout=20)


if __name__ == "__main__":
    unittest.main()
