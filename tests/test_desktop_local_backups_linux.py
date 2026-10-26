"""Opt-in real Docker Desktop volume backup/restore, using only owned QA resources."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import unittest

ROOT = Path(__file__).parents[1]
RUNTIME = os.environ.get("GAOJI_TEST_DESKTOP_RUNTIME", "")
CONTROLLER = os.environ.get("GAOJI_TEST_DESKTOP_CONTROLLER", "")


@unittest.skipUnless(RUNTIME and CONTROLLER, "Explicit local Desktop images required")
class DesktopBackupLinuxTests(unittest.TestCase):
    def command(self, args, *, check=True, timeout=120):
        result = subprocess.run([*self.docker, *args], capture_output=True, text=True, timeout=timeout, env=self.env)
        if check and result.returncode:
            self.fail(f"QA Docker operation failed: {result.stderr[-2000:]}")
        return result

    def test_actual_named_volume_postgres_and_vector_restore(self):
        socket = os.environ.get("GAOJI_TEST_DOCKER_SOCKET", "")
        self.assertTrue(socket.startswith("unix:///") and Path(socket[7:]).is_socket())
        self.assertRegex(RUNTIME, r"^gaoji-app-runtime:[a-f0-9]{64}$")
        self.assertRegex(CONTROLLER, r"^gaoji-desktop-controller:[a-f0-9]{64}$")
        self.docker = [os.environ["GAOJI_TEST_DOCKER"], "--host", socket]
        self.env = {key: value for key, value in os.environ.items() if not key.startswith(("DOCKER_", "COMPOSE_"))}
        info = json.loads(self.command(["info", "--format", "{{json .}}"]).stdout)
        self.assertIn("docker desktop", info["OperatingSystem"].lower())
        controller = json.loads(self.command(["image", "inspect", CONTROLLER]).stdout)[0]
        runtime = json.loads(self.command(["image", "inspect", RUNTIME]).stdout)[0]
        self.assertEqual((controller["Os"], controller["Architecture"]), (runtime["Os"], runtime["Architecture"]))
        job, backup, restore = (secrets.token_hex(16) for _ in range(3))
        label = "gaoji.desktop.job=" + job
        volume, network = "gaoji-desktop-" + job + "-data", "gaoji-desktop-" + job
        name = "gaoji-desktop-backup-qa-" + job
        candidate_project = "gaoji-restore-" + restore
        try:
            self.command(["volume", "create", "--label", label, volume])
            self.command(["network", "create", "--label", label, "--internal", network])
            result = self.command(["run", "--name", name, "--label", label, "--network", "none",
                "--memory", "512m", "--cpus", "1", "--pids-limit", "256", "--entrypoint", "python3",
                "--env", "QA_JOB=" + job, "--env", "QA_BACKUP=" + backup, "--env", "QA_RESTORE=" + restore,
                "--env", "QA_RUNTIME_IMAGE=" + RUNTIME,
                "--mount", "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
                "--mount", "type=volume,source=" + volume + ",target=/var/lib/gaoji-app",
                "--mount", "type=bind,source=" + str(ROOT / "src") + ",target=/opt/gaoji/src,readonly",
                "--mount", "type=bind,source=" + str(ROOT / "desktop/tests/qa-desktop-backups.py") + ",target=/opt/gaoji/qa.py,readonly",
                controller["Id"], "/opt/gaoji/qa.py"], check=False, timeout=600)
            self.assertEqual(result.returncode, 0, result.stderr[-4000:] + result.stdout[-4000:])
            evidence = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertIs(evidence["passed"], True)
            evidence["controllerImageId"] = controller["Id"]
            evidence["runtimeImageId"] = runtime["Id"]
            output = ROOT / "desktop/release/qa/local-backups-linux.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(evidence, indent=2) + "\n")
        finally:
            containers = self.command(["ps", "-aq", "--filter", "label=" + label]).stdout.split()
            if containers:
                records = json.loads(self.command(["container", "inspect", *containers]).stdout)
                self.assertTrue(all(row["Config"]["Labels"].get("gaoji.desktop.job") == job for row in records))
                self.command(["rm", "-f", *containers])
            networks = self.command(["network", "ls", "-q", "--filter", "label=com.docker.compose.project=" + candidate_project]).stdout.split()
            for identifier in networks:
                row = json.loads(self.command(["network", "inspect", identifier]).stdout)[0]
                self.assertEqual(row["Labels"].get("com.docker.compose.project"), candidate_project)
                self.command(["network", "rm", identifier])
            for kind, target in (("network", network), ("volume", volume)):
                result = self.command([kind, "inspect", target], check=False)
                if result.returncode == 0:
                    self.assertEqual(json.loads(result.stdout)[0]["Labels"].get("gaoji.desktop.job"), job)
                    self.command([kind, "rm", target])


if __name__ == "__main__":
    unittest.main()
