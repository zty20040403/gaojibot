"""Opt-in checks against a local Linux Docker engine, never a user's server."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time
import unittest

from src.local_runtime.files import write_json
from src.local_runtime.instances import InstanceStore
from src.local_runtime.runtime import compose_document, runtime_environment
from src.local_runtime.video_assets import MODEL_BYTES, MODEL_FILE, MODEL_SHA256


ROOT = Path(__file__).parents[1]
IMAGE = os.environ.get("GAOJI_TEST_RUNTIME_IMAGE", "")


@unittest.skipUnless(IMAGE, "Set GAOJI_TEST_RUNTIME_IMAGE for explicit local-container acceptance")
class LinuxRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        socket = os.environ.get("GAOJI_TEST_DOCKER_SOCKET", "")
        if not socket.startswith("unix:///") or not Path(socket.removeprefix("unix://")).is_socket():
            raise ValueError("An existing local Unix Docker socket is required; remote engines are prohibited")
        if not re.fullmatch(r"gaoji-app-local-qa:[a-z0-9-]+", IMAGE):
            raise ValueError("Use a dedicated gaoji-app-local-qa image, not an existing deployment")
        cls.docker = [os.environ.get("GAOJI_TEST_DOCKER", "docker"), "--host", socket]
        info = json.loads(cls.command(["image", "inspect", IMAGE]))[0]
        if (info["Architecture"], info["Os"]) != ("amd64", "linux"):
            raise ValueError("Acceptance requires the actual Linux x64 image")
        cls.image = info["Id"]

    @classmethod
    def command(cls, arguments, *, payload=None, timeout=180, check=True):
        result = subprocess.run([*cls.docker, *arguments], input=json.dumps(payload) if payload is not None else None,
            capture_output=True, text=True, timeout=timeout)
        if check and result.returncode:
            raise AssertionError(f"Local Docker operation failed ({result.returncode}): {result.stderr[-4000:]}")
        return result.stdout if check else result

    def runtime(self, command, *extra):
        name = "gaoji-local-qa-" + secrets.token_hex(16)
        try:
            return json.loads(self.command(["run", "--rm", "--name", name, "--platform", "linux/amd64", "--network", "none", "--read-only",
                "--user", "10001:10001", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                "--memory", "1g", "--cpus", "1", "--pids-limit", "256", "--shm-size", "128m",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m,mode=1777", "--env", "HOME=/tmp",
                *extra, self.image, command], timeout=180))
        finally:
            self.command(["rm", "--force", name], timeout=30, check=False)

    def test_offline_nonroot_runtime_dependencies_and_chinese_browser_rendering(self):
        result = self.runtime("check-runtime")
        self.assertGreater(result["sources_checked"], 200)
        self.assertEqual(result["runtime_uid"], 10001)
        self.assertEqual(result["browser"], "ok")
        self.assertEqual(result["chinese_font"], "ok")

    def test_readonly_video_model_actually_loads_in_linux_container(self):
        filename = os.environ.get("GAOJI_TEST_VIDEO_MODEL", "")
        if not filename:
            self.skipTest("Set GAOJI_TEST_VIDEO_MODEL to an already downloaded public Whisper Base model")
        model = Path(filename)
        if model.is_symlink() or not model.is_file() or model.stat().st_size != MODEL_BYTES:
            self.fail("The public model file is missing or invalid")
        if any(char in str(model) for char in ",\n\r"):
            self.fail("Model path contains unsupported mount characters")
        result = self.runtime("check-video", "--mount", f"type=bind,source={model.absolute()},target=/opt/gaoji-models/{MODEL_FILE},readonly")
        self.assertTrue(result["video_ready"])
        self.assertEqual(result["sha256"], MODEL_SHA256)

    def test_pgvector_migration_admin_idempotency_and_database_restart(self):
        with tempfile.TemporaryDirectory(prefix="gaoji-linux-qa-") as temporary:
            root = Path(temporary)
            store = InstanceStore(root)
            config = store.create("Local acceptance")
            directory = store.directory(config["id"])
            _, private = store.snapshot(config["id"])
            write_json(directory / "config/runtime.json", {"schema": 1, "environment": runtime_environment(config, private, configured=False)})
            (directory / "config/postgres.password").write_text(private["database_password"])
            (directory / "config/admin.secret").write_bytes(secrets.token_bytes(32))
            for name in ("runtime.json", "postgres.password", "admin.secret"):
                # Only fixed test credentials in a private temporary parent. The
                # container's unprivileged UID must be able to read each bind.
                (directory / "config" / name).chmod(0o444)
            images = json.loads((ROOT / "packaging/server/images.lock.json").read_text())
            document = compose_document(config, directory, runtime_image="gaoji-app-runtime:" + "0" * 64, images=images)
            document["name"] = "gaoji-local-qa-" + config["id"]
            document["services"].pop("napcat")
            database, bot = document["services"]["database"], document["services"]["bot"]
            database["volumes"][0] = {"type": "volume", "source": "database", "target": "/var/lib/postgresql/data"}
            bot["image"] = self.image
            bot["ports"] = []
            bot["volumes"] = [item for item in bot["volumes"] if item["target"].startswith("/run/gaoji/")]
            bot["tmpfs"].extend(f"/data/{name}:rw,nosuid,nodev,size=64m,uid=10001,gid=10001" for name in ("state", "cache", "outbox"))
            for service in (database, bot):
                service["platform"] = "linux/amd64"
                service["restart"] = "no"
            document["networks"] = {"default": {"internal": True}}
            document["volumes"] = {"database": {}}
            compose = directory / "qa-compose.json"
            write_json(compose, document)
            prefix = ["compose", "--file", str(compose)]
            try:
                self.command([*prefix, "up", "--detach", "--wait", "--wait-timeout", "120", "database"], timeout=600)
                self.command([*prefix, "run", "--rm", "--no-deps", "-T", "bot", "migrate"], timeout=600)
                ready = json.loads(self.command([*prefix, "run", "--rm", "--no-deps", "-T", "bot", "check-database"]))
                self.assertTrue(ready["database_ready"])
                request = {"username": "acceptance", "password": secrets.token_urlsafe(24), "qq_id": "12345678"}
                for created in (True, False):
                    result = json.loads(self.command([*prefix, "run", "--rm", "--no-deps", "-T", "bot", "ensure-admin"], payload=request))
                    self.assertEqual(result["administrator_created"], created)
                rejected = self.command([*prefix, "run", "--rm", "--no-deps", "-T", "bot", "ensure-admin"],
                    payload={**request, "password": "different-test-password"}, check=False)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertNotIn(request["password"], rejected.stderr)
                self.command([*prefix, "restart", "database"])
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    probe = self.command([*prefix, "exec", "-T", "database", "pg_isready", "-U", "gaoji", "-d", "gaoji"], check=False)
                    if probe.returncode == 0:
                        break
                    time.sleep(1)
                else:
                    self.fail("Test PostgreSQL did not return after restart")
                result = json.loads(self.command([*prefix, "run", "--rm", "--no-deps", "-T", "bot", "ensure-admin"], payload=request))
                self.assertFalse(result["administrator_created"])
            finally:
                # Only this unique Compose project's containers/network/volume.
                # Never prune the engine or remove any existing user image.
                self.command([*prefix, "down", "--volumes", "--timeout", "15"], timeout=90)


if __name__ == "__main__":
    unittest.main()
