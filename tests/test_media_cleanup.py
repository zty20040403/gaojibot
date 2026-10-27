from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.bot_storage.media_cleanup import LegacyMediaCleanup, MediaCleanupError
from tests.postgres_fixture import postgres_for_test


class LegacyMediaCleanupTests(unittest.TestCase):
    def test_preview_token_and_apply_preserve_stickers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            ordinary_file = media_root / "blobs" / "ordinary.png"
            sticker_file = media_root / "blobs" / "sticker.png"
            ordinary_file.parent.mkdir(parents=True)
            ordinary_file.write_bytes(b"ordinary")
            sticker_file.write_bytes(b"sticker")
            database = postgres_for_test(self)
            connection = database.store_connection()
            connection.execute(
                "INSERT INTO media_blobs (media_id, sha256, mime_type, byte_size, storage_path, first_seen_at, last_seen_at) "
                "VALUES (1, 'a', 'image/png', 8, 'blobs/ordinary.png', 1, 1)"
            )
            connection.execute(
                "INSERT INTO media_blobs (media_id, sha256, mime_type, byte_size, storage_path, first_seen_at, last_seen_at) "
                "VALUES (2, 'b', 'image/png', 7, 'blobs/sticker.png', 1, 1)"
            )
            connection.execute(
                "INSERT INTO message_media (media_id, scope_key, native_message_id, sender_native_user_id, "
                "segment_index, media_kind, source_type, source_url, created_at, updated_at) "
                "VALUES (1, 'group:1', '1', '2', 0, 'image', 'url', '', 1, 1)"
            )
            connection.execute(
                "INSERT INTO message_media (media_id, scope_key, native_message_id, sender_native_user_id, "
                "segment_index, media_kind, source_type, source_url, created_at, updated_at) "
                "VALUES (2, 'group:1', '2', '2', 0, 'sticker', 'url', '', 1, 1)"
            )
            connection.execute(
                "INSERT INTO sticker_library (media_id, created_at, updated_at) VALUES (2, 1, 1)"
            )
            connection.execute(
                "INSERT INTO semantic_documents (scope_key, source_type, source_handle, content, content_hash, "
                "embedding_model, embedding, created_at, updated_at) "
                "VALUES ('group:1', 'media', 'media#1', 'ordinary', 'hash', 'test', ?::vector, 1, 1)",
                ("[" + ",".join(["0.1"] * 1024) + "]",),
            )
            connection.close()
            cleanup = LegacyMediaCleanup(
                database,
                media_root=media_root,
            )

            preview = cleanup.preview()
            self.assertEqual(preview["candidate_count"], 1)
            self.assertEqual(preview["candidate_bytes"], 8)
            with self.assertRaises(MediaCleanupError):
                cleanup.apply("wrong")

            report = cleanup.apply(str(preview["confirmation_token"]))

            self.assertEqual(report["status"], "completed")
            self.assertEqual(report["deleted_count"], 1)
            self.assertFalse(ordinary_file.exists())
            self.assertTrue(sticker_file.exists())
            self.assertEqual(cleanup.preview()["candidate_count"], 0)

    def test_cleanup_paths_cannot_escape_root(self) -> None:
        root = Path("/tmp/media-cleanup-root").resolve()
        with self.assertRaises(MediaCleanupError):
            LegacyMediaCleanup._safe_path(root, "../outside")


if __name__ == "__main__":
    unittest.main()
