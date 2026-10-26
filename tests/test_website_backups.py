from __future__ import annotations

import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.deployment_platform import backups
from src.deployment_platform.content import extract_website, sha256
from src.deployment_platform.static_site import StaticSite, request_value
from src.local_runtime.files import read_json, write_json


class WebsiteBackupsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gaoji-website-backups-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "status").mkdir()
        self.instance = "a" * 32
        self.directory = self.root / "applications" / self.instance
        source = self.root / "website.tar.gz"
        with tarfile.open(source, "w:gz") as output:
            for name, data in [("index.html", b"<h1>private page</h1>"), ("assets/site.css", b"body {color:red}")]:
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                output.addfile(entry, io.BytesIO(data))
        self.checksum = sha256(source)
        self.request = {"schema": 1, "app_id": "static-site", "instance_id": self.instance, "name": "Site",
                        "operation": "install", "expected_revision": 0, "content_sha256": self.checksum}
        first = StaticSite(Mock(job="0" * 32), self.request, root=self.root)
        first.prepare()
        extract_website(source, self.directory / "releases" / self.checksum, self.checksum)
        self.current = {"checksum": self.checksum, "container": "original", "port": 12345}
        first.persist(current=self.current, status="running", operation={**first.state["operation"], "status": "completed"})

    def operation(self, kind, job="b", **extra):
        state = read_json(self.directory / "instance.json")
        request = {**self.request, "operation": kind, "content_sha256": "", "expected_revision": state["revision"], **extra}
        host = Mock(job=job * 32)
        host.environment = {"DOCKER_HOST": "fixture-only"}
        site = StaticSite(host, request, root=self.root)
        site.cleanup = Mock()
        return site

    def test_create_idempotent_backup_without_docker_and_roundtrip(self):
        site = self.operation("backup")
        site.execute()
        site.host.setup_engine.assert_not_called()
        saved = read_json(site.record)
        self.assertEqual(saved["current"], self.current)
        self.assertEqual(len(saved["backups"]), 1)
        archive, info = backups.verify(self.directory, self.instance, "b" * 32)
        target = self.root / "recovered"
        extract_website(archive, target, info["sha256"])
        self.assertEqual((target / "html/index.html").read_text(), "<h1>private page</h1>")
        replay = StaticSite(site.host, site.request, root=self.root)
        replay.execute()
        self.assertEqual(read_json(site.record)["backups"], saved["backups"])
        self.assertEqual(read_json(site.record)["revision"], saved["revision"])

    def test_backup_publication_interrupt_resumes_original_snapshot(self):
        site = self.operation("backup")
        site.prepare()
        info = backups.create(self.directory, self.instance, site.host.job, self.checksum)
        replay = StaticSite(site.host, site.request, root=self.root)
        replay.execute()
        self.assertEqual(read_json(site.record)["backups"], [info])
        site.host.setup_engine.assert_not_called()

    def test_wrong_instance_corruption_and_parent_link_are_refused(self):
        self.operation("backup").execute()
        with self.assertRaisesRegex(ValueError, "校验失败"):
            backups.verify(self.directory, "c" * 32, "b" * 32)
        archive, _ = backups.verify(self.directory, self.instance, "b" * 32)
        archive.write_bytes(b"broken")
        with self.assertRaisesRegex(ValueError, "校验失败"):
            backups.verify(self.directory, self.instance, "b" * 32)
        release = self.directory / "releases" / self.checksum
        assets = release / "html/assets"
        assets.rename(self.root / "outside")
        assets.symlink_to(self.root / "outside", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "所有者或类型"):
            backups.create(self.directory, self.instance, "c" * 32, self.checksum)
        self.assertTrue((self.root / "outside/site.css").exists())
        self.assertFalse((self.directory / "backups" / ("c" * 32 + ".json")).exists())

    def test_failed_restore_keeps_current_then_retry_promotes_once(self):
        self.operation("backup").execute()
        site = self.operation("restore", job="c", backup_id="b" * 32)
        site.image_and_network = Mock()
        site.candidate = Mock(side_effect=ValueError("HTTP failed"))
        site.remove = Mock()
        with self.assertRaisesRegex(ValueError, "HTTP failed"):
            site.execute()
        self.assertEqual(read_json(site.record)["current"], self.current)
        candidate = {"checksum": site.state["backups"][0]["sha256"], "container": "gaoji-site-" + self.instance + "-" + "c" * 32, "port": 12346}
        site.candidate = Mock(return_value=candidate)
        site.execute()
        self.assertEqual(read_json(site.record)["current"], candidate)
        self.assertEqual(read_json(site.record)["previous"], self.current)
        site.candidate.reset_mock()
        site.execute()
        site.candidate.assert_not_called()

    def test_delete_and_purge_only_selected_backups(self):
        self.operation("backup").execute()
        self.operation("backup", job="c").execute()
        self.operation("delete-backup", job="d", backup_id="b" * 32).execute()
        saved = read_json(self.directory / "instance.json")
        self.assertEqual([item["id"] for item in saved["backups"]], ["c" * 32])
        self.assertEqual(saved["current"], self.current)
        unknown = self.directory / "backups/user-file"
        unknown.write_text("do not delete")
        with self.assertRaisesRegex(ValueError, "未知文件"):
            backups.purge(self.directory)
        self.assertTrue((self.directory / "backups" / ("c" * 32 + ".json")).exists())
        unknown.unlink()
        backups.purge(self.directory)
        self.assertFalse((self.directory / "backups").exists())
        self.assertTrue((self.directory / "releases" / self.checksum).exists())

    def test_strict_request_does_not_accept_paths_or_unbound_backup_ids(self):
        for operation, extra in [("restore", {}), ("restore", {"backup_id": "../../outside"}), ("backup", {"backup_id": "b" * 32})]:
            with self.subTest(operation=operation, extra=extra), self.assertRaises(ValueError):
                request_value({**self.request, "operation": operation, "content_sha256": "", **extra})
        with self.assertRaisesRegex(ValueError, "没有该备份"):
            self.operation("restore", backup_id="f" * 32).execute()

    def test_public_permissions_are_set_before_atomic_publication(self):
        target = self.root / "status/permissions.json"
        replace = os.replace
        for mode in (0o644, 0o600):
            def publish(source, destination):
                self.assertEqual(Path(source).stat().st_mode & 0o777, mode)
                replace(source, destination)
            with patch("src.local_runtime.files.os.replace", side_effect=publish):
                write_json(target, {"fixture": True}, mode=mode)
            self.assertEqual(target.stat().st_mode & 0o777, mode)
