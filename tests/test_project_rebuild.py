"""Local files and RPC; all mounts and containers are explicit fixtures."""

import hashlib
import json
import unittest
from unittest.mock import patch

from src.project_content import manifest_bytes
from src.sandbox_runtime.docker import SandboxError
from src.sandbox_runtime import project_rebuild
from tests import test_project_file_transfer as fixture


class ProjectRebuildTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ProjectFileTransferTests.asyncSetUp
    start_runner = fixture.ProjectFileTransferTests.start_runner
    stop_runner = fixture.ProjectFileTransferTests.stop_runner
    operate = fixture.ProjectFileTransferTests.operate
    journal = fixture.ProjectFileTransferTests.journal
    reserve = fixture.ProjectFileTransferTests.reserve
    call = fixture.ProjectFileTransferTests.call

    async def seed(self):
        await self.reserve()
        (self.path / "package.json").write_text('{"name":"fixture"}')
        (self.path / "package-lock.json").write_text(
            '{"lockfileVersion":3,"packages":{}}'
        )
        (self.path / "src").mkdir()
        (self.path / "src/main.js").write_text("retained source")
        (self.path / "node_modules/pkg").mkdir(parents=True)
        (self.path / "node_modules/pkg/a").write_bytes(b"rebuildable dependency")

    async def snapshot(self, command="npm ci"):
        result, _ = await self.call("snapshot", rebuild_command=command)
        page, _ = await self.call(
            "page", index=0, manifest_sha256=result["manifest_sha256"]
        )
        manifest = {"format": result["format"], "files": page["files"]}
        if result["rebuild"]:
            manifest["rebuild"] = result["rebuild"]
        self.assertEqual(
            hashlib.sha256(
                manifest_bytes(manifest, quota_bytes=64 * 1024**2)
            ).hexdigest(),
            result["manifest_sha256"],
        )
        return result, manifest

    async def prune(self, digest, action="prune", **kwargs):
        return await self.client.project_files(
            action,
            "scope",
            1,
            generation=1,
            fence=5,
            reservation="a" * 32,
            manifest_sha256=digest,
            snapshot_fence=4,
            **kwargs,
        )

    async def test_npm_snapshot_omits_dependencies_and_pins_complete_recipe(self):
        await self.seed()
        result, manifest = await self.snapshot()
        self.assertEqual(result["warnings"], [])
        self.assertEqual(manifest["rebuild"]["command"], "npm ci")
        self.assertEqual(manifest["rebuild"]["excluded"], ["node_modules"])
        self.assertEqual(
            [item["path"] for item in manifest["files"]],
            ["package-lock.json", "package.json", "src/main.js"],
        )
        self.assertTrue((self.path / "node_modules/pkg/a").exists())
        await self.call("check", manifest_sha256=result["manifest_sha256"])

    async def test_missing_lock_keeps_dependency_files_and_returns_warning(self):
        await self.seed()
        (self.path / "package-lock.json").unlink()
        result, manifest = await self.snapshot()
        self.assertEqual(manifest["format"], 1)
        self.assertIn("Missing package-lock.json", result["warnings"][0])
        self.assertIn(
            "node_modules/pkg/a", [item["path"] for item in manifest["files"]]
        )
        await self.prune(result["manifest_sha256"])
        self.assertTrue((self.path / "node_modules/pkg/a").exists())

    async def test_root_lock_does_not_authorize_nested_project_dependency_cleanup(self):
        await self.seed()
        nested = self.path / "another-project/node_modules/manual"
        nested.parent.mkdir(parents=True)
        nested.write_bytes(b"not described by root lock")
        result, manifest = await self.snapshot()
        self.assertEqual(manifest["rebuild"]["excluded"], ["node_modules"])
        self.assertIn(
            "another-project/node_modules/manual",
            [item["path"] for item in manifest["files"]],
        )
        await self.prune(result["manifest_sha256"])
        self.assertEqual(nested.read_bytes(), b"not described by root lock")

    async def test_unknown_shell_command_never_grants_cleanup(self):
        await self.seed()
        result, manifest = await self.snapshot("npm ci && rm -rf src")
        self.assertEqual(manifest["format"], 1)
        self.assertTrue(result["warnings"])
        await self.prune(result["manifest_sha256"])
        self.assertEqual((self.path / "src/main.js").read_text(), "retained source")
        self.assertTrue((self.path / "node_modules/pkg/a").exists())

    async def test_uv_and_cargo_select_only_their_own_dependencies(self):
        await self.reserve()
        for folder in (".venv", "node_modules", "target", "__pycache__"):
            (self.path / folder).mkdir()
            (self.path / folder / "data").write_bytes(b"fixture")
        (self.path / "pyproject.toml").write_text('[project]\nname="fixture"')
        (self.path / "uv.lock").write_text("version=1")
        result, manifest = await self.snapshot("uv sync --frozen")
        self.assertEqual(manifest["rebuild"]["excluded"], [".venv", "__pycache__"])
        await self.prune(result["manifest_sha256"])
        self.assertTrue((self.path / "node_modules/data").exists())
        self.assertTrue((self.path / "target/data").exists())
        (self.path / "Cargo.toml").write_text('[package]\nname="fixture"')
        (self.path / "Cargo.lock").write_text("version=4")
        result, _ = await self.client.project_files(
            "snapshot",
            "scope",
            1,
            generation=1,
            fence=6,
            reservation="a" * 32,
            rebuild_command="cargo build --locked",
        )
        self.assertEqual(result["rebuild"]["excluded"], ["target"])

    async def test_custom_and_browser_caches_keep_notes_and_login_state(self):
        await self.seed()
        for folder in (
            "generated",
            ".gaoji/browser/Cache",
            ".gaoji/browser/Code Cache",
        ):
            (self.path / folder).mkdir(parents=True)
            (self.path / folder / "data").write_bytes(b"cache")
        (self.path / ".gaoji/NOTES.md").write_text("important")
        (self.path / ".gaoji/browser/Cookies").write_bytes(b"private login")
        (self.path / ".gaoji/rebuildable").write_text(
            "# generated outputs\ngenerated\n"
        )
        result, manifest = await self.snapshot()
        self.assertIn(
            ".gaoji/rebuildable", [row["path"] for row in manifest["rebuild"]["locks"]]
        )
        await self.prune(result["manifest_sha256"])
        self.assertEqual(
            (self.path / ".gaoji/browser/Cookies").read_bytes(), b"private login"
        )
        self.assertEqual((self.path / ".gaoji/NOTES.md").read_text(), "important")
        self.assertFalse((self.path / "generated").exists())

    async def test_unsafe_custom_paths_refuse_snapshot_without_deleting(self):
        await self.seed()
        (self.path / ".gaoji").mkdir()
        for path in (
            "../outside",
            ".gaoji",
            ".gaoji/NOTES.md",
            ".gaoji/browser",
            ".git",
            "package.json",
            "node_modules/*",
        ):
            with self.subTest(path=path):
                (self.path / ".gaoji/rebuildable").write_text(path)
                with self.assertRaises(SandboxError):
                    await self.snapshot()
                self.assertTrue((self.path / "node_modules/pkg/a").exists())

    async def test_cleanup_unlinks_internal_symlink_without_following_it(self):
        await self.seed()
        secret = self.root / "outside-secret"
        secret.write_bytes(b"keep")
        (self.path / "node_modules/link").symlink_to(secret)
        result, _ = await self.snapshot()
        await self.prune(result["manifest_sha256"])
        self.assertEqual(secret.read_bytes(), b"keep")
        self.assertFalse((self.path / "node_modules").exists())

    async def test_changed_lock_blocks_cleanup_before_any_delete(self):
        await self.seed()
        result, _ = await self.snapshot()
        (self.path / "package-lock.json").write_text('{"lockfileVersion":2}')
        with self.assertRaises(SandboxError):
            await self.prune(result["manifest_sha256"])
        self.assertEqual(self.journal()["fence"], 4)
        self.assertTrue((self.path / "node_modules/pkg/a").exists())

    async def test_replaced_root_is_not_deleted(self):
        await self.seed()
        result, _ = await self.snapshot()
        (self.path / "node_modules").rename(self.root / "old-dependencies")
        (self.path / "node_modules").mkdir()
        (self.path / "node_modules/personal").write_bytes(b"do not delete replacement")
        with self.assertRaises(SandboxError):
            await self.prune(result["manifest_sha256"])
        self.assertEqual(
            (self.path / "node_modules/personal").read_bytes(),
            b"do not delete replacement",
        )

    async def test_interrupted_cleanup_resumes_same_fence(self):
        await self.seed()
        (self.path / "node_modules/pkg/b").write_bytes(b"second")
        result, _ = await self.snapshot()
        original, interrupted = project_rebuild._delete_tree, False

        def crash(parent, name, device, budget, depth=0):
            nonlocal interrupted
            original(parent, name, device, budget, depth)
            if not interrupted:
                interrupted = True
                raise RuntimeError("fixture crash after deletion")

        with patch.object(project_rebuild, "_delete_tree", side_effect=crash):
            with self.assertRaises(SandboxError):
                await self.prune(result["manifest_sha256"])
        self.assertTrue(self.journal()["pending"])
        await self.stop_runner()
        await self.start_runner()
        await self.prune(result["manifest_sha256"])
        self.assertFalse((self.path / "node_modules").exists())
        self.assertEqual((self.path / "src/main.js").read_text(), "retained source")
        self.assertFalse(self.journal()["pending"])

    async def test_cleanup_check_is_not_a_cached_mutation_receipt(self):
        await self.seed()
        result, _ = await self.snapshot()
        digest = result["manifest_sha256"]
        await self.prune(digest, request_id="b" * 32)
        await self.prune(digest, "prune_check", request_id="c" * 32)
        (self.path / "node_modules").mkdir()
        (self.path / "node_modules/new").write_bytes(b"retain")
        await self.prune(digest, request_id="b" * 32)
        with self.assertRaises(SandboxError):
            await self.prune(digest, "prune_check", request_id="c" * 32)
        self.assertTrue((self.path / "node_modules/new").exists())

    async def test_version_manifest_requires_pinned_inputs_and_no_retained_overlap(
        self,
    ):
        await self.seed()
        _, manifest = await self.snapshot()
        for mutate in (
            lambda value: value["rebuild"]["locks"][0].update(sha256="f" * 64),
            lambda value: value["rebuild"].update(excluded=["src"]),
        ):
            value = json.loads(json.dumps(manifest))
            mutate(value)
            with self.assertRaises(ValueError):
                manifest_bytes(value, quota_bytes=64 * 1024**2)


if __name__ == "__main__":
    unittest.main()
