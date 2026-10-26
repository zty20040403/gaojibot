from __future__ import annotations

import os
import json
import tempfile
import unittest
from unittest.mock import AsyncMock

import nonebot

os.environ.setdefault("AI_ALLOW_LEGACY_SQLITE", "true")
os.environ.setdefault("AI_SUBAGENTS_ENABLED", "false")
nonebot.init()

from src.plugins.ai_chat.subagents import _single_observed_artifact
from src.plugins.ai_chat.vm_sandbox import VmSandboxManager


class ArtifactDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_vm_provisions_the_shared_cjk_pdf_helper(self):
        config = json.loads(VmSandboxManager._cloud_config().split("\n", 1)[1])
        for package in ("python3-reportlab", "python3-pypdf", "fonts-wqy-microhei", "poppler-utils"):
            self.assertIn(package, config["packages"])
        helper = config["write_files"][0]
        self.assertEqual(helper["path"], "/usr/local/bin/gaoji-pdf")
        self.assertEqual(helper["owner"], "root:root")
        self.assertEqual(helper["permissions"], "0755")
        compile(helper["content"], "gaoji-pdf", "exec")

    async def test_vm_exec_reports_created_workspace_files(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = VmSandboxManager(image=f"{directory}/image", root=directory)
            manager._owned_running = AsyncMock(return_value={})
            manager._guest_exec = AsyncMock(side_effect=[
                (0, b"", b""),
                (0, b"built", b""),
                (0, b"/workspace/report.pdf\n/workspace/input.md\n", b""),
                (0, b"", b""),
            ])
            result = await manager.exec("owner", "s183bee", "gaoji-pdf input.md report.pdf")
            self.assertEqual(result.manifest.changed_workspace_paths, ("report.pdf", "input.md"))
            self.assertEqual(result.stdout, "built")

    def test_recovers_only_one_observed_pdf_in_same_run(self):
        evidence = [{
            "run_id": 315,
            "tool_name": "sandbox_exec",
            "arguments": {"sandbox_id": "s183bee"},
            "payload": {"observed_manifest": {
                "changed_workspace_paths": ["report.pdf", "input.md"],
            }},
        }]
        self.assertEqual(_single_observed_artifact(evidence, 315, "做一个 PDF 发到群里"), {
            "handle": "s183bee:/workspace/report.pdf", "kind": "file", "name": "report.pdf",
        })
        self.assertIsNone(_single_observed_artifact(evidence, 316, "做一个 PDF 发到群里"))
        evidence[0]["payload"]["observed_manifest"]["changed_workspace_paths"].append("draft.pdf")
        self.assertIsNone(_single_observed_artifact(evidence, 315, "做一个 PDF 发到群里"))


if __name__ == "__main__":
    unittest.main()
