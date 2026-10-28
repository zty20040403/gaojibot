"""Publish only independently accepted project edits; materialization is separate.

This host-only API deliberately keeps the task lock. A CAS version is not proof
that the project workspace has been restored, rebuilt or verified.
"""

import hashlib
import json
import re
import time

from src.project_update import ProjectUpdate
from .agent.artifact_acceptance import artifact_verdicts
from .agent.control import assert_job_owned
from .projects import ProjectConflict, ProjectQuotaExceeded, require_project_io_idle


def _object(value):
    try:
        result = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return {}
    return result if isinstance(result, dict) else {}


class ProjectPublications:
    def __init__(self, bindings):
        self.bindings = bindings
        self.projects, self.tasks = bindings.projects, bindings.tasks

    def _authority(self, cursor, access, task_id, digest):
        # Revisions take this row before the task row, so use the same ordering.
        control = cursor.execute("SELECT revision FROM subagent_controls WHERE task_id=? FOR UPDATE",
                                 (task_id,)).fetchone()
        project, binding, task = self.bindings._owned(cursor, access, task_id)
        if control is None:
            raise ProjectConflict("Project task has no durable revision")
        prior = cursor.execute("SELECT * FROM project_task_publications WHERE task_id=?", (task_id,)).fetchone()
        if prior is not None:
            if (prior["revision"] != control["revision"] or prior["artifact_sha256"] != digest
                    or prior["project_id"] != project["project_id"]):
                raise ProjectConflict("Project publication identity changed")
            return project, binding, dict(prior), None
        require_project_io_idle(cursor, project["project_id"])
        if (project["locked_task_id"] != task_id or project["lifecycle_job_id"] is not None
                or project["workspace_generation"] != binding["generation"]
                or project["lifecycle_fence"] != binding["fence"]
                or project["status"] not in {"stopped", "hibernated", "archived"}
                or task["cancel_requested"] or task["status"] not in {"completed", "partial"}):
            raise ProjectConflict("Project task cannot publish on this workspace")
        latest = cursor.execute("SELECT version_no,manifest_sha256 FROM project_versions WHERE project_id=? "
                                "ORDER BY version_no DESC LIMIT 1", (project["project_id"],)).fetchone()
        if latest is None or (latest["version_no"], latest["manifest_sha256"]) != (
                binding["base_version"], binding["manifest_sha256"]):
            raise ProjectConflict("Project advanced beyond the admitted baseline")
        proof = self._acceptance(cursor, task, control["revision"], digest)
        return project, binding, None, proof

    @staticmethod
    def _acceptance(cursor, task, revision, digest):
        result = _object(task["result_json"])
        acceptance = _object(result.get("validation")).get("acceptance", {})
        run_id = acceptance.get("run_id") if isinstance(acceptance, dict) else None
        if type(run_id) is not int or acceptance.get("status") != "passed":
            raise ProjectConflict("Project task has no passed independent acceptance")
        reviewer = cursor.execute("SELECT * FROM subagent_runs WHERE task_id=? AND run_id=?",
                                  (task["task_id"], run_id)).fetchone()
        if (reviewer is None or reviewer["status"] != "succeeded"
                or not re.fullmatch(rf"acceptance_r{revision}_[a-f0-9]{{8}}", reviewer["step_key"])):
            raise ProjectConflict("Project acceptance belongs to an obsolete or failed run")
        checks = acceptance.get("checks", [])
        if not isinstance(checks, list) or any(not isinstance(c, dict) for c in checks):
            raise ProjectConflict("Malformed project artifact checks")
        matches = [c for c in checks if c.get("artifact_key") == digest]
        if len(matches) != 1 or matches[0].get("ok") is not True:
            raise ProjectConflict("Exact project update was not checked")
        check = matches[0]
        producer = cursor.execute("SELECT * FROM subagent_runs WHERE task_id=? AND step_key=?",
                                  (task["task_id"], check.get("step"))).fetchone()
        if (producer is None or producer["run_id"] == run_id or producer["status"] != "succeeded"
                or producer["step_key"] not in json.loads(reviewer["dependencies_json"])):
            raise ProjectConflict("Project update has no independently reviewed producer")
        artifacts = _object(producer["result_json"]).get("artifacts", [])
        sources = [a for a in artifacts if isinstance(a, dict) and a.get("snapshot") == digest]
        if len(sources) != 1 or type(sources[0].get("size")) is not int:
            raise ProjectConflict("Project update has no unique immutable snapshot")
        verdicts = artifact_verdicts(matches, _object(reviewer["result_json"]), executed=True)
        if verdicts[0]["status"] != "passed":
            raise ProjectConflict("Project update failed independent review")
        # Require the reviewer to import this exact snapshot and then execute a
        # successful check in that sandbox. A different file's tool call is not proof.
        imported, executed = set(), False
        receipts = cursor.execute("""SELECT * FROM subagent_evidence
            WHERE task_id=? AND revision=? AND run_id=? AND tool_name='sandbox_exec' AND complete=1""",
            (task["task_id"], revision, run_id)).fetchall()
        executions = {}
        for receipt in receipts:
            if hashlib.sha256(receipt["payload_json"].encode()).hexdigest() == receipt["payload_hash"]:
                executions.setdefault(receipt["call_id"], []).append(receipt)
        events = cursor.execute("SELECT payload_json FROM subagent_events WHERE task_id=? AND run_id=? "
            "AND event_type='agent.tool_finished' ORDER BY sequence", (task["task_id"], run_id)).fetchall()
        for row in events:
            event = _object(row["payload_json"])
            args, output = _object(event.get("arguments")), _object(event.get("result"))
            sid = args.get("sandbox_id")
            if not isinstance(sid, str) or not re.fullmatch(r"s[a-f0-9]{6}", sid):
                continue
            if (event.get("tool_name") == "import_agent_artifact" and output.get("ok") is True
                    and output.get("sha256") == digest and output.get("read_only") is True):
                imported.add(sid)
            if event.get("tool_name") == "sandbox_exec" and sid in imported:
                matching = executions.get(event.get("call_id"), [])
                if len(matching) == 1 and _object(matching[0]["arguments_json"]) == args:
                    actual = _object(matching[0]["payload_json"])
                    if type(actual.get("returncode")) is int and actual["returncode"] == 0:
                        executed = True
        if not executed:
            raise ProjectConflict("Missing exact-snapshot independent execution evidence")
        return {"revision": revision, "acceptance_run_id": run_id, "size": sources[0]["size"]}

    def publish(self, access, task_id, artifact_sha256, content=None):
        """Replay without source bytes after commit; never unlock or restore here."""
        assert_job_owned()
        if not isinstance(artifact_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", artifact_sha256):
            raise ValueError("Invalid project artifact digest")
        with self.tasks._transaction() as cursor:
            self.projects._scope_lock(cursor, access)
            project, binding, prior, proof = self._authority(cursor, access, task_id, artifact_sha256)
            if prior is not None:
                return prior
            baseline = self.bindings._manifest(cursor, access, project["project_id"],
                {"version_no": binding["base_version"], "manifest_sha256": binding["manifest_sha256"]})
        if (not isinstance(content, bytes) or len(content) != proof["size"]
                or hashlib.sha256(content).hexdigest() != artifact_sha256):
            raise ProjectConflict("Project artifact bytes changed or expired")
        with ProjectUpdate(content, project_id=project["project_id"], base_version=binding["base_version"],
                base_manifest_sha256=binding["manifest_sha256"], baseline=baseline,
                quota_bytes=project["quota_bytes"]) as update:
            # Stream before acquiring the durable job lease row. Heartbeats stay
            # live while files are copied. Failed publication leaves only CAS
            # orphans under the existing 24-hour garbage-collection grace period.
            with self.projects._transaction() as cursor:
                self.projects._scope_lock(cursor, access)
                self.projects._blob_lock(cursor)
                now = int(time.time())
                self._check_capacity(cursor, access, project["project_id"], update.manifest, update.encoded)
                for item in update.changed:
                    assert_job_owned()
                    with update.open(item["path"]) as source:
                        blob = self.projects.blobs.put(source)
                    if (blob.sha256, blob.byte_size) != (item["sha256"], item["size"]):
                        raise ProjectConflict("Project update member checksum failed")
                    self.projects._register_blob(cursor, blob, now)
                changed = {item["path"] for item in update.changed}
                for item in update.manifest["files"]:
                    assert_job_owned()
                    if item["path"] not in changed:
                        self.projects.blobs.verify(item["sha256"], item["size"])
                manifest = self.projects.blobs.put(update.encoded)
                self.projects._register_blob(cursor, manifest, now)
                # Reused orphan blobs need a fresh grace period too; otherwise
                # GC could remove them between transfer and final publication.
                for digest in {item["sha256"] for item in update.changed} | {manifest.sha256}:
                    cursor.execute("UPDATE project_blobs SET unreferenced_since=? "
                                   "WHERE sha256=? AND unreferenced_since IS NOT NULL", (now, digest))
            with self.tasks._transaction() as cursor:
                self.projects._scope_lock(cursor, access)
                current, bound, prior, confirmed = self._authority(cursor, access, task_id, artifact_sha256)
                if prior is not None:
                    return prior
                if proof != confirmed or binding != bound or current["quota_bytes"] != project["quota_bytes"]:
                    raise ProjectConflict("Project publication authority changed during transfer")
                self.projects._blob_lock(cursor)
                receipt = self._commit(cursor, access, task_id, current, bound, proof,
                                       artifact_sha256, update.manifest, manifest, int(time.time()))
        self.tasks._notify_changed(task_id)
        return receipt

    def _check_capacity(self, cursor, access, pid, document, encoded):
        sizes = {item["sha256"]: item["size"] for item in document["files"]}
        sizes[hashlib.sha256(encoded).hexdigest()] = len(encoded)
        held = {row[0] for row in cursor.execute("""SELECT sha256 FROM project_version_blobs WHERE project_id=?
            UNION SELECT sha256 FROM project_snapshot_staging WHERE project_id=?
            UNION SELECT blob_sha256 FROM project_file_deliveries WHERE project_id=? AND blob_sha256 IS NOT NULL""",
            (pid, pid, pid)).fetchall()}
        additional = sum(size for digest, size in sizes.items() if digest not in held)
        limit = cursor.execute("SELECT quota_bytes FROM project_scope_limits WHERE scope_key=?", (access.scope.key,)).fetchone()[0]
        if (self.projects._scope_bytes(cursor, access.scope.key)
                + self.projects._scope_reserved_bytes(cursor, access.scope.key) + additional > limit):
            raise ProjectQuotaExceeded("Conversation has insufficient capacity for accepted project update")

    def _commit(self, cursor, access, task_id, project, binding, proof, artifact, document, manifest, now):
        pid = project["project_id"]
        digests = {item["sha256"] for item in document["files"]} | {manifest.sha256}
        for item in document["files"]:
            self.projects.blobs.reference(item["sha256"], item["size"])
        self.projects.blobs.reference(manifest.sha256, manifest.byte_size)
        version = binding["base_version"]
        if manifest.sha256 != binding["manifest_sha256"]:
            version += 1
            cursor.execute("""INSERT INTO project_versions(project_id,version_no,manifest_sha256,file_count,
                total_bytes,label,source_task_id,created_by_principal_id,created_at)
                VALUES (?,?,?,?,?,'',?,?,?)""", (pid, version, manifest.sha256, len(document["files"]),
                    sum(item["size"] for item in document["files"]), task_id, access.principal_id, now))
            for digest in sorted(digests):
                cursor.execute("INSERT INTO project_version_blobs(project_id,version_no,sha256) VALUES (?,?,?)",
                               (pid, version, digest))
                cursor.execute("UPDATE project_blobs SET unreferenced_since=NULL WHERE sha256=?", (digest,))
        limit = cursor.execute("SELECT quota_bytes FROM project_scope_limits WHERE scope_key=?", (access.scope.key,)).fetchone()[0]
        if (self.projects._scope_bytes(cursor, access.scope.key)
                + self.projects._scope_reserved_bytes(cursor, access.scope.key) > limit):
            raise ProjectQuotaExceeded("Conversation retained data exceeds its quota")
        receipt = cursor.execute("""INSERT INTO project_task_publications(task_id,revision,project_id,artifact_sha256,
            acceptance_run_id,version_no,manifest_sha256,created_at) VALUES (?,?,?,?,?,?,?,?) RETURNING *""",
            (task_id, proof["revision"], pid, artifact, proof["acceptance_run_id"], version, manifest.sha256, now)).fetchone()
        cursor.execute("UPDATE projects SET version=version+1,updated_at=?,last_active_at=? WHERE project_id=?", (now, now, pid))
        evidence = {**dict(receipt), "materialized": False, "task_lock_retained": True}
        self.projects._event(cursor, pid, access, "task_version_published", evidence, now)
        self.tasks.append_event(task_id, "task.project_version_published", evidence, transaction_cursor=cursor)
        self.tasks.append_checkpoint(task_id, "project_version_published", evidence, transaction_cursor=cursor)
        return dict(receipt)
