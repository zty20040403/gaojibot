"""Accepted artifacts use real PostgreSQL/CAS; reviewers are explicit fixtures."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import io
import json
import stat
import threading
import unittest
from unittest.mock import patch
import zipfile

import nonebot

nonebot.init()

from src.project_content import manifest_digest
from src.project_update import DESCRIPTOR, ProjectUpdate
from src.plugins.ai_chat.project_blobs import ProjectFile
from src.plugins.ai_chat.agent.control import JobFence, LeaseLost, active_job_fence
from src.plugins.ai_chat.project_publication import ProjectPublications
from src.plugins.ai_chat.project_tasks import ProjectTaskStore
from src.plugins.ai_chat.projects import ProjectConflict, ProjectQuotaExceeded
from src.plugins.ai_chat.subagents import SubAgentStore, TaskStep
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from tests import test_project_tasks as fixture


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_item(path, content, mode=0o644):
    return {"path": path, "sha256": sha(content), "size": len(content), "mode": mode}


def archive(descriptor, contents, *, extra=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr(DESCRIPTOR, json.dumps(descriptor))
        for path, content in contents.items():
            zipped.writestr("files/" + path, content)
        if extra:
            zipped.writestr(*extra)
    return output.getvalue()


def descriptor(baseline, changes, *, pid=1, version=1, **updates):
    return {"format": 1, "project_id": pid, "base_version": version,
            "base_manifest_sha256": manifest_digest(baseline, quota_bytes=100000),
            "files": sorted([file_item(p, c) for p, c in changes.items()], key=lambda i: i["path"]),
            "delete": [], **updates}


class ProjectUpdateTests(unittest.TestCase):
    def setUp(self):
        self.baseline = {"format": 1, "files": [file_item("keep.txt", b"keep")]}

    def parse(self, desc, contents, **kwargs):
        return ProjectUpdate(archive(desc, contents, **kwargs), project_id=1, base_version=1,
            base_manifest_sha256=manifest_digest(self.baseline, quota_bytes=100000),
            baseline=self.baseline, quota_bytes=100000)

    def test_missing_files_are_preserved_and_explicit_deletion_is_exact(self):
        changes = {"new/a.txt": b"new"}
        desc = descriptor(self.baseline, changes)
        with self.parse(desc, changes) as result:
            self.assertEqual([i["path"] for i in result.manifest["files"]], ["keep.txt", "new/a.txt"])
            with result.open("new/a.txt") as source:
                self.assertEqual(source.read(), b"new")
        with self.parse({**desc, "delete": ["keep.txt"]}, changes) as result:
            self.assertEqual(len(result.manifest["files"]), 1)
        for deletion in (["missing"], ["keep.txt", "keep.txt"], ["../escape"], "keep.txt"):
            with self.subTest(deletion=deletion), self.assertRaises(ValueError):
                self.parse({**desc, "delete": deletion}, changes)

    def test_identity_undeclared_paths_and_nonregular_entries_are_rejected(self):
        desc = descriptor(self.baseline, {})
        for update in ({"project_id": 2}, {"project_id": True}, {"base_version": 2},
                       {"base_manifest_sha256": "f" * 64}, {"unknown": 1}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.parse({**desc, **update}, {})
        for path in ("../outside", "/outside", "files/unlisted", "FILES/keep.txt"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.parse(desc, {}, extra=(path, b"bad"))
        for mode in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR):
            info = zipfile.ZipInfo("files/link")
            info.create_system = 3
            info.external_attr = (mode | 0o777) << 16
            with self.assertRaisesRegex(ValueError, "Unsafe"):
                self.parse(desc, {}, extra=(info, b"target"))

    def test_case_collisions_missing_and_oversized_members_fail_before_transfer(self):
        for changes in ({"KEEP.txt": b"x"}, {"keep.txt/a": b"x"}):
            with self.assertRaises(ValueError):
                self.parse(descriptor(self.baseline, changes), changes)
        changes = {"new": b"a"}
        desc = descriptor(self.baseline, changes)
        with self.assertRaises(ValueError):
            self.parse(desc, {})
        with self.assertRaises(ValueError):
            self.parse(desc, {"new": b"oversized"})
        with self.assertRaises(ValueError):
            self.parse({**desc, "files": [{**desc["files"][0], "size": 256 * 1024 * 1024 + 1}]}, changes)
        with self.assertRaises(ValueError):
            self.parse({**desc, "delete": ["new"]}, changes)

    def test_browser_state_is_retained_and_changed_lock_hashes_are_rebound(self):
        files = [file_item(".gaoji/browser/Default/Cookies", b"cookies"),
                 file_item("package-lock.json", b"lock"), file_item("package.json", b"package")]
        self.baseline = {"format": 3, "files": files, "browser_cache": [".gaoji/browser/Cache"],
            "rebuild": {"command": "npm ci", "locks": [{"path": i["path"], "sha256": i["sha256"]} for i in files[1:]],
                        "excluded": ["node_modules"]}}
        changes = {"package-lock.json": b"new-lock"}
        desc = descriptor(self.baseline, changes)
        with self.parse(desc, changes) as result:
            self.assertEqual(result.manifest["files"][0], files[0])
            self.assertEqual(result.manifest["browser_cache"], self.baseline["browser_cache"])
            self.assertEqual(result.manifest["rebuild"]["locks"][0]["sha256"], sha(b"new-lock"))
        with self.assertRaises(ValueError):
            self.parse(descriptor(self.baseline, {}, delete=["package-lock.json"]), {})
        with self.parse(descriptor(self.baseline, {}, delete=["package-lock.json"], rebuild=None), {}) as result:
            self.assertIsNone(result.manifest["rebuild"])
        for path in ("node_modules/cache", ".gaoji/browser/Cache/cache"):
            with self.assertRaises(ValueError):
                self.parse(descriptor(self.baseline, {path: b"x"}), {path: b"x"})


class ProjectPublicationTests(unittest.TestCase):
    owned = fixture.ProjectTaskTests.owned
    new_store = fixture.ProjectTaskTests.new_store
    create = fixture.ProjectTaskTests.create
    snapshot = fixture.ProjectTaskTests.snapshot
    prepare = fixture.ProjectTaskTests.prepare
    count = fixture.ProjectTaskTests.count

    def setUp(self):
        fixture.ProjectTaskTests.setUp(self)
        self.publisher = ProjectPublications(self.binding)
        self.task = self.prepare()
        self.baseline = self.store.manifest(self.access, self.pid, 1)

    def accepted(self, changes=None, *, desc_updates=None, task_status="completed"):
        changes = {"index.html": b"updated", "src/main.py": b"print(1)"} if changes is None else changes
        desc = descriptor(self.baseline, changes, pid=self.pid, **(desc_updates or {}))
        self.content = archive(desc, changes)
        self.digest = sha(self.content)
        artifact = {"snapshot": self.digest, "size": len(self.content), "name": "project-update.zip",
                    "handle": "s123456:/workspace/project-update.zip", "kind": "file"}
        self.producer = self.tasks.create_run(self.task.task_id, TaskStep("code", "coder", "edit", "bundle"),
            allowed_tools=[], model_profile="fixture")
        self.tasks.finish_run(self.producer.run_id, "succeeded", result={"status": "success", "artifacts": [artifact]})
        self.reviewer = self.tasks.create_run(self.task.task_id,
            TaskStep("acceptance_r1_12345678", "coder", "review", "verdict", dependencies=("code",)),
            allowed_tools=[], model_profile="fixture")
        self.review = {"status": "success", "metadata": {"artifact_reviews": [
            {"artifact_key": self.digest, "status": "passed", "reason": "fixture inspected update"}]}}
        self.tasks.finish_run(self.reviewer.run_id, "succeeded", result=self.review)
        self.event("import_agent_artifact", {"ok": True, "sha256": self.digest, "read_only": True})
        self.event("sandbox_exec", {"returncode": 0})
        self.result = {"steps": {"code": {"status": "succeeded", "result": {"artifacts": [artifact]}}},
            "validation": {"acceptance": {"status": "passed", "run_id": self.reviewer.run_id,
                "checks": [{"step": "code", "artifact_key": self.digest, "artifact": "project-update.zip", "ok": True}]}}}
        self.tasks.set_task_state(self.task.task_id, task_status, result=self.result)

    def event(self, name, result, *, sid="sabcdef"):
        call_id = str(self.count("subagent_events") + 1)
        arguments = {"sandbox_id": sid}
        if name == "sandbox_exec":
            self.tasks.record_evidence(self.task.task_id, self.reviewer.run_id, name, arguments, result, call_id=call_id)
        self.tasks.append_event(self.task.task_id, "agent.tool_finished", {"tool_name": name,
            "arguments": arguments, "result": json.dumps(result)[:8000], "call_id": call_id}, run_id=self.reviewer.run_id)

    def publish(self, **kwargs):
        return self.publisher.publish(kwargs.pop("access", self.access), self.task.task_id,
            kwargs.pop("digest", self.digest), kwargs.pop("content", self.content), **kwargs)

    def test_publish_atomic_version_receipt_and_lock_then_replay_after_artifact_expiry(self):
        self.accepted()
        notified = []
        self.tasks.set_change_listener(lambda task_id: notified.append(self.count("project_task_publications")))
        receipt = self.publish()
        self.assertEqual(notified, [1])
        self.assertEqual(receipt["version_no"], 2)
        self.assertEqual(self.store.read_file(self.access, self.pid, 1, "index.html"), b"first")
        self.assertEqual(self.store.read_file(self.access, self.pid, 2, "src/main.py"), b"print(1)")
        self.assertEqual(self.store.get(self.access, self.pid)["locked_task_id"], self.task.task_id)
        self.assertEqual(self.count("project_task_publications"), 1)
        self.assertEqual(self.count("project_versions"), 2)
        reopened = ProjectPublications(ProjectTaskStore(self.new_store(), self.owned(SubAgentStore(self.db))))
        self.store.open(self.access, self.create())
        self.assertEqual(reopened.publish(self.access, self.task.task_id, self.digest), receipt)
        checkpoint = self.tasks.checkpoints(self.task.task_id)[-1]
        self.assertEqual(checkpoint["phase"], "project_version_published")
        self.assertIs(checkpoint["state"]["materialized"], False)
        with self.assertRaises(ProjectConflict):
            self.publish(digest="f" * 64)
        self.assertEqual(self.count("project_versions"), 2)

    def test_noop_reuses_version_and_partial_delivery_does_not_change_content_verdict(self):
        self.accepted({"index.html": b"first"}, task_status="partial")
        receipt = self.publish()
        self.assertEqual(receipt["version_no"], 1)
        self.assertEqual(self.count("project_versions"), 1)
        self.assertEqual(self.count("project_task_publications"), 1)

    def test_missing_failed_duplicate_or_obsolete_review_cannot_publish(self):
        self.accepted()
        for metadata in ({}, {"artifact_reviews": [{"artifact_key": self.digest, "status": "failed"}]},
                         {"artifact_reviews": self.review["metadata"]["artifact_reviews"] * 2}):
            self.tasks.finish_run(self.reviewer.run_id, "succeeded", result={"metadata": metadata})
            with self.assertRaises(ProjectConflict):
                self.publish()
        self.tasks.finish_run(self.reviewer.run_id, "succeeded", result=self.review)
        self.sql.execute("UPDATE subagent_controls SET revision=2 WHERE task_id=?", (self.task.task_id,))
        with self.assertRaises(ProjectConflict):
            self.publish()
        self.assertEqual(self.count("project_versions"), 1)

    def test_review_of_another_snapshot_or_sandbox_is_not_execution_proof(self):
        self.accepted()
        self.sql.execute("DELETE FROM subagent_events WHERE run_id=? AND event_type='agent.tool_finished'", (self.reviewer.run_id,))
        self.event("import_agent_artifact", {"ok": True, "sha256": "f" * 64, "read_only": True})
        self.event("sandbox_exec", {"returncode": 0})
        with self.assertRaises(ProjectConflict):
            self.publish()
        self.event("import_agent_artifact", {"ok": True, "sha256": self.digest, "read_only": True})
        self.event("sandbox_exec", {"returncode": 0}, sid="s000000")
        with self.assertRaises(ProjectConflict):
            self.publish()
        self.event("sandbox_exec", {"returncode": 0})
        self.assertEqual(self.publish()["version_no"], 2)

    def test_missing_source_and_changed_bytes_never_publish(self):
        self.accepted()
        for data in (None, b"changed", self.content + b"trailing"):
            with self.subTest(data=data is None), self.assertRaises(ProjectConflict):
                self.publish(content=data)
        self.sql.execute("UPDATE subagent_runs SET status='skipped' WHERE run_id=?", (self.producer.run_id,))
        with self.assertRaises(ProjectConflict):
            self.publish()
        self.assertEqual(self.count("project_versions"), 1)

    def test_truncated_display_log_uses_complete_revision_bound_host_receipt(self):
        self.accepted()
        self.sql.execute("DELETE FROM subagent_evidence WHERE task_id=?", (self.task.task_id,))
        with self.assertRaisesRegex(ProjectConflict, "execution evidence"):
            self.publish()
        self.event("sandbox_exec", {"stdout": "x" * 9000, "returncode": 0})
        self.assertEqual(self.publish()["version_no"], 2)

    def test_incomplete_or_tampered_host_receipt_is_not_proof(self):
        self.accepted()
        self.sql.execute("UPDATE subagent_evidence SET complete=0 WHERE task_id=?", (self.task.task_id,))
        with self.assertRaises(ProjectConflict):
            self.publish()
        self.sql.execute("UPDATE subagent_evidence SET complete=1,payload_json='{}' WHERE task_id=?", (self.task.task_id,))
        with self.assertRaises(ProjectConflict):
            self.publish()

    def test_scope_membership_bot_actor_and_cancel_are_enforced(self):
        self.accepted()
        for access in (self.b, replace(self.access, member=False),
                       replace(self.access, scope=replace(self.scope, native_conversation_id="102")),
                       replace(self.access, scope=replace(self.scope, bot_native_user_id="302"))):
            with self.subTest(access=access), self.assertRaises(PermissionError):
                self.publish(access=access)
        self.sql.execute("UPDATE subagent_tasks SET cancel_requested=TRUE WHERE task_id=?", (self.task.task_id,))
        with self.assertRaises(ProjectConflict):
            self.publish()
        self.assertEqual(self.count("project_task_publications"), 0)

    def test_lost_generation_and_changed_revision_during_transfer_reject_commit(self):
        self.accepted()
        put = self.store.blobs.put
        def changed(source):
            blob = put(source)
            self.sql.execute("UPDATE subagent_controls SET revision=2 WHERE task_id=?", (self.task.task_id,))
            return blob
        with patch.object(self.store.blobs, "put", side_effect=changed), self.assertRaises(ProjectConflict):
            self.publish()
        self.assertEqual(self.count("project_versions"), 1)
        self.sql.execute("UPDATE subagent_controls SET revision=1 WHERE task_id=?", (self.task.task_id,))
        self.sql.execute("UPDATE projects SET lifecycle_fence=lifecycle_fence+1 WHERE project_id=?", (self.pid,))
        with self.assertRaises(ProjectConflict):
            self.publish()

    def test_checkpoint_failure_rolls_back_version_refs_and_receipt_then_retries(self):
        self.accepted()
        with patch.object(self.tasks, "append_checkpoint", side_effect=RuntimeError("crash")), self.assertRaises(RuntimeError):
            self.publish()
        self.assertEqual(self.count("project_versions"), 1)
        self.assertEqual(self.count("project_task_publications"), 0)
        self.assertEqual(self.store.get(self.access, self.pid)["locked_task_id"], self.task.task_id)
        self.assertEqual(self.publish()["version_no"], 2)

    def test_interrupted_stream_leaves_no_version_and_reuses_cas_on_retry(self):
        self.accepted()
        put = self.store.blobs.put
        calls = []
        def interrupted(source):
            blob = put(source)
            calls.append(blob.sha256)
            if len(calls) == 2:
                raise RuntimeError("stream interrupted")
            return blob
        with patch.object(self.store.blobs, "put", side_effect=interrupted), self.assertRaises(RuntimeError):
            self.publish()
        self.assertEqual(self.count("project_versions"), 1)
        self.assertEqual(self.count("project_task_publications"), 0)
        self.assertEqual(self.publish()["version_no"], 2)

    def test_reused_orphans_receive_fresh_gc_grace_before_commit(self):
        self.accepted()
        orphan = self.store.blobs.put(b"updated")
        with self.store._transaction() as cursor:
            self.store._register_blob(cursor, orphan, 1)
        with patch.object(self.publisher, "_commit", side_effect=RuntimeError("stop before commit")), self.assertRaises(RuntimeError):
            self.publish()
        row = self.sql.execute("SELECT unreferenced_since FROM project_blobs WHERE sha256=?", (orphan.sha256,)).fetchone()
        self.assertGreaterEqual(row[0], self.now)
        self.assertEqual(self.count("project_versions"), 1)
        self.assertEqual(self.publish()["version_no"], 2)

    def test_transfer_does_not_block_lease_renewal_and_stale_worker_cannot_commit(self):
        self.accepted()
        jobs = self.owned(DurableJobStore(self.db))
        jobs.enqueue(kind="subagent.task", idempotency_key="publication-fixture")
        job = jobs.claim_due("publisher")[0]
        token = active_job_fence.set(JobFence(job.job_id, "publisher", job.attempts, lambda: True))
        self.addCleanup(active_job_fence.reset, token)
        self.sql.execute("SET statement_timeout='2s'")
        put = self.store.blobs.put
        def transfer(source):
            blob = put(source)
            # A separate connection can update the lease during the slow IO phase.
            self.sql.execute("UPDATE durable_jobs SET lease_until=lease_until+1 WHERE job_id=?", (job.job_id,))
            self.sql.execute("UPDATE durable_jobs SET lease_owner='replacement' WHERE job_id=?", (job.job_id,))
            return blob
        with patch.object(self.store.blobs, "put", side_effect=transfer), self.assertRaises(LeaseLost):
            self.publish()
        self.assertEqual(self.count("project_versions"), 1)
        self.assertEqual(self.count("project_task_publications"), 0)

    def test_scope_quota_failure_rolls_back_and_can_resume_after_capacity_increase(self):
        self.accepted()
        self.sql.execute("UPDATE project_scope_limits SET quota_bytes=1 WHERE scope_key=?", (self.scope.key,))
        with patch.object(self.store.blobs, "put", wraps=self.store.blobs.put) as put, self.assertRaises(ProjectQuotaExceeded):
            self.publish()
        put.assert_not_called()
        self.assertEqual(self.count("project_versions"), 1)
        self.assertEqual(self.count("project_task_publications"), 0)
        self.sql.execute("UPDATE project_scope_limits SET quota_bytes=100000 WHERE scope_key=?", (self.scope.key,))
        self.assertEqual(self.publish()["version_no"], 2)

    def test_member_hash_not_descriptor_claim_controls_cas(self):
        self.accepted(desc_updates={"files": [file_item("index.html", b"wrong!!"), file_item("src/main.py", b"print(1)")]})
        with self.assertRaisesRegex(ProjectConflict, "checksum"):
            self.publish()
        self.assertEqual(self.count("project_versions"), 1)

    def test_concurrent_publish_creates_only_one_version(self):
        self.accepted()
        publishers = [ProjectPublications(ProjectTaskStore(self.new_store(), self.owned(SubAgentStore(self.db)))) for _ in range(2)]
        barrier = threading.Barrier(2)
        def run(publisher):
            barrier.wait(timeout=5)
            return publisher.publish(self.access, self.task.task_id, self.digest, self.content)
        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(run, publishers))
        self.assertEqual(receipts[0], receipts[1])
        self.assertEqual(self.count("project_versions"), 2)
        self.assertEqual(self.count("project_task_publications"), 1)

    def test_publication_keeps_retained_version_when_task_lock_is_later_reconciled(self):
        self.accepted()
        self.publish()
        # Explicit fixture for the future verified-materialization/release boundary.
        self.sql.execute("UPDATE projects SET locked_task_id=NULL WHERE project_id=?", (self.pid,))
        self.store.snapshot(self.access, self.pid, [ProjectFile("index.html", b"third")],
            expected_version=self.store.get(self.access, self.pid)["version"])
        self.store.prune_versions(self.access, self.pid, set(),
            expected_version=self.store.get(self.access, self.pid)["version"])
        self.assertEqual(self.store.read_file(self.access, self.pid, 2, "index.html"), b"updated")


if __name__ == "__main__":
    unittest.main()
