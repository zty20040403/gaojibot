from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .database import PostgresDatabase


class MediaCleanupError(RuntimeError):
    pass


@dataclass(frozen=True)
class CleanupCandidate:
    media_id: int
    sha256: str
    byte_size: int
    storage_path: str
    prepared_path: str
    archive_path: str
    ordinary_links: int


class LegacyMediaCleanup:
    """Audited removal of inert ordinary-image data from the old media design."""

    KIND = "legacy-ordinary-images-v1"

    def __init__(
        self,
        database: PostgresDatabase,
        *,
        media_root: Path,
        archive_root: Path | None = None,
    ) -> None:
        self.database = database
        self.media_root = media_root.expanduser().resolve()
        self.archive_root = (
            archive_root.expanduser().resolve()
            if archive_root is not None
            else None
        )

    def preview(self) -> dict[str, object]:
        candidates = self._candidates()
        total_bytes = sum(item.byte_size for item in candidates)
        return {
            "kind": self.KIND,
            "candidate_count": len(candidates),
            "candidate_bytes": total_bytes,
            "ordinary_links": sum(item.ordinary_links for item in candidates),
            "confirmation_token": self._confirmation_token(candidates),
            "recent_runs": self._recent_runs(),
        }

    def apply(self, confirmation_token: str) -> dict[str, object]:
        candidates = self._candidates()
        expected = self._confirmation_token(candidates)
        supplied = str(confirmation_token).strip()
        if not candidates:
            raise MediaCleanupError("there are no legacy ordinary images to clean")
        if not supplied or supplied != expected:
            raise MediaCleanupError(
                "cleanup preview changed or confirmation token is invalid"
            )

        now = int(time.time())
        connection = self.database.store_connection()
        try:
            row = connection.execute(
                """
                INSERT INTO media_cleanup_runs (
                    cleanup_kind, confirmation_token, candidate_count,
                    candidate_bytes, deleted_count, status, report_json,
                    requested_at
                ) VALUES (?, ?, ?, ?, 0, 'running', '{}', ?)
                RETURNING cleanup_id
                """,
                (self.KIND, expected, len(candidates),
                 sum(item.byte_size for item in candidates), now),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise MediaCleanupError("could not create cleanup audit record")
        cleanup_id = int(row["cleanup_id"])

        deleted_ids: list[int] = []
        transaction = self.database.store_connection()
        cursor = transaction.cursor()
        try:
            for item in candidates:
                cursor.execute(
                    """
                    DELETE FROM message_media AS ordinary
                    WHERE ordinary.media_id = ?
                      AND ordinary.media_kind = 'image'
                      AND ordinary.source_type <> 'qq-official.caption'
                      AND NOT EXISTS (
                          SELECT 1 FROM message_media AS sticker_link
                          WHERE sticker_link.media_id = ordinary.media_id
                            AND sticker_link.media_kind = 'sticker'
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM sticker_library AS sticker
                          WHERE sticker.media_id = ordinary.media_id
                      )
                    """,
                    (item.media_id,),
                )
                cursor.execute(
                    """
                    DELETE FROM media_blobs AS blob
                    WHERE blob.media_id = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM message_media AS link
                          WHERE link.media_id = blob.media_id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM sticker_library AS sticker
                          WHERE sticker.media_id = blob.media_id
                      )
                    """,
                    (item.media_id,),
                )
                if cursor.rowcount == 1:
                    deleted_ids.append(item.media_id)
                    cursor.execute(
                        """
                        DELETE FROM semantic_documents
                        WHERE source_type = 'media' AND source_handle = ?
                        """,
                        (f"media#{item.media_id}",),
                    )
            transaction.commit()
        except Exception as exc:
            transaction.rollback()
            self._finish_audit(
                cleanup_id,
                status="failed",
                deleted_count=0,
                report={"error": self._safe_error(exc)},
            )
            raise MediaCleanupError("legacy media cleanup transaction failed") from exc
        finally:
            transaction.close()

        deleted_id_set = set(deleted_ids)
        deleted_candidates = [
            item for item in candidates if item.media_id in deleted_id_set
        ]
        removed_files = 0
        missing_files = 0
        file_errors: list[str] = []
        seen_paths: set[Path] = set()
        for item in deleted_candidates:
            path_specs = (
                (self.media_root, item.storage_path),
                (self.media_root, item.prepared_path),
                (self.archive_root, item.archive_path),
            )
            for root, relative in path_specs:
                if root is None or not relative:
                    continue
                try:
                    path = self._safe_path(root, relative)
                    if path in seen_paths:
                        continue
                    seen_paths.add(path)
                    if not path.exists():
                        missing_files += 1
                        continue
                    path.unlink()
                    removed_files += 1
                except (OSError, MediaCleanupError) as exc:
                    file_errors.append(f"{relative}: {self._safe_error(exc)}")

        status = (
            "completed"
            if len(deleted_ids) == len(candidates) and not file_errors
            else "partial"
        )
        report: dict[str, object] = {
            "cleanup_id": cleanup_id,
            "kind": self.KIND,
            "status": status,
            "candidate_count": len(candidates),
            "deleted_count": len(deleted_ids),
            "deleted_bytes": sum(item.byte_size for item in deleted_candidates),
            "removed_files": removed_files,
            "missing_files": missing_files,
            "file_errors": file_errors[:20],
        }
        self._finish_audit(
            cleanup_id,
            status=status,
            deleted_count=len(deleted_ids),
            report=report,
        )
        return report

    def _candidates(self) -> list[CleanupCandidate]:
        connection = self.database.store_connection()
        try:
            rows = connection.execute(
                """
                SELECT blob.media_id, blob.sha256, blob.byte_size,
                       blob.storage_path, blob.prepared_path,
                       COALESCE(blob.archive_path, '') AS archive_path,
                       (
                           SELECT COUNT(*) FROM message_media AS ordinary
                           WHERE ordinary.media_id = blob.media_id
                             AND ordinary.media_kind = 'image'
                       ) AS ordinary_links
                FROM media_blobs AS blob
                WHERE NOT EXISTS (
                    SELECT 1 FROM message_media official
                    WHERE official.media_id=blob.media_id AND official.source_type='qq-official.caption'
                ) AND EXISTS (
                    SELECT 1 FROM message_media AS ordinary
                    WHERE ordinary.media_id = blob.media_id
                      AND ordinary.media_kind = 'image'
                )
                  AND NOT EXISTS (
                    SELECT 1 FROM message_media AS sticker_link
                    WHERE sticker_link.media_id = blob.media_id
                      AND sticker_link.media_kind = 'sticker'
                )
                  AND NOT EXISTS (
                    SELECT 1 FROM sticker_library AS sticker
                    WHERE sticker.media_id = blob.media_id
                )
                ORDER BY blob.media_id
                """
            ).fetchall()
        finally:
            connection.close()
        return [
            CleanupCandidate(
                media_id=int(row["media_id"]),
                sha256=str(row["sha256"]),
                byte_size=int(row["byte_size"]),
                storage_path=str(row["storage_path"] or ""),
                prepared_path=str(row["prepared_path"] or ""),
                archive_path=str(row["archive_path"] or ""),
                ordinary_links=int(row["ordinary_links"]),
            )
            for row in rows
        ]

    def _recent_runs(self, limit: int = 10) -> list[dict[str, object]]:
        connection = self.database.store_connection()
        try:
            rows = connection.execute(
                """
                SELECT cleanup_id, cleanup_kind, candidate_count,
                       candidate_bytes, deleted_count, status,
                       requested_at, finished_at
                FROM media_cleanup_runs
                ORDER BY requested_at DESC, cleanup_id DESC
                LIMIT ?
                """,
                (min(max(int(limit), 1), 50),),
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def _finish_audit(
        self,
        cleanup_id: int,
        *,
        status: str,
        deleted_count: int,
        report: dict[str, object],
    ) -> None:
        connection = self.database.store_connection()
        try:
            connection.execute(
                """
                UPDATE media_cleanup_runs
                SET status = ?, deleted_count = ?, report_json = ?, finished_at = ?
                WHERE cleanup_id = ?
                """,
                (
                    status,
                    int(deleted_count),
                    json.dumps(report, ensure_ascii=False),
                    int(time.time()),
                    int(cleanup_id),
                ),
            )
        finally:
            connection.close()

    @classmethod
    def _confirmation_token(cls, candidates: list[CleanupCandidate]) -> str:
        payload = json.dumps(
            [asdict(item) for item in candidates],
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(f"{cls.KIND}:{payload}".encode()).hexdigest()[:24]

    @staticmethod
    def _safe_path(root: Path, relative: str) -> Path:
        candidate = (root / relative).resolve()
        if candidate != root and root not in candidate.parents:
            raise MediaCleanupError("media cleanup path escaped its root")
        return candidate

    @staticmethod
    def _safe_error(error: Exception) -> str:
        return f"{type(error).__name__}: {error}"[:500]
