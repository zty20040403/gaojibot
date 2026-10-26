"""Opt-in real Desktop upgrade, with explicitly selected cached base images."""
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tarfile
import unittest

ROOT = Path(__file__).parents[1]
OLD = os.environ.get("GAOJI_TEST_DESKTOP_BASE", "")


@unittest.skipUnless(OLD, "Explicit local Desktop base digest required")
class DesktopUpdateLinuxTests(unittest.TestCase):
    driver = "qa-desktop-updates.py"
    evidence_name = "local-updates-linux.json"
    flow_timeout = 1000
    def command(self, args, *, data=None, timeout=120, check=True):
        result = subprocess.run([*self.docker, *args], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, env=self.env)
        if check and result.returncode:
            self.fail(result.stderr.decode()[-3000:])
        return result

    def test_real_worker_upgrade_and_rollback(self):
        self.assertRegex(OLD, r"^[a-f0-9]{64}$")
        host = os.environ["GAOJI_TEST_DOCKER_SOCKET"]
        self.assertTrue(host.startswith("unix:///") and Path(host[7:]).is_socket())
        self.docker = [os.environ["GAOJI_TEST_DOCKER"], "--host", host]
        self.env = {k:v for k,v in os.environ.items() if not k.startswith(("DOCKER_", "COMPOSE_"))}
        info = json.loads(self.command(["info", "--format", "{{json .}}"]).stdout)
        self.assertIn("docker desktop", info["OperatingSystem"].lower())
        job, new = secrets.token_hex(16), hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        label = "gaoji.desktop.job=" + job
        volume, network = "gaoji-desktop-" + job + "-data", "gaoji-desktop-" + job
        images = []
        recipe_files = {name: ROOT / "packaging/sandbox" / name for name in ("default.nix", "build.sh", "builder.lock.json")}
        recipe_files["image.nix"] = ROOT / "nix/sandbox-image.nix"
        recipe_hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in recipe_files.items()}
        recipe_id = hashlib.sha256(json.dumps(recipe_hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        original_sandbox_images = set(self.command(["image", "ls", "--format", "{{.Repository}}:{{.Tag}}", "gaoji-app-sandbox"]).stdout.decode().split())
        manifest = json.loads(self.command(["run", "--rm", "--network", "none", "--entrypoint", "cat",
            "gaoji-desktop-controller:" + OLD, "/opt/gaoji/manifest.json"]).stdout)
        entries = {item["path"]: item for item in manifest["files"]}
        sources = [path for folder in ("src/local_runtime", "src/sandbox_runtime") for path in (ROOT / folder).rglob("*.py")]
        for path in sources:
            content = path.read_bytes()
            name = path.relative_to(ROOT).as_posix()
            entries[name] = {"path": name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        manifest["files"] = list(entries.values())
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        try:
            for prefix in ("gaoji-app-runtime", "gaoji-desktop-controller"):
                tag = prefix + ":" + new
                buffer = io.BytesIO()
                with tarfile.open(fileobj=buffer, mode="w") as archive:
                    content = f"FROM {prefix}:{OLD}\nCOPY src /opt/gaoji/src\nCOPY sandbox /opt/gaoji/sandbox\nCOPY manifest.json /opt/gaoji/manifest.json\n".encode()
                    item = tarfile.TarInfo("Dockerfile"); item.size = len(content); item.mode = 0o644
                    archive.addfile(item, io.BytesIO(content))
                    content = json.dumps(manifest).encode()
                    item = tarfile.TarInfo("manifest.json"); item.size = len(content); item.mode = 0o644
                    archive.addfile(item, io.BytesIO(content))
                    for path in sources:
                        archive.add(path, arcname=path.relative_to(ROOT).as_posix(), recursive=False)
                    for name, path in recipe_files.items():
                        archive.add(path, arcname="sandbox/" + name, recursive=False)
                self.command(["build", "--network=none", "--label", "gaoji.desktop.runtime-digest=" + new,
                    "--label", label, "-t", tag, "-"], data=buffer.getvalue(), timeout=180)
                images.append(tag)
            self.command(["volume", "create", "--label", label, volume])
            self.command(["network", "create", "--label", label, "--internal", network])
            result = self.command(["run", "--name", "gaoji-upgrade-qa-" + job, "--label", label,
                "--network", network, "--memory", "512m", "--entrypoint", "python3",
                "--env", "QA_JOB=" + job, "--env", "QA_OLD=" + OLD, "--env", "QA_NEW=" + new, "--env", "QA_PORT=" + str(port),
                "--mount", "type=volume,source=" + volume + ",target=/var/lib/gaoji-app",
                "--mount", "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
                "--mount", "type=bind,source=" + str(ROOT / "desktop/tests" / self.driver) + ",target=/opt/gaoji/qa.py,readonly",
                images[1], "/opt/gaoji/qa.py"], timeout=self.flow_timeout, check=False)
            self.assertEqual(result.returncode, 0, result.stderr.decode()[-5000:] + result.stdout.decode()[-3000:])
            evidence = json.loads(result.stdout.decode().strip().splitlines()[-1])
            self.assertTrue(evidence["passed"])
            output = ROOT / "desktop/release/qa" / self.evidence_name
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(evidence, indent=2) + "\n")
        finally:
            # Read only this random fixture's metadata before deleting its data
            # volume, so workloads created by the runner are cleaned as well.
            if images:
                record = self.command(["run", "--rm", "--network", "none", "--entrypoint", "cat",
                    "--mount", "type=volume,source=" + volume + ",target=/fixture,readonly", images[-1],
                    "/fixture/jobs/" + job + ".json"], check=False)
                if record.returncode == 0:
                    namespace = json.loads(record.stdout)["instance_id"]
                    self.assertRegex(namespace, r"^[a-f0-9]{32}$")
                    owned = "gaoji.app.instance=" + namespace
                    for kind, listing in (("container", ["ps", "-aq"]), ("volume", ["volume", "ls", "-q"])):
                        for name in self.command([*listing, "--filter", "label=" + owned]).stdout.decode().split():
                            row = json.loads(self.command([kind, "inspect", name]).stdout)[0]
                            self.assertEqual((row.get("Config", row).get("Labels") or {}).get("gaoji.app.instance"), namespace)
                            self.command(["rm", "-f", name] if kind == "container" else ["volume", "rm", name])
                for name in self.command(["ps", "-a", "--format", "{{.Names}}", "--filter", "label=gaoji.app.image-build"]).stdout.decode().split():
                    if name.endswith("-" + job):
                        self.command(["rm", "-f", name])
            identifiers = self.command(["ps", "-aq", "--filter", "label=" + label]).stdout.decode().split()
            projects = set()
            for identifier in identifiers:
                row = json.loads(self.command(["container", "inspect", identifier]).stdout)[0]
                self.assertEqual(row["Config"]["Labels"].get("gaoji.desktop.job"), job)
                project = row["Config"]["Labels"].get("com.docker.compose.project", "")
                if project.startswith("gaoji-restore-"):
                    projects.add(project)
            if identifiers:
                self.command(["rm", "-f", *identifiers])
            for project in projects:
                for identifier in self.command(["network", "ls", "-q", "--filter", "label=com.docker.compose.project=" + project]).stdout.decode().split():
                    self.command(["network", "rm", identifier])
            for kind, target in (("network", network), ("volume", volume)):
                found = self.command([kind, "inspect", target], check=False)
                if found.returncode == 0:
                    self.assertEqual(json.loads(found.stdout)[0]["Labels"].get("gaoji.desktop.job"), job)
                    self.command([kind, "rm", target])
            for tag in images:
                self.command(["image", "rm", tag])
            if self.driver == "qa-desktop-sandbox.py":
                tag = "gaoji-app-sandbox:" + recipe_id
                if tag not in original_sandbox_images:
                    self.command(["image", "rm", tag], check=False)


if __name__ == "__main__":
    unittest.main()
