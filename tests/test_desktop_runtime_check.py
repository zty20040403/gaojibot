from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

from src.local_runtime.runtime_check import ROOT_FILES, verify_sources


class ControllerImportsTests(unittest.TestCase):
    def test_controller_recipe_includes_transitive_stdlib_modules(self):
        source = Path(__file__).resolve().parents[1]
        recipe = (source / "packaging/server/Desktop.Dockerfile").read_text()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for line in recipe.splitlines():
                if not line.startswith("COPY src/"):
                    continue
                _, *inputs, destination = shlex.split(line)
                target = root / destination
                for item in inputs:
                    path = source / item
                    if path.is_dir():
                        shutil.copytree(path, target, ignore=shutil.ignore_patterns("__pycache__"))
                    else:
                        target.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(path, target / path.name)
            result = subprocess.run(
                [sys.executable, "-I", "-S", "-B", "-c",
                 "import sys; sys.path.insert(0, '.'); import src.local_runtime.desktop; import src.sandbox_runtime.server"],
                cwd=root, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


class RuntimeManifestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.files = []
        for name in ROOT_FILES | {"src/__init__.py", "src/local_runtime/entrypoint.py", "migrations/env.py",
                "src/plugins/ai_chat/admin_ui_dist/app.js", "src/plugins/ai_chat/admin_ui_dist/app.css"}:
            content = b"# fixture\n"
            filename = self.root / name
            filename.parent.mkdir(parents=True, exist_ok=True)
            filename.write_bytes(content)
            self.files.append({"path": name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()})

    def write_manifest(self, extras=()):
        files = self.files + [{"path": name, "bytes": 0, "sha256": hashlib.sha256(b"").hexdigest()} for name in extras]
        (self.root / "manifest.json").write_text(json.dumps({"schema": 1, "files": files}))

    def test_host_only_build_recipes_are_not_required_inside_runtime_image(self):
        self.write_manifest(["Dockerfile", "Desktop.Dockerfile", "images.lock.json", "applications.json", "sandbox/default.nix", "sandbox/build.sh",
            "sandbox/builder.lock.json", "sandbox/image.nix", "vendor/whisper.cpp-v1.9.4.tar.gz"])
        self.assertEqual(verify_sources(self.root), len(self.files))

    def test_unknown_recipe_and_traversal_are_still_rejected(self):
        for name in ("applications-other.json", "sandbox/unexpected.nix", "vendor/unexpected.tar.gz", "sandbox/../escape", "../escape"):
            with self.subTest(name=name):
                self.write_manifest([name])
                with self.assertRaises(ValueError):
                    verify_sources(self.root)

    def test_runtime_sources_still_require_exact_hash_and_size(self):
        self.write_manifest(["sandbox/build.sh"])
        (self.root / "src/__init__.py").write_text("modified = True\n")
        with self.assertRaisesRegex(ValueError, "verification failed"):
            verify_sources(self.root)


if __name__ == "__main__":
    unittest.main()
