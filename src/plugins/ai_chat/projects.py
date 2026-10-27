"""Scoped project metadata and atomic version publication (PostgreSQL only).

The host constructs ProjectAccess after checking live conversation membership.
Tools/HTTP callers must never accept its membership/admin flags from a model or
request body. Cross-scope access remains forbidden even for administrators.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import threading
import time
from typing import Iterable

from src.bot_storage import DatabaseSource, open_store_connection
from .conversation_scope import ConversationScope
from .project_blobs import (
    GC_GRACE_SECONDS,
    MAX_VERSION_FILES,
    ProjectBlobStore,
    ProjectFile,
    project_path,
)


class ProjectConflict(ValueError):
    pass


class ProjectQuotaExceeded(ValueError):
    pass


def require_project_lease(cursor, project, job, now=None):
    """Host-only continuation guard; a job ID alone is never an authorization."""
    now = int(time.time()) if now is None else now
    if job is None or project["lifecycle_job_id"] != job.job_id:
        raise ProjectConflict("Project is locked by a lifecycle operation")
    row = cursor.execute(
        """SELECT * FROM durable_jobs WHERE job_id=? AND kind='project.lifecycle'
        AND scope_key=? AND status='running' AND lease_owner=? AND lease_generation=?
        AND lease_until>? FOR UPDATE""",
        (job.job_id, project["scope_key"], job.lease_owner, job.lease_generation, now),
    ).fetchone()
    if (
        row is None
        or job.kind != "project.lifecycle"
        or job.scope_key != project["scope_key"]
        or json.loads(row["payload_json"]) != job.payload
        or job.payload != {"project_id": project["project_id"]}
    ):
        raise ProjectConflict("Project lifecycle lease was lost")


@dataclass(frozen=True)
class ProjectAccess:
    scope: ConversationScope
    principal_id: int
    member: bool = False
    admin: bool = False


def validate_project_access(cursor, access: ProjectAccess):
    if not access.member or access.principal_id <= 0:
        raise PermissionError("Current conversation membership is required")
    if access.scope.kind == "private":
        row = cursor.execute(
            """SELECT 1 FROM principal_identities
            WHERE platform=? AND native_user_id=? AND principal_id=?""",
            (
                access.scope.platform,
                access.scope.native_conversation_id,
                access.principal_id,
            ),
        ).fetchone()
    else:
        row = cursor.execute(
            """SELECT 1 FROM principal_identities
            WHERE platform=? AND native_user_id=? AND principal_id=?""",
            (
                access.scope.platform,
                access.scope.actor_native_user_id,
                access.principal_id,
            ),
        ).fetchone()
    if row is None:
        raise PermissionError("Principal does not belong to this conversation identity")


class ProjectStore:
    def __init__(self, source: DatabaseSource, root: Path):
        _, self._connection = open_store_connection(source)
        self._lock = threading.RLock()
        self.blobs = ProjectBlobStore(root)

    def close(self):
        with self._lock:
            self._connection.close()

    @contextmanager
    def _transaction(self):
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute("BEGIN")
                yield cursor
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
            finally:
                cursor.close()

    @staticmethod
    def _scope_lock(cursor, access):
        validate_project_access(cursor, access)
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(?,0))",
            ("project-scope:" + access.scope.key,),
        )

    @staticmethod
    def _blob_lock(cursor):
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('project-blobs',0))"
        )

    @staticmethod
    def _get(
        cursor,
        access,
        project_id,
        *,
        edit=False,
        owner=False,
        expected=None,
        allow_trash=False,
        lifecycle_job=None,
        now=None,
    ):
        validate_project_access(cursor, access)
        row = cursor.execute(
            "SELECT * FROM projects WHERE project_id=? AND scope_key=? FOR UPDATE",
            (project_id, access.scope.key),
        ).fetchone()
        if (
            row is None
            or row["status"] == "purged"
            or (row["status"] == "trash" and not allow_trash)
        ):
            raise PermissionError("Project is not available in this conversation")
        is_owner = row["creator_principal_id"] == access.principal_id
        if access.scope.kind == "private" and not is_owner:
            raise PermissionError("Private projects are owner-only")
        if owner and not (is_owner or access.admin):
            raise PermissionError("Project owner permission required")
        if edit and not (is_owner or access.admin):
            member = cursor.execute(
                "SELECT role FROM project_members WHERE project_id=? AND principal_id=?",
                (project_id, access.principal_id),
            ).fetchone()
            if member is None or member["role"] != "editor":
                raise PermissionError("Project editor permission required")
        if expected is not None and row["version"] != expected:
            raise ProjectConflict("Project changed; reload its current version")
        if edit and row["locked_task_id"] is not None:
            raise ProjectConflict(f"Project is locked by task#{row['locked_task_id']}")
        if edit and (row["lifecycle_job_id"] is not None or lifecycle_job is not None):
            require_project_lease(cursor, row, lifecycle_job, now)
        return dict(row)

    @staticmethod
    def _event(cursor, project_id, access, kind, payload, now):
        cursor.execute(
            """INSERT INTO project_events(project_id,actor_principal_id,kind,payload,created_at)
            VALUES (?,?,?,?::jsonb,?)""",
            (
                project_id,
                access.principal_id,
                kind,
                json.dumps(payload, sort_keys=True),
                now,
            ),
        )

    def create(
        self, access, title, *, backend="oci", quota_bytes=2 * 1024**3, now=None
    ):
        now = int(time.time()) if now is None else now
        title = title.strip()
        if (
            not 1 <= len(title) <= 120
            or backend not in {"oci", "vm"}
            or quota_bytes <= 0
        ):
            raise ValueError("Invalid project title, backend or quota")
        with self._transaction() as cursor:
            self._scope_lock(cursor, access)
            group = access.scope.kind == "group"
            cursor.execute(
                """INSERT INTO project_scope_limits(scope_key,kind,quota_bytes,max_projects)
                VALUES (?,?,?,?) ON CONFLICT(scope_key) DO NOTHING""",
                (
                    access.scope.key,
                    access.scope.kind,
                    (5 if group else 2) * 1024**3,
                    5 if group else 3,
                ),
            )
            limits = cursor.execute(
                "SELECT * FROM project_scope_limits WHERE scope_key=?",
                (access.scope.key,),
            ).fetchone()
            count = cursor.execute(
                "SELECT count(*) FROM projects WHERE scope_key=? AND status<>'purged'",
                (access.scope.key,),
            ).fetchone()[0]
            if count >= limits["max_projects"]:
                raise ProjectQuotaExceeded(
                    "Conversation project count reached its limit"
                )
            if quota_bytes > limits["quota_bytes"] or (
                quota_bytes > 2 * 1024**3 and not access.admin
            ):
                raise ProjectQuotaExceeded("Project quota exceeds permitted capacity")
            row = cursor.execute(
                """INSERT INTO projects(scope_key,creator_principal_id,title,backend,quota_bytes,
                last_active_at,state_changed_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?) RETURNING *""",
                (
                    access.scope.key,
                    access.principal_id,
                    title,
                    backend,
                    quota_bytes,
                    now,
                    now,
                    now,
                    now,
                ),
            ).fetchone()
            pid = row["project_id"]
            cursor.execute(
                """INSERT INTO project_members(project_id,principal_id,role,added_by,created_at)
                VALUES (?,?,'owner',?,?)""",
                (pid, access.principal_id, access.principal_id, now),
            )
            self._focus(cursor, access, pid, now)
            self._event(cursor, pid, access, "created", {"title": title}, now)
            return dict(row)

    def get(self, access, project_id):
        with self._transaction() as cursor:
            return self._get(cursor, access, project_id)

    def list(self, access, *, include_trash=False):
        with self._transaction() as cursor:
            validate_project_access(cursor, access)
            rows = cursor.execute(
                """SELECT * FROM projects WHERE scope_key=? AND status<>'purged'
                AND (? OR status<>'trash') ORDER BY last_active_at DESC,project_id DESC LIMIT 200""",
                (access.scope.key, include_trash),
            ).fetchall()
            return [
                dict(row)
                for row in rows
                if access.scope.kind == "group"
                or row["creator_principal_id"] == access.principal_id
            ]

    @staticmethod
    def _focus(cursor, access, project_id, now):
        cursor.execute(
            """INSERT INTO project_focus(scope_key,principal_id,project_id,updated_at) VALUES (?,?,?,?)
            ON CONFLICT(scope_key,principal_id) DO UPDATE SET project_id=excluded.project_id,updated_at=excluded.updated_at""",
            (access.scope.key, access.principal_id, project_id, now),
        )

    def open(self, access, project_id, *, now=None):
        now = int(time.time()) if now is None else now
        with self._transaction() as cursor:
            row = self._get(cursor, access, project_id)
            self._focus(cursor, access, project_id, now)
            cursor.execute(
                "UPDATE projects SET last_active_at=? WHERE project_id=?",
                (now, project_id),
            )
            row["last_active_at"] = now
            return row

    @staticmethod
    def _touch_focus(cursor, access, project_id, now):
        cursor.execute(
            """UPDATE project_focus SET updated_at=?
            WHERE scope_key=? AND principal_id=? AND project_id=?""",
            (now, access.scope.key, access.principal_id, project_id),
        )

    def current(self, access, *, now=None):
        now = int(time.time()) if now is None else now
        with self._transaction() as cursor:
            validate_project_access(cursor, access)
            focus = cursor.execute(
                """SELECT project_id FROM project_focus WHERE scope_key=? AND principal_id=?
                AND updated_at>?""",
                (access.scope.key, access.principal_id, now - 24 * 3600),
            ).fetchone()
            if focus is None:
                return None
            try:
                return self._get(cursor, access, focus["project_id"])
            except PermissionError:
                return None

    def update(
        self,
        access,
        project_id,
        *,
        expected_version,
        title=None,
        rebuild_command=None,
        pinned=None,
        quota_bytes=None,
        now=None,
    ):
        now = int(time.time()) if now is None else now
        with self._transaction() as cursor:
            self._scope_lock(cursor, access)
            row = self._get(
                cursor, access, project_id, edit=True, expected=expected_version
            )
            if (pinned is not None or quota_bytes is not None) and not access.admin:
                raise PermissionError(
                    "Only administrators can adjust project quota or pinning"
                )
            values = dict(
                title=row["title"] if title is None else title.strip(),
                rebuild_command=row["rebuild_command"]
                if rebuild_command is None
                else rebuild_command,
                pinned=row["pinned"] if pinned is None else bool(pinned),
                quota_bytes=row["quota_bytes"]
                if quota_bytes is None
                else int(quota_bytes),
            )
            if (
                not 1 <= len(values["title"]) <= 120
                or len(values["rebuild_command"]) > 4000
                or values["quota_bytes"] <= 0
                or values["quota_bytes"] < row["used_bytes"]
            ):
                raise ValueError("Invalid metadata or quota below current usage")
            updated = cursor.execute(
                """UPDATE projects SET title=?,rebuild_command=?,pinned=?,quota_bytes=?,
                version=version+1,updated_at=? WHERE project_id=? RETURNING *""",
                (*values.values(), now, project_id),
            ).fetchone()
            self._touch_focus(cursor, access, project_id, now)
            self._event(
                cursor,
                project_id,
                access,
                "updated",
                {"version": updated["version"]},
                now,
            )
            return dict(updated)

    def set_member(
        self, access, project_id, principal_id, role, *, expected_version, now=None
    ):
        now = int(time.time()) if now is None else now
        if role not in {"editor", "viewer", None}:
            raise ValueError("Only editor/viewer grants are supported")
        with self._transaction() as cursor:
            row = self._get(
                cursor, access, project_id, owner=True, expected=expected_version
            )
            if (
                principal_id == row["creator_principal_id"]
                or access.scope.kind == "private"
            ):
                raise PermissionError(
                    "Cannot replace the owner or share a private project"
                )
            if role is None:
                cursor.execute(
                    "DELETE FROM project_members WHERE project_id=? AND principal_id=?",
                    (project_id, principal_id),
                )
            else:
                cursor.execute(
                    """INSERT INTO project_members(project_id,principal_id,role,added_by,created_at)
                    VALUES (?,?,?,?,?) ON CONFLICT(project_id,principal_id) DO UPDATE SET role=excluded.role,added_by=excluded.added_by""",
                    (project_id, principal_id, role, access.principal_id, now),
                )
            cursor.execute(
                "UPDATE projects SET version=version+1,updated_at=? WHERE project_id=?",
                (now, project_id),
            )
            self._event(
                cursor,
                project_id,
                access,
                "member_changed",
                {"principal_id": principal_id, "role": role},
                now,
            )

    @staticmethod
    def _register_blob(cursor, blob, now):
        cursor.execute(
            """INSERT INTO project_blobs(sha256,byte_size,storage_path,created_at,unreferenced_since)
            VALUES (?,?,?,?,?) ON CONFLICT(sha256) DO NOTHING""",
            (blob.sha256, blob.byte_size, blob.storage_path, now, now),
        )

    @staticmethod
    def _scope_bytes(cursor, scope_key):
        return int(
            cursor.execute(
                """SELECT COALESCE((SELECT sum(used_bytes) FROM projects WHERE scope_key=?),0)
            + COALESCE((SELECT sum(b.byte_size) FROM project_blobs b JOIN
            (SELECT DISTINCT r.project_id,r.sha256 FROM project_version_blobs r JOIN projects p USING(project_id)
             WHERE p.scope_key=?) refs USING(sha256)),0)""",
                (scope_key, scope_key),
            ).fetchone()[0]
        )

    def snapshot(
        self,
        access,
        project_id,
        files: Iterable[ProjectFile],
        *,
        expected_version,
        label="",
        source_task_id=None,
        source_turn_id=None,
        now=None,
        force_version=False,
        lifecycle_job=None,
    ):
        supplied_now = now
        now = int(time.time()) if now is None else now
        if len(label) > 120:
            raise ValueError("Version label exceeds 120 characters")
        with self._transaction() as cursor:
            self._scope_lock(cursor, access)
            row = self._get(
                cursor,
                access,
                project_id,
                edit=True,
                expected=expected_version,
                lifecycle_job=lifecycle_job,
                now=supplied_now,
            )
            if lifecycle_job is not None:
                step = cursor.execute(
                    """SELECT 1 FROM project_lifecycle_runs r
                    JOIN project_lifecycle_steps s ON s.job_id=r.job_id AND s.step_index=r.next_step
                    WHERE r.job_id=? AND r.project_id=? AND r.generation=? AND r.status='running'
                    AND s.status='pending' AND s.operation='snapshot_workspace' AND s.dispatch_lease=?""",
                    (
                        lifecycle_job.job_id,
                        project_id,
                        row["workspace_generation"],
                        lifecycle_job.lease_generation,
                    ),
                ).fetchone()
                if step is None:
                    raise ProjectConflict("No leased lifecycle snapshot is pending")
            self._blob_lock(cursor)
            for table, column, identifier in (
                ("subagent_tasks", "task_id", source_task_id),
                ("agent_turns", "turn_id", source_turn_id),
            ):
                if identifier is not None:
                    source = cursor.execute(
                        f"SELECT scope_key FROM {table} WHERE {column}=?", (identifier,)
                    ).fetchone()
                    if source is None or source["scope_key"] != access.scope.key:
                        raise PermissionError(
                            "Version evidence belongs to another conversation"
                        )
            manifest, names, total, digests = [], set(), 0, set()
            for item in files:
                if len(manifest) >= MAX_VERSION_FILES:
                    raise ValueError("Version exceeds 20000 files")
                name = project_path(item.path)
                folded = name.casefold()
                if item.mode not in (0o644, 0o755) or folded in names:
                    raise ValueError("Duplicate path or unsafe file mode")
                names.add(folded)
                blob = self.blobs.put(item.content)
                total += blob.byte_size
                if total > row["quota_bytes"]:
                    raise ProjectQuotaExceeded("Retained files exceed project quota")
                self._register_blob(cursor, blob, now)
                digests.add(blob.sha256)
                manifest.append(
                    {
                        "path": name,
                        "sha256": blob.sha256,
                        "size": blob.byte_size,
                        "mode": item.mode,
                    }
                )
            for name in names:
                if any(
                    "/".join(name.split("/")[:i]) in names
                    for i in range(1, len(name.split("/")))
                ):
                    raise ValueError("A file cannot also be a directory")
            manifest.sort(key=lambda item: item["path"])
            encoded = json.dumps(
                {"format": 1, "files": manifest}, sort_keys=True, separators=(",", ":")
            ).encode()
            blob = self.blobs.put(encoded)
            self._register_blob(cursor, blob, now)
            digests.add(blob.sha256)
            previous = cursor.execute(
                "SELECT * FROM project_versions WHERE project_id=? ORDER BY version_no DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            if (
                not force_version
                and previous is not None
                and previous["manifest_sha256"] == blob.sha256
            ):
                if label and label != previous["label"]:
                    previous = cursor.execute(
                        """UPDATE project_versions SET label=?
                        WHERE project_id=? AND version_no=? RETURNING *""",
                        (label, project_id, previous["version_no"]),
                    ).fetchone()
                    cursor.execute(
                        "UPDATE projects SET version=version+1,updated_at=? WHERE project_id=?",
                        (now, project_id),
                    )
                    self._event(
                        cursor,
                        project_id,
                        access,
                        "version_labelled",
                        {"version_no": previous["version_no"], "label": label},
                        now,
                    )
                cursor.execute(
                    "UPDATE projects SET last_active_at=? WHERE project_id=?",
                    (now, project_id),
                )
                self._touch_focus(cursor, access, project_id, now)
                if lifecycle_job is not None:
                    require_project_lease(cursor, row, lifecycle_job, supplied_now)
                return dict(previous)
            version = 1 if previous is None else previous["version_no"] + 1
            record = cursor.execute(
                """INSERT INTO project_versions(project_id,version_no,manifest_sha256,file_count,
                total_bytes,label,source_task_id,source_turn_id,created_by_principal_id,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?) RETURNING *""",
                (
                    project_id,
                    version,
                    blob.sha256,
                    len(manifest),
                    total,
                    label,
                    source_task_id,
                    source_turn_id,
                    access.principal_id,
                    now,
                ),
            ).fetchone()
            for digest in sorted(digests):
                cursor.execute(
                    "INSERT INTO project_version_blobs(project_id,version_no,sha256) VALUES (?,?,?)",
                    (project_id, version, digest),
                )
                cursor.execute(
                    "UPDATE project_blobs SET unreferenced_since=NULL WHERE sha256=?",
                    (digest,),
                )
            limit = cursor.execute(
                "SELECT quota_bytes FROM project_scope_limits WHERE scope_key=?",
                (access.scope.key,),
            ).fetchone()[0]
            if self._scope_bytes(cursor, access.scope.key) > limit:
                raise ProjectQuotaExceeded(
                    "Conversation retained data exceeds its quota"
                )
            cursor.execute(
                "UPDATE projects SET version=version+1,last_active_at=?,updated_at=? WHERE project_id=?",
                (now, now, project_id),
            )
            self._touch_focus(cursor, access, project_id, now)
            self._event(
                cursor,
                project_id,
                access,
                "version_created",
                {"version_no": version, "files": len(manifest)},
                now,
            )
            if lifecycle_job is not None:
                require_project_lease(cursor, row, lifecycle_job, supplied_now)
            return dict(record)

    def versions(self, access, project_id):
        with self._transaction() as cursor:
            self._get(cursor, access, project_id)
            return [
                dict(row)
                for row in cursor.execute(
                    """SELECT * FROM project_versions WHERE project_id=?
                ORDER BY version_no DESC LIMIT 200""",
                    (project_id,),
                ).fetchall()
            ]

    def _manifest(self, cursor, access, project_id, version_no):
        self._get(cursor, access, project_id)
        version = cursor.execute(
            "SELECT * FROM project_versions WHERE project_id=? AND version_no=?",
            (project_id, version_no),
        ).fetchone()
        if version is None:
            raise ValueError("Project version does not exist")
        return json.loads(self.blobs.read(version["manifest_sha256"]))

    def manifest(self, access, project_id, version_no):
        with self._transaction() as cursor:
            return self._manifest(cursor, access, project_id, version_no)

    def read_file(self, access, project_id, version_no, path):
        name = project_path(path)
        with self._transaction() as cursor:
            manifest = self._manifest(cursor, access, project_id, version_no)
            item = next(
                (item for item in manifest["files"] if item["path"] == name), None
            )
            if item is None:
                raise FileNotFoundError(name)
            return self.blobs.read(item["sha256"])

    def restore(self, access, project_id, version_no, *, expected_version, now=None):
        with self._transaction() as cursor:
            self._get(cursor, access, project_id, edit=True, expected=expected_version)
            manifest = self._manifest(cursor, access, project_id, version_no)
        files = (
            ProjectFile(item["path"], self.blobs.read(item["sha256"]), item["mode"])
            for item in manifest["files"]
        )
        return self.snapshot(
            access,
            project_id,
            files,
            expected_version=expected_version,
            force_version=True,
            now=now,
        )

    def pin_version(
        self, access, project_id, version_no, *, pinned, expected_version, now=None
    ):
        now = int(time.time()) if now is None else now
        with self._transaction() as cursor:
            self._get(cursor, access, project_id, edit=True, expected=expected_version)
            row = cursor.execute(
                """UPDATE project_versions SET pinned=? WHERE project_id=? AND version_no=? RETURNING *""",
                (bool(pinned), project_id, version_no),
            ).fetchone()
            if row is None:
                raise ValueError("Project version does not exist")
            cursor.execute(
                "UPDATE projects SET version=version+1,updated_at=? WHERE project_id=?",
                (now, project_id),
            )
            self._event(
                cursor,
                project_id,
                access,
                "version_pinned",
                {"version_no": version_no, "pinned": bool(pinned)},
                now,
            )

    def prune_versions(self, access, project_id, keep, *, expected_version, now=None):
        """M2 computes retention; this boundary additionally protects durable references."""
        now = int(time.time()) if now is None else now
        keep = {int(number) for number in keep}
        removed = []
        with self._transaction() as cursor:
            self._scope_lock(cursor, access)
            self._get(
                cursor,
                access,
                project_id,
                edit=True,
                owner=True,
                expected=expected_version,
            )
            self._blob_lock(cursor)
            versions = cursor.execute(
                "SELECT * FROM project_versions WHERE project_id=? ORDER BY version_no DESC",
                (project_id,),
            ).fetchall()
            if versions:
                keep.add(versions[0]["version_no"])
            for version in versions:
                number = version["version_no"]
                if number in keep or version["pinned"] or version["label"]:
                    continue
                if cursor.execute(
                    """SELECT 1 FROM project_previews WHERE project_id=? AND version_no=?
                    UNION ALL SELECT 1 FROM subagent_tasks WHERE project_id=? AND project_base_version=? LIMIT 1""",
                    (project_id, number, project_id, number),
                ).fetchone():
                    continue
                cursor.execute(
                    "DELETE FROM project_versions WHERE project_id=? AND version_no=?",
                    (project_id, number),
                )
                removed.append(number)
            cursor.execute(
                """UPDATE project_blobs b SET unreferenced_since=? WHERE unreferenced_since IS NULL
                AND NOT EXISTS(SELECT 1 FROM project_version_blobs r WHERE r.sha256=b.sha256)
                AND NOT EXISTS(SELECT 1 FROM project_versions v WHERE v.manifest_sha256=b.sha256)""",
                (now,),
            )
            if removed:
                cursor.execute(
                    "UPDATE projects SET version=version+1,updated_at=? WHERE project_id=?",
                    (now, project_id),
                )
                self._event(
                    cursor,
                    project_id,
                    access,
                    "versions_pruned",
                    {"versions": removed},
                    now,
                )
        return removed

    def usage(self, access):
        with self._transaction() as cursor:
            validate_project_access(cursor, access)
            return {
                "scope_key": access.scope.key,
                "total_bytes": self._scope_bytes(cursor, access.scope.key),
            }

    def set_scope_limits(
        self, access, *, quota_bytes, max_projects, expected_version, now=None
    ):
        if not access.admin:
            raise PermissionError("Only administrators can adjust conversation quotas")
        if quota_bytes <= 0 or not 1 <= max_projects <= 1000:
            raise ValueError("Invalid conversation quota")
        with self._transaction() as cursor:
            self._scope_lock(cursor, access)
            limits = cursor.execute(
                "SELECT * FROM project_scope_limits WHERE scope_key=? FOR UPDATE",
                (access.scope.key,),
            ).fetchone()
            if limits is None or limits["version"] != expected_version:
                raise ProjectConflict("Conversation quota changed")
            if self._scope_bytes(cursor, access.scope.key) > quota_bytes:
                raise ProjectQuotaExceeded("Quota is below retained conversation data")
            cursor.execute(
                """UPDATE project_scope_limits SET quota_bytes=?,max_projects=?,version=version+1
                WHERE scope_key=?""",
                (quota_bytes, max_projects, access.scope.key),
            )

    def events(self, access, project_id, *, before=None, limit=50):
        with self._transaction() as cursor:
            self._get(cursor, access, project_id)
            return [
                dict(row)
                for row in cursor.execute(
                    """SELECT * FROM project_events WHERE project_id=?
                AND event_id<? ORDER BY event_id DESC LIMIT ?""",
                    (
                        project_id,
                        before if before is not None else 2**63 - 1,
                        min(max(limit, 1), 200),
                    ),
                ).fetchall()
            ]

    def collect_blobs(self, *, now=None, limit=100):
        """Internal maintenance only; no scope/model accepts a raw blob digest."""
        now = int(time.time()) if now is None else now
        deleted = []
        with self._transaction() as cursor:
            self._blob_lock(cursor)
            rows = cursor.execute(
                """SELECT sha256 FROM project_blobs b WHERE unreferenced_since<=?
                AND NOT EXISTS(SELECT 1 FROM project_version_blobs r WHERE r.sha256=b.sha256)
                AND NOT EXISTS(SELECT 1 FROM project_versions v WHERE v.manifest_sha256=b.sha256)
                ORDER BY unreferenced_since LIMIT ? FOR UPDATE""",
                (now - GC_GRACE_SECONDS, min(max(limit, 1), 1000)),
            ).fetchall()
            for row in rows:
                self.blobs.remove(row["sha256"])
                cursor.execute(
                    "DELETE FROM project_blobs WHERE sha256=?", (row["sha256"],)
                )
                deleted.append(row["sha256"])
            for digest in self.blobs.orphan_candidates(now - GC_GRACE_SECONDS):
                if len(deleted) >= min(max(limit, 1), 1000):
                    break
                if (
                    cursor.execute(
                        "SELECT 1 FROM project_blobs WHERE sha256=?", (digest,)
                    ).fetchone()
                    is None
                ):
                    self.blobs.remove(digest)
                    deleted.append(digest)
            self.blobs.collect_staging(
                now - GC_GRACE_SECONDS, limit=min(max(limit, 1), 1000)
            )
        return deleted
