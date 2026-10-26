"""Opt-in App image/RPC/toolchain test, restricted to a local Docker socket."""
from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import unittest
import uuid
from unittest.mock import patch

from src.sandbox_runtime.client import SandboxClient
from src.sandbox_runtime.docker import DockerSandboxManager
from src.sandbox_runtime.server import SandboxRunner


DOCKER = os.environ.get("GAOJI_TEST_DOCKER", "")
SOCKET = os.environ.get("GAOJI_TEST_DOCKER_SOCKET", "")
IMAGE = os.environ.get("GAOJI_TEST_SANDBOX_IMAGE", "")


@unittest.skipUnless(DOCKER and SOCKET and IMAGE, "Explicit local App sandbox image required")
class ToolchainTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def cli(*arguments):
        result = subprocess.run([DOCKER, "--host", SOCKET, *arguments], capture_output=True,
                                text=True, timeout=90)
        if result.returncode:
            raise AssertionError(result.stderr[-2000:])
        return result.stdout.strip()

    async def asyncSetUp(self):
        if (not Path(DOCKER).is_absolute() or not SOCKET.startswith("unix:///")
                or not Path(SOCKET[7:]).is_socket() or not re.fullmatch(r"gaoji-app-sandbox:[a-f0-9]{64}", IMAGE)):
            raise ValueError("An explicit local Docker socket and App content tag are required")
        image = json.loads(self.cli("image", "inspect", "--format", "{{json .}}", IMAGE))
        self.assertEqual((image["Os"], image["Architecture"], image["Config"]["User"]), ("linux", "amd64", "1000:1000"))
        self.assertEqual(image["Config"]["Labels"]["io.gaoji.app.recipe"], IMAGE.split(":")[1])
        platform = patch.dict(os.environ, {"DOCKER_DEFAULT_PLATFORM": "linux/amd64"})
        platform.start()
        self.addCleanup(platform.stop)
        temporary = tempfile.TemporaryDirectory(prefix="gj-tools-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manager = DockerSandboxManager(namespace=uuid.uuid4().hex, image=IMAGE,
            docker_executable=DOCKER, docker_host=SOCKET, memory_mib=1024,
            cpus=1, pids_limit=256, max_file_bytes=4 * 1024**2)
        self.addAsyncCleanup(self.clean_fixtures)
        self.token = secrets.token_urlsafe(32)
        await self.start_runner()
        self.addAsyncCleanup(self.close_runner)
        self.client = SandboxClient(str(self.root / "socket/run.sock"), self.token, max_file_bytes=4 * 1024**2)

    async def start_runner(self):
        self.runner = SandboxRunner(self.manager, socket_path=self.root / "socket/run.sock",
            state_directory=self.root / "state", token=self.token)
        await self.runner.start()

    async def close_runner(self):
        await asyncio.wait_for(self.runner.close(), 15)

    async def clean_fixtures(self):
        manager = self.manager
        listing = await asyncio.to_thread(self.cli, "ps", "-a", "--no-trunc", "--filter",
            "label=gaoji.app.instance=" + manager.namespace, "--format", "{{.ID}}")
        for identifier in listing.splitlines():
            item = json.loads(await asyncio.to_thread(self.cli, "inspect", "--format", "{{json .}}", identifier))
            self.assertEqual(item["Config"]["Labels"]["gaoji.app.instance"], manager.namespace)
            self.assertTrue(item["Name"].startswith("/gaoji-app-" + manager.namespace + "-"))
            await asyncio.to_thread(self.cli, "rm", "-f", identifier)
        volumes = await asyncio.to_thread(self.cli, "volume", "ls", "--quiet", "--filter",
            "label=gaoji.app.instance=" + manager.namespace)
        for name in volumes.splitlines():
            labels = json.loads(await asyncio.to_thread(self.cli, "volume", "inspect", "--format", "{{json .Labels}}", name))
            self.assertEqual(labels["gaoji.app.instance"], manager.namespace)
            await asyncio.to_thread(self.cli, "volume", "rm", name)

    async def test_rpc_creates_executes_installs_exports_and_replays_after_restart(self):
        from pypdf import PdfReader

        owner = "group:local-test:user:local-test"
        created = await self.client.create(owner)
        sandbox = created["sandbox_id"]
        basic = await self.client.exec(owner, sandbox,
            "python -c 'import os,docx,openpyxl,fitz; assert os.getuid()==1000; print(\"python-ok\")'", 60)
        self.assertEqual(basic.returncode, 0, basic.stderr)
        self.assertIn("python-ok", basic.stdout)
        node = await self.client.exec(owner, sandbox, "node -e 'console.log(6*7)'", 30)
        self.assertEqual((node.returncode, node.stdout.strip()), (0, "42"))
        package = await self.client.exec(owner, sandbox, "hello", 120, ["hello"])
        self.assertEqual(package.returncode, 0, package.stderr)
        self.assertIn("Hello", package.stdout)
        source = "# 中文验收报告\n\n沙盒生成文件并交付，不应出现方框。\n\n| 项目 | 结果 |\n| --- | --- |\n| 字体 | 正常 |\n"
        await self.client.write_file(owner, sandbox, "report.md", source.encode())
        document = await self.client.exec(owner, sandbox, "gaoji-pdf report.md report.pdf", 90)
        self.assertEqual(document.returncode, 0, document.stderr)
        pdf, archived = await self.client.export_artifact(owner, sandbox, "report.pdf")
        self.assertFalse(archived)
        self.assertTrue(pdf.startswith(b"%PDF-"))
        reader = PdfReader(io.BytesIO(pdf))
        self.assertIn("中文验收报告", "".join(page.extract_text() for page in reader.pages))
        fonts = [font.get_object() for page in reader.pages for font in page["/Resources"]["/Font"].values()]
        self.assertTrue(any(font.get("/FontDescriptor") and
            font["/FontDescriptor"].get_object().get("/FontFile2") for font in fonts))
        output = os.environ.get("GAOJI_TEST_SANDBOX_PDF", "")
        if output:
            target = Path(output).absolute()
            if not target.parent.resolve().is_relative_to(Path("/private/tmp")):
                raise ValueError("QA PDF output is restricted to the local temporary directory")
            target.write_bytes(pdf)
        identifier = uuid.uuid4().hex
        arguments = {"owner": owner, "sandbox_id": sandbox,
            "command": "python -c 'from pathlib import Path; p=Path(\"once\"); p.write_text(p.read_text()+\"x\" if p.exists() else \"x\")'"}
        first = await self.client.request("exec", arguments, request_id=identifier)
        await self.close_runner()
        await self.start_runner()
        self.assertEqual(await self.client.request("exec", arguments, request_id=identifier), first)
        self.assertEqual(await self.client.read_file(owner, sandbox, "once"), b"x")
        await self.client.destroy(owner, sandbox)
        self.assertEqual(await self.client.list(owner), [])


if __name__ == "__main__":
    unittest.main()
