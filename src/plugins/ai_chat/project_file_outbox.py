"""Immutable project attachments, atomically linked to tool receipts and Outbox."""

import hashlib
import json
from pathlib import PurePosixPath
import re
import time

from src.storage_admission import require_capacity
from .message_ir import MediaNode, MessageBody
from .projects import ProjectConflict, ProjectQuotaExceeded
from .project_io import ProjectIOGrant
from .project_usage import ProjectQuotaRejected

FILE_PREFIX = "project-file:"


def delivery_name(value):
    name = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', "_", value).strip().strip(".")
    if not name or len(name.encode()) > 180:
        raise ValueError("Invalid project attachment filename")
    return name


class ProjectFileOutbox:
    def __init__(self, io, deliveries):
        self.io, self.projects, self.deliveries = io, io.projects, deliveries

    def queue(self, access, grant, content, path, filename=None):
        if not isinstance(content, bytes) or not 0 < len(content) <= 256 * 1024**2:
            raise ValueError("Empty or oversized project attachment")
        digest = hashlib.sha256(content).hexdigest()
        filename = delivery_name(filename or PurePosixPath(path).name)
        # An operation-specific name makes reconciliation distinguish even two
        # deliberate sends of identical bytes, not just their size and filename.
        filename = (
            f"prj-{grant.project_id}-{grant.operation_id}-{digest[:10]}-{filename}"
        )
        now = int(time.time())
        with self.projects._transaction() as cursor:
            self.projects._scope_lock(cursor, access)
            self.projects._get(cursor, access, grant.project_id)
            _, operation = self.io._owned(cursor, grant)
            if (
                operation["actor_principal_id"] != access.principal_id
                or operation["tool_name"] != "send_file_from_sandbox"
            ):
                raise PermissionError(
                    "Attachment does not belong to this tool invocation"
                )
            prior = cursor.execute(
                "SELECT * FROM project_file_deliveries WHERE operation_id=?",
                (grant.operation_id,),
            ).fetchone()
            if prior:
                if (prior["sha256"], prior["path"], prior["filename"]) != (
                    digest,
                    path,
                    filename,
                ):
                    raise ProjectConflict("Project attachment changed on replay")
                return operation["result"]
            if operation["state"] != "dispatched" or operation["result"] is not None:
                raise ProjectConflict("Attachment requires an unsettled read grant")
            limit = cursor.execute(
                "SELECT quota_bytes FROM project_scope_limits WHERE scope_key=?",
                (access.scope.key,),
            ).fetchone()[0]
            self.projects._blob_lock(cursor)
            already_counted = cursor.execute(
                """SELECT 1 FROM (
                SELECT project_id,sha256 FROM project_version_blobs UNION
                SELECT project_id,sha256 FROM project_snapshot_staging UNION
                SELECT project_id,blob_sha256 AS sha256 FROM project_file_deliveries) refs
                WHERE project_id=? AND sha256=? LIMIT 1""",
                (grant.project_id, digest),
            ).fetchone()
            extra = 0 if already_counted else len(content)
            peers = cursor.execute(
                """SELECT p.*,u.complete,u.generation,u.fence,u.sample FROM projects p
                LEFT JOIN project_usage u USING(project_id) WHERE p.scope_key=? AND p.status<>'purged'""",
                (access.scope.key,),
            ).fetchall()
            if any(not self.io.usage._fresh(peer, peer, now) for peer in peers):
                raise ProjectQuotaRejected("project_scope_usage_unknown")
            if (
                self.projects._scope_bytes(cursor, access.scope.key)
                + self.projects._scope_reserved_bytes(cursor, access.scope.key)
                + extra
                > limit
            ):
                raise ProjectQuotaExceeded(
                    "Project attachment exceeds conversation quota"
                )
            require_capacity(len(content))
            blob = self.projects.blobs.put(content)
            self.projects._register_blob(cursor, blob, now)
            body = MessageBody(
                (MediaNode(0, "file", name=filename, source_type="project_attachment"),)
            )
            delivery, _ = self.deliveries.enqueue(
                idempotency_key=FILE_PREFIX + str(grant.operation_id),
                source_scope_key=access.scope.key,
                target_scope=access.scope,
                body=body,
                transaction_cursor=cursor,
            )
            cursor.execute(
                """INSERT INTO project_file_deliveries
                (delivery_id,operation_id,project_id,sha256,blob_sha256,byte_size,filename,path)
                VALUES (?,?,?,?,?,?,?,?)""",
                (
                    delivery.delivery_id,
                    grant.operation_id,
                    grant.project_id,
                    digest,
                    digest,
                    len(content),
                    filename,
                    path,
                ),
            )
            if (
                self.projects._scope_bytes(cursor, access.scope.key)
                + self.projects._scope_reserved_bytes(cursor, access.scope.key)
                > limit
            ):
                raise ProjectQuotaExceeded(
                    "Project attachment exceeds conversation quota"
                )
            result = {
                "ok": True,
                "queued": True,
                "delivered": False,
                "delivery_handle": delivery.handle,
                "delivery_id": delivery.delivery_id,
                "filename": filename,
                "size": len(content),
                "project_handle": f"prj#{grant.project_id}",
                "operation_id": grant.operation_id,
                "message": "文件已保存并排队，尚未确认送达；请勿声称已经发送成功。",
            }
            cursor.execute(
                "UPDATE project_io SET result=?::jsonb,updated_at=? WHERE operation_id=?",
                (json.dumps(result), now, grant.operation_id),
            )
            self.projects._event(
                cursor,
                grant.project_id,
                access,
                "file_queued",
                {
                    "operation_id": grant.operation_id,
                    "delivery_id": delivery.delivery_id,
                    "sha256": digest,
                },
                now,
            )
            return result

    def identity(self, delivery):
        with self.projects._transaction() as cursor:
            row = cursor.execute(
                """SELECT f.*,p.scope_key,p.bot_native_user_id,p.creator_principal_id,
                io.actor_principal_id,io.state AS operation_state
                FROM project_file_deliveries f JOIN projects p USING(project_id)
                JOIN project_io io ON io.operation_id=f.operation_id WHERE f.delivery_id=?""",
                (delivery.delivery_id,),
            ).fetchone()
            if (
                row is None
                or delivery.idempotency_key != FILE_PREFIX + str(row["operation_id"])
                or row["scope_key"] != delivery.source_scope_key
                or row["scope_key"] != delivery.target_scope.key
            ):
                raise PermissionError("Project attachment delivery identity mismatch")
            return dict(row)

    def prepare(self, access, delivery):
        identity = self.identity(delivery)
        with self.projects._transaction() as cursor:
            self.projects._scope_lock(cursor, access)
            self.projects._get(cursor, access, identity["project_id"])
            if identity["actor_principal_id"] != access.principal_id:
                raise PermissionError("Project file belongs to another actor")
            if identity["operation_state"] != "completed":
                raise ProjectConflict("Project read has not settled")
            self.projects._blob_lock(cursor)
            if identity["blob_sha256"] != identity["sha256"]:
                raise ValueError("Attachment retention has ended")
            content = self.projects.blobs.read(identity["sha256"])
            if len(content) != identity["byte_size"]:
                raise ValueError("Project attachment integrity check failed")
            return content

    def begin_upload(self, delivery):
        now = int(time.time())
        with self.projects._transaction() as cursor:
            row = cursor.execute(
                "SELECT * FROM deliveries WHERE delivery_id=? FOR UPDATE",
                (delivery.delivery_id,),
            ).fetchone()
            if (
                row is None
                or row["status"] != "sending"
                or row["attempts"] != delivery.attempts
                or not row["lease_until"]
                or row["lease_until"] <= now
            ):
                raise ProjectConflict("Attachment delivery lease changed")
            cursor.execute(
                """UPDATE project_file_deliveries SET upload_started_at=?
                WHERE delivery_id=? AND upload_started_at IS NULL""",
                (now, delivery.delivery_id),
            )
            if cursor.rowcount != 1:
                raise ProjectConflict("Attachment upload was already dispatched")
        return now

    def confirm(self, delivery, file_id):
        if not isinstance(file_id, str) or not file_id.strip():
            raise ValueError("File receipt is missing")
        now = int(time.time())
        with self.projects._transaction() as cursor:
            if not cursor.execute(
                "SELECT 1 FROM project_file_deliveries WHERE delivery_id=? AND upload_started_at IS NOT NULL",
                (delivery.delivery_id,),
            ).fetchone():
                raise ProjectConflict("No dispatched project file to confirm")
            cursor.execute(
                """UPDATE deliveries SET status='committed',native_message_id=?,lease_until=NULL,
                last_error='',updated_at=? WHERE delivery_id=? AND status IN ('sending','ambiguous','failed','cancelled')""",
                ("file:" + file_id, now, delivery.delivery_id),
            )
            if cursor.rowcount:
                self.deliveries._finish_attempt(
                    cursor,
                    delivery.delivery_id,
                    "committed",
                    "project file receipt",
                    now,
                )

    def reconcile_due(self, *, limit=20):
        now = int(time.time())
        with self.projects._transaction() as cursor:
            rows = cursor.execute(
                """SELECT f.delivery_id FROM project_file_deliveries f
                JOIN deliveries d USING(delivery_id) WHERE d.status IN ('ambiguous','failed','cancelled')
                AND f.upload_started_at IS NOT NULL AND f.next_reconcile_at<=?
                ORDER BY f.next_reconcile_at,f.delivery_id LIMIT ? FOR UPDATE OF f SKIP LOCKED""",
                (now, limit),
            ).fetchall()
            for row in rows:
                cursor.execute(
                    "UPDATE project_file_deliveries SET next_reconcile_at=? WHERE delivery_id=?",
                    (now + 60, row["delivery_id"]),
                )
        return [self.deliveries.get(row["delivery_id"]) for row in rows]

    def resume_unsent(self):
        # A crash before durable dispatch is provably not an upload attempt.
        with self.projects._transaction() as cursor:
            rows = cursor.execute(
                """UPDATE deliveries d SET status='pending',next_attempt_at=?,last_error='',
                attempts=greatest(attempts-1,0)
                FROM project_file_deliveries f WHERE f.delivery_id=d.delivery_id
                AND d.status='ambiguous' AND f.upload_started_at IS NULL RETURNING d.delivery_id,d.attempts+1 AS previous_attempt""",
                (int(time.time()),),
            ).fetchall()
            for row in rows:
                cursor.execute(
                    "DELETE FROM delivery_attempts WHERE delivery_id=? AND attempt=?",
                    (row["delivery_id"], row["previous_attempt"]),
                )

    def pending_reads(self):
        now = int(time.time())
        with self.projects._transaction() as cursor:
            rows = cursor.execute(
                """SELECT io.*,p.scope_key,p.bot_native_user_id,p.creator_principal_id
                FROM project_io io JOIN project_file_deliveries f USING(operation_id)
                JOIN projects p ON p.project_id=io.project_id
                WHERE io.state IN ('dispatched','needs_attention') AND io.result IS NOT NULL
                AND f.next_reconcile_at<=?
                ORDER BY f.next_reconcile_at,io.operation_id LIMIT 20 FOR UPDATE OF f SKIP LOCKED""",
                (now,),
            ).fetchall()
            for row in rows:
                cursor.execute(
                    "UPDATE project_file_deliveries SET next_reconcile_at=? WHERE operation_id=?",
                    (now + 60, row["operation_id"]),
                )
            return [
                (
                    ProjectIOGrant(
                        row["operation_id"], row["project_id"], row["token"]
                    ),
                    dict(row),
                )
                for row in rows
            ]

    def status(self, access, project_id):
        with self.projects._transaction() as cursor:
            self.projects._scope_lock(cursor, access)
            self.projects._get(cursor, access, project_id)
            return [
                dict(row)
                for row in cursor.execute(
                    """SELECT f.delivery_id,f.filename,f.byte_size,d.status,d.last_error,d.updated_at
                FROM project_file_deliveries f JOIN deliveries d USING(delivery_id)
                WHERE f.project_id=? ORDER BY f.delivery_id DESC LIMIT 5""",
                    (project_id,),
                ).fetchall()
            ]
