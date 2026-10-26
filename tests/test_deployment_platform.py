from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.deployment_platform.content import extract_website, sha256
from src.deployment_platform.static_site import StaticSite, request_value
from src.deployment_platform.retention import prune_uploads, prune_versions
from src.local_runtime.files import read_json, write_json


class WebsiteContentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gaoji-static-content-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def archive(self, files, *, link=None):
        archive = self.root / "website.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            for name, content in files:
                entry = tarfile.TarInfo(name)
                entry.size = len(content)
                output.addfile(entry, io.BytesIO(content))
            if link:
                entry = tarfile.TarInfo(link)
                entry.type = tarfile.SYMTYPE
                entry.linkname = "/etc/passwd"
                output.addfile(entry)
        return archive, sha256(archive)

    def test_extract_and_repeat_validate_same_immutable_version(self):
        archive, checksum = self.archive([("index.html", b"<h1>hello</h1>"), ("assets/style.css", b"body{}")] )
        target = self.root / "releases" / checksum
        first = extract_website(archive, target, checksum)
        self.assertEqual(first, extract_website(archive, target, checksum))
        self.assertEqual(first["bytes"], 20)
        (target / "html/index.html").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "校验失败"):
            extract_website(archive, target, checksum)

    def test_reject_unsafe_and_duplicate_paths_without_partial_release(self):
        for name in ["../escaped", "/etc/passwd", ".env", "x\\y", "assets/.ssh/key", "secret.pem"]:
            with self.subTest(name=name):
                archive, checksum = self.archive([("index.html", b"hello"), (name, b"no")])
                with self.assertRaises(ValueError):
                    extract_website(archive, self.root / "releases" / checksum, checksum)
        archive, checksum = self.archive([("index.html", b"hello"), ("INDEX.HTML", b"duplicate")])
        with self.assertRaises(ValueError):
            extract_website(archive, self.root / "releases" / checksum, checksum)
        self.assertEqual(list((self.root / "releases").iterdir()), [])

    def test_reject_links_empty_entrypoint_and_mismatched_digest(self):
        archive, checksum = self.archive([("index.html", b"hello")], link="secret.txt")
        with self.assertRaises(ValueError):
            extract_website(archive, self.root / "target", checksum)
        archive, checksum = self.archive([("index.html", b"")])
        with self.assertRaises(ValueError):
            extract_website(archive, self.root / "target", checksum)
        with self.assertRaises(ValueError):
            extract_website(archive, self.root / "target", "0" * 64)


class WebsiteTransactionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gaoji-static-tasks-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "status").mkdir()
        self.host = Mock(job="a" * 32, archive=self.root / "gaoji-runtime.tar.gz")
        self.request = {"schema": 1, "app_id": "static-site", "instance_id": "b" * 32, "name": "Website",
                        "operation": "install", "expected_revision": 0, "content_sha256": "c" * 64}
        sockets = patch("src.deployment_platform.static_site.socket.socket")
        self.listener = sockets.start().return_value.__enter__.return_value
        self.listener.getsockname.return_value = ("127.0.0.1", 45678)
        self.addCleanup(sockets.stop)

    def instance(self, request=None, host=None):
        chosen = host or self.host
        chosen.environment = {"DOCKER_HOST": "unix:///fixture-only.sock"}
        return StaticSite(chosen, request or self.request, root=self.root)

    def test_resuming_preserves_revision_and_completed_job_does_not_repeat(self):
        first = self.instance()
        self.assertFalse(first.prepare())
        self.assertEqual(first.state["revision"], 1)
        resumed = self.instance()
        self.assertFalse(resumed.prepare())
        self.assertEqual(resumed.state["revision"], 1)
        resumed.persist(operation={**resumed.state["operation"], "status": "completed"})
        finished = self.instance()
        finished.execute()
        self.host.setup_engine.assert_not_called()
        self.assertEqual(read_json(first.record)["revision"], 1)

    def test_stale_and_concurrent_tasks_cannot_replace_an_instance(self):
        first = self.instance()
        first.prepare()
        changed = {**self.request, "name": "changed"}
        with self.assertRaisesRegex(ValueError, "参数发生变化"):
            self.instance(changed).prepare()
        other_host = Mock(job="d" * 32)
        with self.assertRaisesRegex(ValueError, "原任务"):
            self.instance(host=other_host).prepare()
        first.persist(operation={**first.state["operation"], "status": "completed"})
        with self.assertRaisesRegex(ValueError, "发生变化"):
            self.instance(host=other_host).prepare()
        with self.assertRaisesRegex(ValueError, "发生变化"):
            self.instance(host=other_host).execute()
        other_host.save.assert_called_with(instance_id="b" * 32, app_id="static-site", rejected=True)
        other_host.setup_engine.assert_not_called()
        self.assertEqual(read_json(first.record)["revision"], 1)

    def test_failed_candidate_leaves_previous_website_and_can_accept_corrected_content(self):
        first = self.instance()
        first.prepare()
        old = {"container": "existing-site", "checksum": "e" * 64, "port": 45678}
        first.persist(current=old, status="running", operation={**first.state["operation"], "status": "completed"})
        new_host = Mock(job="d" * 32, archive=self.root / "gaoji-runtime.tar.gz")
        request = {**self.request, "expected_revision": 1, "operation": "start", "content_sha256": ""}
        instance = self.instance(request, new_host)
        instance.image_and_network = Mock()
        instance.candidate = Mock(side_effect=ValueError("bad HTTP"))
        instance.remove = Mock()
        with self.assertRaisesRegex(ValueError, "bad HTTP"):
            instance.execute()
        result = read_json(instance.record)
        self.assertEqual(result["current"], old)
        self.assertEqual(result["status"], "running")
        self.assertTrue(result["operation"]["safe_to_replace"])
        self.assertNotIn("existing-site", [call.args[0] for call in instance.remove.call_args_list])

    def test_commit_is_saved_before_cleanup_and_recovery_does_not_promote_twice(self):
        first = self.instance()
        first.prepare()
        old = {"container": "old", "checksum": "e" * 64}
        first.persist(current=old, status="running", operation={**first.state["operation"], "status": "completed"})
        host = Mock(job="d" * 32)
        request = {**self.request, "operation": "start", "content_sha256": "", "expected_revision": 1}
        candidate = {"container": "gaoji-site-" + "b" * 32 + "-" + "d" * 32, "checksum": "e" * 64}
        instance = self.instance(request, host)
        instance.image_and_network = Mock()
        instance.candidate = Mock(return_value=candidate)
        instance.remove = Mock(side_effect=OSError("interrupted cleanup"))
        with self.assertRaises(OSError):
            instance.execute()
        self.assertEqual(read_json(instance.record)["current"], candidate)
        restored = self.instance(request, host)
        restored.image_and_network = Mock()
        restored.candidate = Mock(side_effect=AssertionError("must not promote again"))
        restored.remove = Mock()
        restored.execute()
        restored.candidate.assert_not_called()
        self.assertEqual(read_json(restored.record)["operation"]["status"], "completed")

    def test_unknown_directory_and_unapproved_purge_are_refused(self):
        instance = self.instance()
        instance.directory.mkdir(parents=True)
        (instance.directory / "user-file").write_text("retain")
        with self.assertRaisesRegex(ValueError, "未知内容"):
            instance.prepare()
        self.assertEqual((instance.directory / "user-file").read_text(), "retain")
        with self.assertRaises(ValueError):
            request_value({**self.request, "command": "anything"})
        with self.assertRaises(ValueError):
            request_value({**self.request, "operation": "purge"})

    def test_docker_empty_json_list_means_missing_image_and_requires_pull(self):
        self.host.release = self.root
        self.host.docker = "/usr/bin/docker"
        image = "docker.io/nginxinc/nginx-unprivileged:stable-alpine@sha256:" + "a" * 64
        write_json(self.root / "applications.json", {"applications": [{"id": "static-site", "images": {"web": image}}]})
        self.host.run.side_effect = lambda command, **_: "29.0.0" if "info" in command else "[]\n"
        instance = self.instance()
        instance.inspect = Mock(return_value={"Name": "existing-network"})
        instance.image_and_network()
        self.assertIn([self.host.docker, "pull", image], [call.args[0] for call in self.host.run.call_args_list])

    def test_missing_published_port_is_a_clear_failure_not_a_type_error(self):
        instance = self.instance()
        release = instance.directory / "releases" / ("c" * 64)
        release.mkdir(parents=True)
        write_json(release / "content.json", {"files": [{"path": "index.html", "sha256": "d" * 64}], "bytes": 1})
        instance.inspect = Mock(side_effect=[None, {"NetworkSettings": {"Ports": {"8080/tcp": None}}}])
        with self.assertRaisesRegex(ValueError, "访问端口"):
            instance.candidate("c" * 64)

    def test_candidate_pins_loopback_port_for_docker_restart(self):
        instance = self.instance()
        release = instance.directory / "releases" / ("c" * 64)
        release.mkdir(parents=True)
        body = b"<h1>persistent port</h1>"
        digest = hashlib.sha256(body).hexdigest()
        write_json(release / "content.json", {"files": [{"path": "index.html", "sha256": digest}], "bytes": len(body)})
        instance.inspect = Mock(side_effect=[None, {"NetworkSettings": {"Ports": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "45678"}]}}}])
        with patch("src.deployment_platform.static_site.http.client.HTTPConnection") as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = body
            result = instance.candidate("c" * 64)
        command = next(call.args[0] for call in self.host.run.call_args_list if "--publish" in call.args[0])
        self.assertEqual(command[command.index("--publish") + 1], "127.0.0.1:45678:8080")
        self.listener.bind.assert_called_once_with(("127.0.0.1", 0))
        self.assertEqual(result["port"], 45678)


class WebsiteRetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gaoji-site-retention-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "jobs").mkdir()
        self.instance = "a" * 32
        self.host = Mock(job="b" * 32)
        self.host.run.return_value = "0"

    def upload(self, job, instance, status="completed"):
        data = b"fixture-code"
        checksum = hashlib.sha256(data).hexdigest()
        content = b"fixture-website"
        work = self.root / "bootstrap" / job / checksum
        work.mkdir(parents=True)
        write_json(self.root / "jobs" / (job + ".json"), {"job": job, "instance_id": instance, "app_id": "static-site", "checksum": checksum, "status": status})
        write_json(work / "application.json", {"app_id": "static-site", "instance_id": instance, "content_sha256": hashlib.sha256(content).hexdigest()})
        (work / "gaoji-runtime.tar.gz").write_bytes(data)
        (work / "website.tar.gz").write_bytes(content)
        return work

    def test_cleanup_only_completed_own_verified_uploads(self):
        completed = self.upload("b" * 32, self.instance)
        pending = self.upload("c" * 32, self.instance, "running")
        foreign = self.upload("d" * 32, "e" * 32)
        released = prune_uploads(self.host, self.root, self.instance)
        self.assertGreater(released, 0)
        self.assertFalse((completed / "website.tar.gz").exists())
        self.assertFalse((completed / "gaoji-runtime.tar.gz").exists())
        self.assertTrue((completed / "application.json").exists())
        self.assertTrue((pending / "website.tar.gz").exists())
        self.assertTrue((foreign / "website.tar.gz").exists())

    def test_running_old_task_or_changed_file_is_not_deleted(self):
        work = self.upload("c" * 32, self.instance)
        self.host.run.return_value = "123"
        self.assertEqual(prune_uploads(self.host, self.root, self.instance), 0)
        with self.assertRaisesRegex(ValueError, "后台任务"):
            prune_uploads(self.host, self.root, self.instance, purge=True)
        self.host.run.return_value = "0"
        (work / "website.tar.gz").write_bytes(b"user-changed")
        with self.assertRaisesRegex(ValueError, "改动"):
            prune_uploads(self.host, self.root, self.instance)
        self.assertTrue((work / "website.tar.gz").exists())

    def test_current_previous_and_externally_mounted_versions_are_retained(self):
        for char in "abcd":
            release = self.root / "releases" / (char * 64)
            release.mkdir(parents=True)
            write_json(release / "content.json", {"checksum": char * 64, "files": []})
        mounted = str(self.root / "releases" / ("c" * 64) / "html")
        prune_versions(self.root, {"a" * 64, "b" * 64}, [mounted])
        self.assertEqual({item.name for item in (self.root / "releases").iterdir()}, {char * 64 for char in "abc"})

    def test_retention_does_not_delete_unlisted_user_files(self):
        release = self.root / "releases" / ("c" * 64)
        release.mkdir(parents=True)
        write_json(release / "content.json", {"checksum": "c" * 64, "files": []})
        (release / "user-notes.txt").write_text("keep")
        with self.assertRaisesRegex(ValueError, "未知文件"):
            prune_versions(self.root, set(), [])
        self.assertEqual((release / "user-notes.txt").read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
