from __future__ import annotations

import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from xml.etree import ElementTree

import nonebot

os.environ.setdefault("AI_SUBAGENTS_ENABLED", "false")
nonebot.init()

from src.plugins.ai_chat.subagents import _single_observed_artifact
from src.plugins.ai_chat.vm_sandbox import VmSandboxManager
from src.sandbox_runtime.docker import SandboxError
from src.sandbox_runtime.project_vm_guest import HELPER_PATH
from tests.test_project_vm_guest import binding

DOMAIN_UUID = "22222222-2222-4222-8222-222222222222"

class ArtifactDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_project_domain_separates_data_disk_with_discard_and_identity(self):
        manager = VmSandboxManager(image="/base.qcow2", root="/vm")
        with patch("shutil.which", return_value="/test/qemu"):
            xml = manager._domain_xml("s123456", Path("/vm/s123456"), project_disk=binding(), project_domain_uuid=DOMAIN_UUID)
        root = ElementTree.fromstring(xml)
        disks = root.findall("./devices/disk")
        self.assertEqual(len(disks), 3)
        self.assertEqual(root.findtext("uuid"), DOMAIN_UUID)
        data = next(item for item in disks if item.find("target").get("dev") == "vdb")
        self.assertEqual(data.find("driver").get("discard"), "unmap")
        self.assertEqual(data.find("source").get("file"), str(binding().path))
        self.assertEqual(data.findtext("serial"), binding().guest_config()["serial"])
        self.assertEqual(root.find("./metadata/{urn:gaoji:project:1}workspace").get("generation"), "2")
        serial = root.find("./devices/serial")
        self.assertEqual(serial.get("type"), "pty")
        self.assertIsNone(serial.find("source"))
        with patch("shutil.which", return_value="/test/qemu"), self.assertRaises(SandboxError):
            manager._domain_xml("s654321", Path("/vm/s654321"), project_disk=binding(), project_domain_uuid=DOMAIN_UUID)
        with patch("shutil.which", return_value="/test/qemu"), self.assertRaises(SandboxError):
            manager._domain_xml("s123456", Path("/vm/s123456"), project_disk=binding())

    def test_nonproject_vm_keeps_existing_console_log(self):
        manager = VmSandboxManager(image="/base.qcow2", root="/vm")
        with patch("shutil.which", return_value="/test/qemu"):
            root = ElementTree.fromstring(manager._domain_xml("s123456", Path("/vm/s123456")))
        serial = root.find("./devices/serial")
        self.assertEqual(serial.get("type"), "file")
        self.assertEqual(serial.find("source").get("path"), "/vm/s123456/console.log")

    def test_project_cloud_init_mounts_with_readonly_root_owned_helper(self):
        config = json.loads(VmSandboxManager._cloud_config(project_disk=binding(), initialize_project=True).split("\n", 1)[1])
        files = {item["path"]: item for item in config["write_files"]}
        self.assertEqual(files["/etc/gaoji-project.json"]["permissions"], "0600")
        self.assertEqual(files[HELPER_PATH]["owner"], "root:root")
        compile(files[HELPER_PATH]["content"], HELPER_PATH, "exec")
        gate = files["/usr/local/lib/gaoji/project_gate.py"]
        self.assertEqual(gate["owner"], "root:root")
        self.assertEqual(gate["permissions"], "0755")
        compile(gate["content"], gate["path"], "exec")
        resize = files["/usr/local/lib/gaoji/project_resize.py"]
        self.assertEqual((resize["owner"], resize["permissions"]), ("root:root", "0700"))
        compile(resize["content"], resize["path"], "exec")
        self.assertEqual(json.loads(files["/etc/gaoji-project.json"]["content"]), binding().guest_config(initialize=True))
        self.assertEqual(config["users"], [{"name": "sandbox", "uid": 1000, "shell": "/bin/bash", "lock_passwd": True, "sudo": False}])
        self.assertNotIn(["chown", "sandbox:sandbox", "/workspace"], config["runcmd"])
        self.assertIn(["systemctl", "enable", "--now", "gaoji-project-workspace.service"], config["runcmd"])
        with self.assertRaises(SandboxError):
            VmSandboxManager._cloud_config(initialize_project=True)

    async def test_project_commands_stop_before_user_code_when_mount_is_missing(self):
        temporary = tempfile.TemporaryDirectory(prefix="gaoji-vm-admission-")
        self.addCleanup(temporary.cleanup)
        manager = VmSandboxManager(image="/base.qcow2", root=temporary.name)
        config = binding().guest_config()
        config.pop("initialize")
        manager._metadata = AsyncMock(return_value={"purpose": "project", "project": config})
        manager._running = AsyncMock(return_value=True)
        manager._guest_exec = AsyncMock(return_value=(1, b"", b"not mounted"))
        with self.assertRaisesRegex(SandboxError, "适配器"):
            await manager.exec("scope", "s123456", "echo should-not-run")
        self.assertEqual(manager._guest_exec.await_count, 0)
        self.assertEqual(manager._active_execs, {})

    async def test_project_guard_checks_identity_and_rejects_false_success(self):
        manager = VmSandboxManager(image="/base.qcow2", root="/vm")
        config = binding().guest_config()
        config.pop("initialize")
        metadata = {"purpose": "project", "project": config}
        observed = {**config, "verified": True, "mounted": True, "used_bytes": 1234, "initialization_sealed": True}
        manager._guest_exec = AsyncMock(return_value=(0, json.dumps(observed).encode(), b""))
        self.assertEqual(await manager._verify_project_mount("s123456", metadata), observed)
        for change in ({"project_id": 10}, {"generation": 1}, {"generation": True}, {"mounted": False},
                       {"filesystem_uuid": "wrong"}, {"used_bytes": -1}, {"verified": 1}, {"initialization_sealed": False}):
            manager._guest_exec.return_value = (0, json.dumps({**observed, **change}).encode(), b"")
            with self.subTest(change=change), self.assertRaises(SandboxError):
                await manager._verify_project_mount("s123456", metadata)

    async def test_generic_lifecycle_does_not_reclaim_or_restart_projects(self):
        manager = VmSandboxManager(image="/base.qcow2", root="/vm")
        manager._metadata = AsyncMock(return_value={"purpose": "project"})
        manager._running = AsyncMock(return_value=False)
        manager._virsh = AsyncMock()
        for method in (manager.start_owned, manager.stop_owned, manager.destroy):
            with self.subTest(method=method.__name__), self.assertRaises(SandboxError):
                await method("scope", "s123456")
        manager._virsh.assert_not_awaited()

    async def test_project_creation_cannot_fall_back_to_ephemeral_task(self):
        manager = VmSandboxManager(image="/unused/image", root="/unused/root")
        manager._all_metadata = AsyncMock()
        with self.assertRaisesRegex(SandboxError, "持久项目"):
            await manager.create("scope", purpose="project")
        manager._all_metadata.assert_not_awaited()

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
            invocation = manager._guest_exec.await_args_list[1].args[2]
            self.assertIn("/usr/bin/prlimit", invocation)
            self.assertIn("--nproc=256:256", invocation)

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
