from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import random
import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Mapping, Sequence
from urllib.parse import urljoin, urlsplit

import httpx

from src.bot_storage import DatabaseError, PostgresDatabase

from .cold_archive import ColdArchiveError, copy_verified_file
from .conversation_scope import ConversationScope
from .llm_gateway import LLMGateway
from .model_catalog import ModelCatalog, ModelProfile
from .semantic_recall import SemanticDocument, SemanticRecallService


SUPPORTED_SOURCE_SCHEMES = {"http", "https"}
QQ_IMAGE_HOST_SUFFIXES = ("qq.com", "qq.com.cn", "qpic.cn")
SUPPORTED_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/bmp",
}
VISION_SYSTEM_PROMPT = """你是 QQ 机器人后台的图片理解服务。
只输出一个 JSON 对象，不要 Markdown。必须包含：
- summary：8 到 20 个中文字的简短标签；
- description：一到两句准确的中文画面描述；
- text：图片中可辨认的关键文字，没有则为空字符串；
- subjects：主体标签字符串数组，例如猫、女孩、终端；
- actions：动作标签字符串数组，例如挥手、哭、摊手；
- emotion：中文情绪字符串数组；
- usage：适合发送这张图的聊天场景字符串数组；
- is_sticker：它是否适合当聊天表情包；
- contains_person：是否包含可识别真人；
- contains_private_info：是否含聊天记录、联系方式、身份证件、二维码、账号凭据等隐私；
- safety：safe、review、blocked 之一。
真人照片和含隐私截图不能进入自动发送表情库；不确定时 safety 返回 review。"""
GLOBAL_STICKER_SCOPE_KEY = "global:stickers"
STICKER_ALIAS_GROUPS = (
    ("猫娘", "猫耳", "猫耳少女", "猫女"),
    ("男娘", "伪娘", "女装少年"),
    ("开心", "高兴", "大笑", "欢快", "笑"),
    ("震惊", "惊讶", "吃惊", "瞪眼"),
    ("无语", "质问", "疑问", "问号", "懵"),
    ("可爱", "卖萌", "呆萌", "俏皮", "萌"),
    ("生气", "愤怒", "发火", "咆哮"),
    ("难过", "伤心", "委屈", "可怜", "哭"),
    ("饿", "挨饿", "饥饿"),
    ("狗", "小狗", "狗狗"),
    ("猫", "猫咪", "橘猫"),
    ("鲸鱼", "小鲸鱼"),
    ("吐舌", "搞怪"),
)
STICKER_QUERY_FILLERS = (
    "给我",
    "帮我",
    "来一张",
    "来一个",
    "来个",
    "发一张",
    "发一个",
    "发个",
    "发张",
    "表情包",
    "表情",
    "贴纸",
    "图片",
    "一下",
    "再换一个",
    "再换一张",
    "来个别的",
    "来张别的",
    "不一样的",
    "不要这个",
    "换一个",
    "换一张",
    "换个",
    "换张",
    "再来一个",
    "再来一张",
    "再发一个",
    "再发一张",
    "另一个",
    "另一张",
    "别的",
)
STICKER_VARIATION_MARKERS = (
    "换一个",
    "换一张",
    "换个",
    "换张",
    "再来一个",
    "再来一张",
    "再发一个",
    "再发一张",
    "另一个",
    "另一张",
    "来个别的",
    "来张别的",
    "不一样的",
    "不要这个",
)


class MediaLibraryError(RuntimeError):
    pass


@dataclass(frozen=True)
class MediaJob:
    job_id: int
    job_type: str
    message_media_id: int | None
    media_id: int | None
    attempts: int
    source_url: str = ""
    media_kind: str = "image"
    storage_path: str = ""
    mime_type: str = "application/octet-stream"
    sha256: str = ""


@dataclass(frozen=True)
class MediaRecord:
    media_id: int
    handle: str
    summary: str
    description: str
    extracted_text: str
    emotions: tuple[str, ...]
    usage: tuple[str, ...]
    is_sticker: bool
    safety: str
    storage_path: Path
    mime_type: str
    score: float = 0.0
    subjects: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    times_sent: int = 0
    last_sent_at: int | None = None


def choose_sticker_candidate(
    candidates: Sequence[MediaRecord],
    *,
    now: int | None = None,
    allow_recent_fallback: bool = True,
) -> MediaRecord | None:
    if not candidates:
        return None

    current_time = int(time.time()) if now is None else int(now)
    recent_cutoff = current_time - 300
    not_recent = [
        candidate
        for candidate in candidates
        if candidate.last_sent_at is None
        or candidate.last_sent_at < recent_cutoff
    ]
    if not_recent:
        pool = not_recent
    elif allow_recent_fallback:
        pool = list(candidates)
    else:
        return None
    minimum_sent = min(max(candidate.times_sent, 0) for candidate in pool)
    weights = [
        max(candidate.score, 0.25)
        / (1.0 + max(candidate.times_sent - minimum_sent, 0)) ** 0.5
        for candidate in pool
    ]
    return random.choices(pool, weights=weights, k=1)[0]


def requests_sticker_variation(text: str) -> bool:
    normalized = MediaLibrary._normalize_sticker_text(text)
    return any(
        MediaLibrary._normalize_sticker_text(marker) in normalized
        for marker in STICKER_VARIATION_MARKERS
    )


class MediaLibrary:
    def __init__(
        self,
        database: PostgresDatabase,
        *,
        root: Path,
        model_catalog: ModelCatalog,
        llm_gateway: LLMGateway,
        vision_profile: str,
        semantic_recall: SemanticRecallService | None = None,
        max_source_bytes: int = 100 * 1024 * 1024,
        max_vision_bytes: int = 20 * 1024 * 1024,
        prepare_threshold_bytes: int = 1024 * 1024,
        max_edge_pixels: int = 1568,
        timeout_seconds: int = 180,
        max_attempts: int = 5,
        lease_seconds: int = 240,
        batch_size: int = 4,
        worker_concurrency: int = 2,
        ffmpeg_path: str = "ffmpeg",
        archive_root: Path | None = None,
    ) -> None:
        self.database = database
        self.root = root.expanduser().resolve()
        self.model_catalog = model_catalog
        self.llm_gateway = llm_gateway
        self.vision_profile_name = vision_profile.strip()
        self.semantic_recall = semantic_recall
        self.max_source_bytes = max(int(max_source_bytes), 1024)
        self.max_vision_bytes = max(int(max_vision_bytes), 1024)
        self.prepare_threshold_bytes = max(int(prepare_threshold_bytes), 0)
        self.max_edge_pixels = max(int(max_edge_pixels), 256)
        self.timeout_seconds = max(int(timeout_seconds), 5)
        self.max_attempts = max(int(max_attempts), 1)
        # A caption job may spend one timeout preparing the image and another
        # waiting for the model. Keep its lease valid for the whole operation.
        self.lease_seconds = max(
            int(lease_seconds),
            self.timeout_seconds * 2 + 60,
            30,
        )
        self.batch_size = min(max(int(batch_size), 1), 20)
        self.worker_concurrency = min(max(int(worker_concurrency), 1), 8)
        self.ffmpeg_path = ffmpeg_path.strip() or "ffmpeg"
        self.archive_root = (
            archive_root.expanduser().resolve()
            if archive_root is not None
            else None
        )
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}"
        self._wake = asyncio.Event()
        self._closed = False

        profile = self.model_catalog.resolve(self.vision_profile_name)
        if not profile.capabilities.vision:
            raise MediaLibraryError(
                f"vision profile {profile.name!r} is not marked vision-capable"
            )
        if not profile.configured:
            raise MediaLibraryError(
                f"vision profile {profile.name!r} has no usable API key"
            )
        for directory in ("blobs", "prepared", "quarantine"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)

    def ingest_message(
        self,
        scope: ConversationScope,
        *,
        native_message_id: str | int,
        sender_native_user_id: str | int,
        segments: Sequence[Mapping[str, object]],
        canonical_message_id: int | None = None,
        occurred_at: int | None = None,
    ) -> int:
        now = int(occurred_at or time.time())
        added = 0
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            for index, segment in enumerate(segments):
                segment_type = str(segment.get("type") or "")
                if segment_type not in {"image", "mface"}:
                    continue
                raw_data = segment.get("data")
                data = raw_data if isinstance(raw_data, Mapping) else {}
                # Ordinary pictures are handled by the transient vision queue.
                # Only platform-classified stickers may enter durable storage.
                if not self._is_sticker(segment_type, data):
                    continue
                source_url = str(data.get("url") or "").strip()
                if not self._supported_source(source_url):
                    continue
                summary = str(data.get("summary") or "").strip()[:500]
                cursor.execute(
                    """
                    INSERT INTO message_media (
                        scope_key, canonical_message_id, native_message_id,
                        sender_native_user_id, segment_index, media_kind,
                        source_type, source_url, source_summary, fetch_status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    ON CONFLICT(scope_key, native_message_id, segment_index)
                    DO UPDATE SET
                        canonical_message_id = COALESCE(
                            EXCLUDED.canonical_message_id,
                            message_media.canonical_message_id
                        ),
                        source_url = EXCLUDED.source_url,
                        source_summary = EXCLUDED.source_summary,
                        updated_at = EXCLUDED.updated_at
                    RETURNING message_media_id, fetch_status
                    """,
                    (
                        scope.key,
                        canonical_message_id,
                        str(native_message_id),
                        str(sender_native_user_id),
                        index,
                        "sticker",
                        segment_type,
                        source_url,
                        summary,
                        now,
                        now,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    continue
                message_media_id = int(row["message_media_id"])
                if str(row["fetch_status"]) == "ready":
                    continue
                cursor.execute(
                    """
                    INSERT INTO media_jobs (
                        job_type, message_media_id, status, attempts,
                        next_attempt_at, created_at, updated_at
                    ) VALUES ('fetch', ?, 'pending', 0, ?, ?, ?)
                    ON CONFLICT DO NOTHING
                    """,
                    (message_media_id, now, now, now),
                )
                added += int(cursor.rowcount > 0)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        if added:
            self._wake.set()
        return added

    async def run_forever(self) -> None:
        semaphore = asyncio.Semaphore(self.worker_concurrency)
        while not self._closed:
            jobs = await asyncio.to_thread(self.claim_jobs)
            if not jobs:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=15)
                except TimeoutError:
                    pass
                continue

            async def run(job: MediaJob) -> None:
                async with semaphore:
                    try:
                        await self.process_job(job)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        await asyncio.to_thread(self.fail_job, job, exc)

            await asyncio.gather(*(run(job) for job in jobs))

    async def process_job(self, job: MediaJob) -> None:
        if job.job_type == "fetch":
            await self._process_fetch(job)
        elif job.job_type == "caption":
            await self._process_caption(job)
        elif job.job_type == "embedding":
            await self._process_embedding(job)
        else:
            raise MediaLibraryError(f"unsupported media job type: {job.job_type}")
        await asyncio.to_thread(self.complete_job, job.job_id)

    def claim_jobs(self) -> list[MediaJob]:
        now = int(time.time())
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                WITH due AS (
                    SELECT job_id
                    FROM media_jobs
                    WHERE (
                        status = 'pending' AND next_attempt_at <= ?
                    ) OR (
                        status = 'running' AND lease_until IS NOT NULL
                        AND lease_until <= ?
                    )
                    ORDER BY next_attempt_at, job_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT ?
                )
                UPDATE media_jobs AS jobs
                SET status = 'running', attempts = jobs.attempts + 1,
                    lease_until = ?, worker_id = ?, updated_at = ?
                FROM due
                WHERE jobs.job_id = due.job_id
                RETURNING jobs.job_id, jobs.job_type, jobs.message_media_id,
                          jobs.media_id, jobs.attempts
                """,
                (
                    now,
                    now,
                    self.batch_size,
                    now + self.lease_seconds,
                    self.worker_id,
                    now,
                ),
            )
            rows = cursor.fetchall()
            jobs: list[MediaJob] = []
            for row in rows:
                job = MediaJob(
                    job_id=int(row["job_id"]),
                    job_type=str(row["job_type"]),
                    message_media_id=(
                        int(row["message_media_id"])
                        if row["message_media_id"] is not None
                        else None
                    ),
                    media_id=int(row["media_id"]) if row["media_id"] is not None else None,
                    attempts=int(row["attempts"]),
                )
                jobs.append(self._hydrate_job(cursor, job))
            connection.commit()
            return jobs
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _hydrate_job(self, cursor: Any, job: MediaJob) -> MediaJob:
        if job.job_type == "fetch" and job.message_media_id is not None:
            cursor.execute(
                """
                SELECT source_url, media_kind
                FROM message_media WHERE message_media_id = ?
                """,
                (job.message_media_id,),
            )
            row = cursor.fetchone()
            if row is not None:
                return MediaJob(
                    **{**job.__dict__, "source_url": str(row["source_url"]), "media_kind": str(row["media_kind"])}
                )
        if job.media_id is not None:
            cursor.execute(
                """
                SELECT storage_path, mime_type, sha256
                FROM media_blobs WHERE media_id = ?
                """,
                (job.media_id,),
            )
            row = cursor.fetchone()
            if row is not None:
                return MediaJob(
                    **{
                        **job.__dict__,
                        "storage_path": str(row["storage_path"]),
                        "mime_type": str(row["mime_type"]),
                        "sha256": str(row["sha256"]),
                    }
                )
        return job

    async def _process_fetch(self, job: MediaJob) -> None:
        if job.message_media_id is None or not job.source_url:
            raise MediaLibraryError("fetch job has no message media source")
        content, mime_type = await self._download(job.source_url)
        sha256 = hashlib.sha256(content).hexdigest()
        suffix = self._extension(mime_type)
        relative = Path("blobs") / sha256[:2] / f"{sha256}{suffix}"
        destination = self.root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            temporary = destination.with_suffix(destination.suffix + f".{os.getpid()}.tmp")
            temporary.write_bytes(content)
            os.replace(temporary, destination)

        now = int(time.time())
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO media_blobs (
                    sha256, mime_type, byte_size, storage_path, status,
                    first_seen_at, last_seen_at, times_seen, last_accessed_at
                ) VALUES (?, ?, ?, ?, 'ready', ?, ?, 1, ?)
                ON CONFLICT(sha256) DO UPDATE SET
                    mime_type = EXCLUDED.mime_type,
                    byte_size = EXCLUDED.byte_size,
                    storage_path = EXCLUDED.storage_path,
                    status = 'ready',
                    last_seen_at = EXCLUDED.last_seen_at,
                    times_seen = media_blobs.times_seen + 1,
                    last_accessed_at = EXCLUDED.last_accessed_at,
                    local_deleted_at = NULL
                RETURNING media_id
                """,
                (sha256, mime_type, len(content), str(relative), now, now, now),
            )
            row = cursor.fetchone()
            if row is None:
                raise MediaLibraryError("could not store media blob")
            media_id = int(row["media_id"])
            cursor.execute(
                """
                UPDATE message_media
                SET media_id = ?, fetch_status = 'ready', updated_at = ?
                WHERE message_media_id = ?
                """,
                (media_id, now, job.message_media_id),
            )
            cursor.execute(
                "SELECT 1 FROM media_analysis WHERE media_id = ?",
                (media_id,),
            )
            if cursor.fetchone() is None:
                cursor.execute(
                    """
                    INSERT INTO media_jobs (
                        job_type, media_id, status, attempts,
                        next_attempt_at, created_at, updated_at
                    ) VALUES ('caption', ?, 'pending', 0, ?, ?, ?)
                    ON CONFLICT DO NOTHING
                    """,
                    (media_id, now, now, now),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def _process_caption(self, job: MediaJob) -> None:
        if job.media_id is None or not job.storage_path:
            raise MediaLibraryError("caption job has no stored media")
        source = self._resolve_storage_path(job.storage_path)
        prepared, mime_type = await asyncio.to_thread(
            self._prepare_for_vision,
            source,
            job.mime_type,
            job.sha256,
        )
        if prepared.stat().st_size > self.max_vision_bytes:
            raise MediaLibraryError("prepared image exceeds the vision size limit")
        payload = base64.b64encode(prepared.read_bytes()).decode("ascii")
        profile = self._vision_profile()
        response = await self.llm_gateway.create_completion(
            profile,
            model=profile.model,
            messages=[
                {"role": "system", "content": VISION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "分析这张图片并返回 JSON。"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{payload}",
                            },
                        },
                    ],
                },
            ],
            response_format=(
                {"type": "json_object"}
                if profile.capabilities.json_mode
                else None
            ),
        )
        raw_content = str(response.choices[0].message.content or "").strip()
        analysis = self._parse_analysis(raw_content)
        now = int(time.time())
        content_hash = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
        safe_for_stickers = (
            analysis["is_sticker"]
            and not analysis["contains_person"]
            and not analysis["contains_private_info"]
            and analysis["safety"] == "safe"
        )
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO media_analysis (
                    media_id, vision_profile, vision_model, summary,
                    description, extracted_text, subjects_json, actions_json,
                    emotions_json, usage_json,
                    is_sticker, contains_person, contains_private_info,
                    safety, raw_response_json, content_hash, analyzed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(media_id) DO UPDATE SET
                    vision_profile = EXCLUDED.vision_profile,
                    vision_model = EXCLUDED.vision_model,
                    summary = EXCLUDED.summary,
                    description = EXCLUDED.description,
                    extracted_text = EXCLUDED.extracted_text,
                    subjects_json = EXCLUDED.subjects_json,
                    actions_json = EXCLUDED.actions_json,
                    emotions_json = EXCLUDED.emotions_json,
                    usage_json = EXCLUDED.usage_json,
                    is_sticker = EXCLUDED.is_sticker,
                    contains_person = EXCLUDED.contains_person,
                    contains_private_info = EXCLUDED.contains_private_info,
                    safety = EXCLUDED.safety,
                    raw_response_json = EXCLUDED.raw_response_json,
                    content_hash = EXCLUDED.content_hash,
                    analyzed_at = EXCLUDED.analyzed_at
                """,
                (
                    job.media_id,
                    profile.name,
                    profile.model,
                    analysis["summary"],
                    analysis["description"],
                    analysis["text"],
                    json.dumps(analysis["subjects"], ensure_ascii=False),
                    json.dumps(analysis["actions"], ensure_ascii=False),
                    json.dumps(analysis["emotion"], ensure_ascii=False),
                    json.dumps(analysis["usage"], ensure_ascii=False),
                    int(bool(analysis["is_sticker"])),
                    int(bool(analysis["contains_person"])),
                    int(bool(analysis["contains_private_info"])),
                    analysis["safety"],
                    json.dumps(analysis, ensure_ascii=False),
                    content_hash,
                    now,
                ),
            )
            if safe_for_stickers:
                cursor.execute(
                    """
                    INSERT INTO sticker_library (
                        media_id, enabled, banned, times_sent,
                        created_at, updated_at
                    ) VALUES (?, 1, 0, 0, ?, ?)
                    ON CONFLICT(media_id) DO UPDATE SET updated_at = EXCLUDED.updated_at
                    """,
                    (job.media_id, now, now),
                )
            else:
                cursor.execute(
                    "UPDATE sticker_library SET enabled = 0, updated_at = ? WHERE media_id = ?",
                    (now, job.media_id),
                )
            if self.semantic_recall is not None:
                cursor.execute(
                    """
                    INSERT INTO media_jobs (
                        job_type, media_id, status, attempts,
                        next_attempt_at, created_at, updated_at
                    ) VALUES ('embedding', ?, 'pending', 0, ?, ?, ?)
                    ON CONFLICT DO NOTHING
                    """,
                    (job.media_id, now, now, now),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def _process_embedding(self, job: MediaJob) -> None:
        if job.media_id is None or self.semantic_recall is None:
            return
        sticker = self.get_sticker(job.media_id)
        if sticker is None:
            return
        await self.semantic_recall.index(
            [
                SemanticDocument(
                    scope_key=GLOBAL_STICKER_SCOPE_KEY,
                    source_type="media",
                    source_handle=sticker.handle,
                    content=self._record_search_content(sticker),
                    metadata={"media_id": sticker.media_id, "is_sticker": True},
                )
            ]
        )

    def enqueue_sticker_embeddings(self) -> int:
        if self.semantic_recall is None:
            return 0
        now = int(time.time())
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO media_jobs (
                    job_type, media_id, status, attempts,
                    next_attempt_at, created_at, updated_at
                )
                SELECT 'embedding', stickers.media_id, 'pending', 0, ?, ?, ?
                FROM sticker_library AS stickers
                WHERE stickers.enabled = 1 AND stickers.banned = 0
                  AND NOT EXISTS (
                      SELECT 1 FROM semantic_documents AS document
                      WHERE document.scope_key = ?
                        AND document.source_type = 'media'
                        AND document.source_handle = 'media#' || stickers.media_id::text
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM media_jobs AS job
                      WHERE job.media_id = stickers.media_id
                        AND job.job_type = 'embedding'
                        AND job.status IN ('pending', 'running')
                  )
                """,
                (now, now, now, GLOBAL_STICKER_SCOPE_KEY),
            )
            added = max(int(cursor.rowcount), 0)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        if added:
            self._wake.set()
        return added

    def find_media(
        self,
        scope: ConversationScope,
        query: str,
        *,
        stickers_only: bool = False,
        limit: int = 5,
    ) -> list[MediaRecord]:
        cleaned = " ".join(str(query).split()).strip()
        bounded = min(max(int(limit), 1), 20)
        wildcard = f"%{cleaned}%"
        connection = self.database.store_connection()
        try:
            rows = connection.execute(
                """
                SELECT DISTINCT ON (blob.media_id)
                    blob.media_id, blob.storage_path, blob.mime_type,
                    blob.sha256, blob.byte_size, blob.archive_path,
                    analysis.summary, analysis.description,
                    analysis.extracted_text, analysis.subjects_json,
                    analysis.actions_json, analysis.emotions_json,
                    analysis.usage_json, analysis.is_sticker, analysis.safety
                FROM message_media AS link
                JOIN media_blobs AS blob ON blob.media_id = link.media_id
                JOIN media_analysis AS analysis ON analysis.media_id = blob.media_id
                LEFT JOIN sticker_library AS stickers ON stickers.media_id = blob.media_id
                WHERE link.scope_key = ?
                  AND analysis.safety = 'safe'
                  AND (? = 0 OR (
                      stickers.enabled = 1 AND stickers.banned = 0
                      AND analysis.is_sticker = 1
                  ))
                  AND (
                      ? = '' OR analysis.summary ILIKE ?
                      OR analysis.description ILIKE ?
                      OR analysis.extracted_text ILIKE ?
                      OR analysis.subjects_json ILIKE ?
                      OR analysis.actions_json ILIKE ?
                      OR analysis.emotions_json ILIKE ?
                      OR analysis.usage_json ILIKE ?
                  )
                ORDER BY blob.media_id, link.created_at DESC
                LIMIT ?
                """,
                (
                    scope.key,
                    int(stickers_only),
                    cleaned,
                    wildcard,
                    wildcard,
                    wildcard,
                    wildcard,
                    wildcard,
                    wildcard,
                    wildcard,
                    bounded,
                ),
            ).fetchall()
        finally:
            connection.close()
        return [self._record_from_row(row) for row in rows]

    async def search_media(
        self,
        scope: ConversationScope,
        query: str,
        *,
        stickers_only: bool = False,
        limit: int = 5,
    ) -> list[MediaRecord]:
        lexical = self.find_media(
            scope,
            query,
            stickers_only=stickers_only,
            limit=limit,
        )
        if self.semantic_recall is None or not query.strip():
            return lexical
        try:
            hits = await self.semantic_recall.search([scope.key], query, limit=limit * 3)
        except (OSError, RuntimeError, ValueError, httpx.HTTPError):
            return lexical
        by_id = {item.media_id: item for item in lexical}
        for hit in hits:
            if hit.source_type != "media":
                continue
            try:
                media_id = int(hit.source_handle.removeprefix("media#"))
            except ValueError:
                continue
            record = self.get_media(
                scope,
                media_id,
                sendable_sticker_only=stickers_only,
            )
            if record is None:
                continue
            by_id[media_id] = MediaRecord(**{**record.__dict__, "score": hit.score})
        return sorted(
            by_id.values(),
            key=lambda item: (item.score, item.media_id),
            reverse=True,
        )[: min(max(int(limit), 1), 20)]

    def find_stickers(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[MediaRecord]:
        cleaned = " ".join(str(query).split()).strip()
        bounded = min(max(int(limit), 1), 20)
        terms = self._sticker_query_terms(cleaned)
        required_groups = self._sticker_required_alias_groups(cleaned)
        term_groups: list[str] = []
        term_parameters: list[object] = []
        for term in terms:
            term_groups.append(
                "(analysis.summary ILIKE ? OR analysis.description ILIKE ? "
                "OR analysis.extracted_text ILIKE ? "
                "OR analysis.subjects_json ILIKE ? OR analysis.actions_json ILIKE ? "
                "OR analysis.emotions_json ILIKE ? OR analysis.usage_json ILIKE ?)"
            )
            wildcard = f"%{term}%"
            term_parameters.extend([wildcard] * 7)
        term_filter = (
            "AND (" + " OR ".join(term_groups) + ")"
            if term_groups
            else ""
        )
        candidate_limit = min(max(bounded * 10, 50), 500)
        connection = self.database.store_connection()
        try:
            rows = connection.execute(
                f"""
                SELECT blob.media_id, blob.storage_path, blob.mime_type,
                       blob.sha256, blob.byte_size, blob.archive_path,
                       analysis.summary, analysis.description,
                       analysis.extracted_text, analysis.subjects_json,
                       analysis.actions_json, analysis.emotions_json,
                       analysis.usage_json, analysis.is_sticker, analysis.safety,
                       stickers.times_sent, stickers.last_sent_at
                FROM media_blobs AS blob
                JOIN media_analysis AS analysis ON analysis.media_id = blob.media_id
                JOIN sticker_library AS stickers ON stickers.media_id = blob.media_id
                WHERE analysis.safety = 'safe'
                  AND analysis.is_sticker = 1
                  AND stickers.enabled = 1 AND stickers.banned = 0
                  {term_filter}
                ORDER BY stickers.times_sent ASC,
                         stickers.last_sent_at ASC NULLS FIRST,
                         blob.last_seen_at DESC
                LIMIT ?
                """,
                (*term_parameters, candidate_limit),
            ).fetchall()
        finally:
            connection.close()
        records = [self._record_from_row(row, touch=False) for row in rows]
        if not terms:
            return records[:bounded]
        ranked = [
            MediaRecord(
                **{
                    **record.__dict__,
                    "score": self._sticker_relevance(record, terms),
                }
            )
            for record in records
            if self._record_matches_sticker_groups(record, required_groups)
        ]
        return sorted(
            (record for record in ranked if record.score > 0),
            key=lambda record: (record.score, record.media_id),
            reverse=True,
        )[:bounded]

    async def search_stickers(
        self,
        query: str,
        *,
        limit: int = 5,
        minimum_score: float = 0.68,
    ) -> list[MediaRecord]:
        cleaned = " ".join(str(query).split()).strip()
        bounded = min(max(int(limit), 1), 20)
        lexical = self.find_stickers(cleaned, limit=bounded)
        required_groups = self._sticker_required_alias_groups(cleaned)
        if (
            self.semantic_recall is None
            or not self._sticker_query_terms(cleaned)
        ):
            return lexical
        try:
            hits = await self.semantic_recall.search(
                [GLOBAL_STICKER_SCOPE_KEY],
                cleaned,
                limit=bounded * 3,
            )
        except (OSError, RuntimeError, ValueError, httpx.HTTPError):
            return lexical
        by_id = {item.media_id: item for item in lexical}
        for hit in hits:
            if hit.source_type != "media" or hit.score < minimum_score:
                continue
            try:
                media_id = int(hit.source_handle.removeprefix("media#"))
            except ValueError:
                continue
            record = self.get_sticker(media_id)
            if record is None:
                continue
            if not self._record_matches_sticker_groups(record, required_groups):
                continue
            lexical_score = self._sticker_relevance(
                record,
                self._sticker_query_terms(cleaned),
            )
            combined_score = (
                0.7 * lexical_score + 0.3 * hit.score
                if lexical_score > 0
                else 0.72 * hit.score
            )
            existing = by_id.get(media_id)
            if existing is None or combined_score > existing.score:
                by_id[media_id] = MediaRecord(
                    **{**record.__dict__, "score": min(combined_score, 1.0)}
                )
        return sorted(
            by_id.values(),
            key=lambda item: (item.score, item.media_id),
            reverse=True,
        )[:bounded]

    def get_sticker(self, media_id: int) -> MediaRecord | None:
        connection = self.database.store_connection()
        try:
            row = connection.execute(
                """
                SELECT blob.media_id, blob.storage_path, blob.mime_type,
                       blob.sha256, blob.byte_size, blob.archive_path,
                       analysis.summary, analysis.description,
                       analysis.extracted_text, analysis.subjects_json,
                       analysis.actions_json, analysis.emotions_json,
                       analysis.usage_json, analysis.is_sticker, analysis.safety,
                       stickers.times_sent, stickers.last_sent_at
                FROM media_blobs AS blob
                JOIN media_analysis AS analysis ON analysis.media_id = blob.media_id
                JOIN sticker_library AS stickers ON stickers.media_id = blob.media_id
                WHERE blob.media_id = ?
                  AND analysis.safety = 'safe'
                  AND analysis.is_sticker = 1
                  AND stickers.enabled = 1 AND stickers.banned = 0
                """,
                (int(media_id),),
            ).fetchone()
        finally:
            connection.close()
        return self._record_from_row(row) if row is not None else None

    def get_media(
        self,
        scope: ConversationScope,
        media_id: int,
        *,
        sendable_sticker_only: bool = False,
    ) -> MediaRecord | None:
        sticker_filter = ""
        if sendable_sticker_only:
            sticker_filter = """
                AND analysis.safety = 'safe'
                AND analysis.is_sticker = 1
                AND EXISTS (
                    SELECT 1 FROM sticker_library AS sticker
                    WHERE sticker.media_id = blob.media_id
                      AND sticker.enabled = 1 AND sticker.banned = 0
                )
            """
        connection = self.database.store_connection()
        try:
            row = connection.execute(
                f"""
                SELECT blob.media_id, blob.storage_path, blob.mime_type,
                       blob.sha256, blob.byte_size, blob.archive_path,
                       analysis.summary, analysis.description,
                       analysis.extracted_text, analysis.subjects_json,
                       analysis.actions_json, analysis.emotions_json,
                       analysis.usage_json, analysis.is_sticker, analysis.safety
                FROM media_blobs AS blob
                JOIN media_analysis AS analysis ON analysis.media_id = blob.media_id
                WHERE blob.media_id = ? AND EXISTS (
                    SELECT 1 FROM message_media AS link
                    WHERE link.media_id = blob.media_id AND link.scope_key = ?
                )
                {sticker_filter}
                """,
                (int(media_id), scope.key),
            ).fetchone()
        finally:
            connection.close()
        return self._record_from_row(row) if row is not None else None

    def latest_for_message(
        self,
        scope: ConversationScope,
        native_message_id: str | int,
        *,
        segment_index: int | None = None,
    ) -> MediaRecord | None:
        parameters: list[object] = [scope.key, str(native_message_id)]
        segment_sql = ""
        if segment_index is not None:
            segment_sql = " AND link.segment_index = ?"
            parameters.append(int(segment_index))
        connection = self.database.store_connection()
        try:
            row = connection.execute(
                f"""
                SELECT blob.media_id, blob.storage_path, blob.mime_type,
                       blob.sha256, blob.byte_size, blob.archive_path,
                       analysis.summary, analysis.description,
                       analysis.extracted_text, analysis.subjects_json,
                       analysis.actions_json, analysis.emotions_json,
                       analysis.usage_json, analysis.is_sticker, analysis.safety
                FROM message_media AS link
                JOIN media_blobs AS blob ON blob.media_id = link.media_id
                JOIN media_analysis AS analysis ON analysis.media_id = blob.media_id
                WHERE link.scope_key = ? AND link.native_message_id = ?
                {segment_sql}
                ORDER BY link.segment_index
                LIMIT 1
                """,
                parameters,
            ).fetchone()
        finally:
            connection.close()
        return self._record_from_row(row) if row is not None else None

    def has_message_media(
        self,
        scope: ConversationScope,
        native_message_id: str | int,
        *,
        segment_index: int | None = None,
    ) -> bool:
        parameters: list[object] = [scope.key, str(native_message_id)]
        segment_sql = ""
        if segment_index is not None:
            segment_sql = " AND segment_index = ?"
            parameters.append(int(segment_index))
        connection = self.database.store_connection()
        try:
            row = connection.execute(
                f"""
                SELECT 1 FROM message_media
                WHERE scope_key = ? AND native_message_id = ?
                {segment_sql}
                LIMIT 1
                """,
                parameters,
            ).fetchone()
        finally:
            connection.close()
        return row is not None

    def latest_message_for_sender(
        self,
        scope: ConversationScope,
        sender_native_user_id: str | int,
        *,
        max_age_seconds: int = 300,
    ) -> tuple[str, int] | None:
        cutoff = int(time.time()) - max(int(max_age_seconds), 1)
        connection = self.database.store_connection()
        try:
            row = connection.execute(
                """
                SELECT native_message_id, segment_index
                FROM message_media
                WHERE scope_key = ? AND sender_native_user_id = ?
                  AND created_at >= ?
                ORDER BY created_at DESC, message_media_id DESC
                LIMIT 1
                """,
                (scope.key, str(sender_native_user_id), cutoff),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return str(row["native_message_id"]), int(row["segment_index"])

    def mark_sent(self, media_id: int) -> None:
        now = int(time.time())
        connection = self.database.store_connection()
        try:
            connection.execute(
                """
                UPDATE sticker_library
                SET times_sent = times_sent + 1, last_sent_at = ?, updated_at = ?
                WHERE media_id = ? AND enabled = 1 AND banned = 0
                """,
                (now, now, int(media_id)),
            )
        finally:
            connection.close()

    def set_review_state(self, media_id: int, state: str) -> dict[str, object]:
        normalized = str(state).strip().lower()
        safety_by_state = {
            "approved": "safe",
            "pending": "review",
            "rejected": "blocked",
        }
        if normalized not in safety_by_state:
            raise MediaLibraryError("media review state is invalid")
        now = int(time.time())
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            row = cursor.execute(
                """
                SELECT media_id, is_sticker, contains_person,
                       contains_private_info, safety
                FROM media_analysis WHERE media_id = ?
                """,
                (int(media_id),),
            ).fetchone()
            if row is None:
                raise MediaLibraryError("media item was not found")
            safety = safety_by_state[normalized]
            cursor.execute(
                "UPDATE media_analysis SET safety = ?, analyzed_at = ? WHERE media_id = ?",
                (safety, now, int(media_id)),
            )
            sendable = bool(row["is_sticker"]) and not bool(
                row["contains_person"]
            ) and not bool(row["contains_private_info"])
            enabled = normalized == "approved" and sendable
            banned = normalized == "rejected"
            cursor.execute(
                """
                INSERT INTO sticker_library (
                    media_id, enabled, banned, times_sent, created_at, updated_at
                ) VALUES (?, ?, ?, 0, ?, ?)
                ON CONFLICT(media_id) DO UPDATE SET
                    enabled = EXCLUDED.enabled,
                    banned = EXCLUDED.banned,
                    updated_at = EXCLUDED.updated_at
                """,
                (int(media_id), int(enabled), int(banned), now, now),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return {
            "media_id": int(media_id),
            "review_state": normalized,
            "safety": safety_by_state[normalized],
            "sticker_enabled": enabled,
            "sticker_banned": banned,
            "sendable": sendable,
        }

    def admin_snapshot(self, *, limit: int = 100) -> dict[str, object]:
        connection = self.database.store_connection()
        try:
            counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM sticker_library
                     WHERE enabled = 1 AND banned = 0) AS total,
                    (SELECT COALESCE(SUM(blob.byte_size), 0)
                     FROM media_blobs AS blob
                     JOIN sticker_library AS sticker ON sticker.media_id = blob.media_id
                     WHERE sticker.enabled = 1 AND sticker.banned = 0) AS bytes,
                    (SELECT COUNT(*) FROM media_analysis AS analysis
                     JOIN sticker_library AS sticker ON sticker.media_id = analysis.media_id) AS analyzed,
                    (SELECT COUNT(*) FROM sticker_library WHERE enabled = 1 AND banned = 0) AS stickers,
                    (SELECT COUNT(*) FROM media_jobs WHERE status IN ('pending', 'running')) AS queued,
                    (SELECT COUNT(*) FROM media_jobs WHERE status = 'failed') AS failed
                """
            ).fetchone()
            rows = connection.execute(
                """
                SELECT blob.media_id, blob.sha256, blob.mime_type, blob.byte_size,
                       blob.last_seen_at, analysis.summary, analysis.safety,
                       analysis.subjects_json, analysis.actions_json,
                       analysis.emotions_json, analysis.usage_json,
                       analysis.is_sticker, analysis.vision_model,
                       COALESCE(stickers.enabled, 0) AS enabled,
                       COALESCE(stickers.banned, 0) AS banned,
                       COALESCE(stickers.times_sent, 0) AS times_sent
                FROM media_blobs AS blob
                JOIN media_analysis AS analysis ON analysis.media_id = blob.media_id
                JOIN sticker_library AS stickers ON stickers.media_id = blob.media_id
                ORDER BY blob.last_seen_at DESC, blob.media_id DESC
                LIMIT ?
                """,
                (min(max(int(limit), 1), 500),),
            ).fetchall()
            jobs = connection.execute(
                """
                SELECT job_id, job_type, status, attempts, last_error, updated_at
                FROM media_jobs
                WHERE status IN ('pending', 'running', 'failed')
                ORDER BY updated_at DESC, job_id DESC
                LIMIT 100
                """
            ).fetchall()
        finally:
            connection.close()
        return {
            "counts": dict(counts) if counts is not None else {},
            "root": str(self.root),
            "vision_profile": self.vision_profile_name,
            "items": [dict(row) for row in rows],
            "jobs": [dict(row) for row in jobs],
        }

    def complete_job(self, job_id: int) -> None:
        now = int(time.time())
        connection = self.database.store_connection()
        try:
            connection.execute(
                """
                UPDATE media_jobs
                SET status = 'succeeded', lease_until = NULL, worker_id = '',
                    last_error = '', updated_at = ?, finished_at = ?
                WHERE job_id = ?
                """,
                (now, now, int(job_id)),
            )
        finally:
            connection.close()

    def fail_job(self, job: MediaJob, error: Exception) -> None:
        now = int(time.time())
        final = job.attempts >= self.max_attempts
        delay = min(30 * (2 ** max(job.attempts - 1, 0)), 3600)
        connection = self.database.store_connection()
        try:
            connection.execute(
                """
                UPDATE media_jobs
                SET status = ?, next_attempt_at = ?, lease_until = NULL,
                    worker_id = '', last_error = ?, updated_at = ?,
                    finished_at = ?
                WHERE job_id = ?
                """,
                (
                    "failed" if final else "pending",
                    now + delay,
                    self._safe_error(error),
                    now,
                    now if final else None,
                    job.job_id,
                ),
            )
            if final and job.message_media_id is not None:
                connection.execute(
                    """
                    UPDATE message_media
                    SET fetch_status = 'failed', updated_at = ?
                    WHERE message_media_id = ?
                    """,
                    (now, job.message_media_id),
                )
        finally:
            connection.close()

    async def close(self) -> None:
        self._closed = True
        self._wake.set()

    async def wait_for_message(
        self,
        scope: ConversationScope,
        native_message_id: str | int,
        *,
        segment_index: int | None = None,
        timeout_seconds: int | None = None,
    ) -> MediaRecord | None:
        deadline = time.monotonic() + max(
            int(timeout_seconds or self.timeout_seconds),
            1,
        )
        while True:
            try:
                record = await asyncio.to_thread(
                    self.latest_for_message,
                    scope,
                    native_message_id,
                    segment_index=segment_index,
                )
            except MediaLibraryError:
                record = None
            if record is not None:
                return record
            if time.monotonic() >= deadline or self._message_fetch_failed(
                scope,
                native_message_id,
                segment_index,
            ):
                return None
            self._wake.set()
            await asyncio.sleep(0.5)

    async def _download(self, source_url: str) -> tuple[bytes, str]:
        from src.storage_admission import CapacityError, require_capacity
        try:
            require_capacity(self.max_source_bytes)
        except CapacityError as error:
            raise MediaLibraryError(str(error)) from None
        if not self._supported_source(source_url):
            raise MediaLibraryError("unsupported media source URL")
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=False,
            ) as client:
                current_url = source_url
                for redirect_count in range(6):
                    if not self._supported_source(current_url):
                        raise MediaLibraryError(
                            "QQ image redirected to an unsupported host"
                        )
                    async with client.stream("GET", current_url) as response:
                        if response.is_redirect:
                            location = response.headers.get("location", "").strip()
                            if not location or redirect_count >= 5:
                                raise MediaLibraryError(
                                    "QQ image has an invalid redirect chain"
                                )
                            current_url = urljoin(str(response.url), location)
                            continue
                        response.raise_for_status()
                        content_type = (
                            response.headers.get("content-type", "")
                            .split(";", 1)[0]
                            .lower()
                        )
                        chunks: list[bytes] = []
                        size = 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > self.max_source_bytes:
                                raise MediaLibraryError(
                                    "image exceeds the source size limit"
                                )
                            chunks.append(chunk)
                        break
                else:
                    raise MediaLibraryError("QQ image redirect limit exceeded")
        except httpx.HTTPError as exc:
            raise MediaLibraryError("could not download QQ image") from exc
        content = b"".join(chunks)
        if not content:
            raise MediaLibraryError("downloaded image is empty")
        mime_type = content_type if content_type in SUPPORTED_MIME_TYPES else self._sniff_mime(content)
        if mime_type not in SUPPORTED_MIME_TYPES:
            raise MediaLibraryError("downloaded file is not a supported image")
        return content, mime_type

    def _message_fetch_failed(
        self,
        scope: ConversationScope,
        native_message_id: str | int,
        segment_index: int | None,
    ) -> bool:
        parameters: list[object] = [scope.key, str(native_message_id)]
        extra = ""
        if segment_index is not None:
            extra = " AND segment_index = ?"
            parameters.append(int(segment_index))
        connection = self.database.store_connection()
        try:
            row = connection.execute(
                f"""
                SELECT fetch_status FROM message_media
                WHERE scope_key = ? AND native_message_id = ? {extra}
                ORDER BY segment_index LIMIT 1
                """,
                parameters,
            ).fetchone()
        finally:
            connection.close()
        return row is not None and str(row["fetch_status"]) == "failed"

    def _prepare_for_vision(
        self,
        source: Path,
        mime_type: str,
        sha256: str,
    ) -> tuple[Path, str]:
        if (
            mime_type not in {"image/gif", "image/webp"}
            and source.stat().st_size <= self.prepare_threshold_bytes
            and source.stat().st_size <= self.max_vision_bytes
        ):
            return source, mime_type
        prepared = self.root / "prepared" / sha256[:2] / f"{sha256}.jpg"
        prepared.parent.mkdir(parents=True, exist_ok=True)
        if prepared.exists() and prepared.stat().st_size <= self.max_vision_bytes:
            return prepared, "image/jpeg"
        if not shutil.which(self.ffmpeg_path):
            if source.stat().st_size <= self.max_vision_bytes:
                return source, mime_type
            raise MediaLibraryError("ffmpeg is required to prepare this image")
        with TemporaryDirectory(prefix="qq-bot-media-") as directory:
            temporary = Path(directory) / "prepared.jpg"
            command = [
                self.ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                (
                    f"scale='min({self.max_edge_pixels},iw)':"
                    f"'min({self.max_edge_pixels},ih)':force_original_aspect_ratio=decrease"
                ),
                "-q:v",
                "3",
                str(temporary),
            ]
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
            if completed.returncode != 0 or not temporary.exists():
                if source.stat().st_size <= self.max_vision_bytes:
                    return source, mime_type
                raise MediaLibraryError("ffmpeg could not prepare image")
            shutil.copyfile(temporary, prepared)
        if prepared.stat().st_size >= source.stat().st_size and source.stat().st_size <= self.max_vision_bytes:
            prepared.unlink(missing_ok=True)
            return source, mime_type
        return prepared, "image/jpeg"

    def _analysis_scopes(self, media_id: int) -> list[tuple[str, str, bool]]:
        connection = self.database.store_connection()
        try:
            rows = connection.execute(
                """
                SELECT DISTINCT link.scope_key, analysis.summary,
                       analysis.description, analysis.extracted_text,
                       analysis.subjects_json, analysis.actions_json,
                       analysis.emotions_json, analysis.usage_json,
                       analysis.is_sticker
                FROM message_media AS link
                JOIN media_analysis AS analysis ON analysis.media_id = link.media_id
                WHERE link.media_id = ?
                """,
                (media_id,),
            ).fetchall()
        finally:
            connection.close()
        result: list[tuple[str, str, bool]] = []
        for row in rows:
            content = "\n".join(
                part
                for part in (
                    str(row["summary"]),
                    str(row["description"]),
                    str(row["extracted_text"]),
                    str(row["subjects_json"]),
                    str(row["actions_json"]),
                    str(row["emotions_json"]),
                    str(row["usage_json"]),
                )
                if part and part not in {"[]", ""}
            )
            result.append((str(row["scope_key"]), content, bool(row["is_sticker"])))
        return result

    def _record_from_row(
        self,
        row: Mapping[str, object],
        *,
        touch: bool = True,
    ) -> MediaRecord:
        media_id = int(row["media_id"])
        storage_path = self._resolve_storage_path(
            str(row["storage_path"]),
            archive_relative=str(row.get("archive_path") or ""),
            expected_sha256=str(row.get("sha256") or ""),
            expected_size=int(row.get("byte_size") or 0),
        )
        if touch:
            self._touch_media(media_id)
        return MediaRecord(
            media_id=media_id,
            handle=f"media#{media_id}",
            summary=str(row["summary"] or ""),
            description=str(row["description"] or ""),
            extracted_text=str(row["extracted_text"] or ""),
            emotions=tuple(self._string_list(row["emotions_json"])),
            usage=tuple(self._string_list(row["usage_json"])),
            is_sticker=bool(row["is_sticker"]),
            safety=str(row["safety"] or "review"),
            storage_path=storage_path,
            mime_type=str(row["mime_type"] or "application/octet-stream"),
            subjects=tuple(self._string_list(row.get("subjects_json"))),
            actions=tuple(self._string_list(row.get("actions_json"))),
            times_sent=int(row.get("times_sent") or 0),
            last_sent_at=(
                int(row["last_sent_at"])
                if row.get("last_sent_at") is not None
                else None
            ),
        )

    @staticmethod
    def _record_search_content(record: MediaRecord) -> str:
        return "\n".join(
            part
            for part in (
                record.summary,
                record.description,
                record.extracted_text,
                json.dumps(record.subjects, ensure_ascii=False),
                json.dumps(record.actions, ensure_ascii=False),
                json.dumps(record.emotions, ensure_ascii=False),
                json.dumps(record.usage, ensure_ascii=False),
            )
            if part and part not in {"[]", ""}
        )

    @classmethod
    def _sticker_query_terms(cls, query: str) -> tuple[str, ...]:
        normalized = cls._normalize_sticker_text(query)
        content = normalized
        for filler in STICKER_QUERY_FILLERS:
            content = content.replace(cls._normalize_sticker_text(filler), "")
        if content.startswith("更") and len(content) > 1:
            content = content[1:]
        if content.endswith("的") and len(content) > 1:
            content = content[:-1]
        terms: list[str] = [content] if content else []
        for group in STICKER_ALIAS_GROUPS:
            if any(cls._normalize_sticker_text(alias) in content for alias in group):
                terms.extend(cls._normalize_sticker_text(alias) for alias in group)
        return tuple(dict.fromkeys(term for term in terms if term))

    @classmethod
    def _sticker_required_alias_groups(
        cls,
        query: str,
    ) -> tuple[tuple[str, ...], ...]:
        normalized = cls._normalize_sticker_text(query)
        return tuple(
            tuple(cls._normalize_sticker_text(alias) for alias in group)
            for group in STICKER_ALIAS_GROUPS
            if any(cls._normalize_sticker_text(alias) in normalized for alias in group)
        )

    @classmethod
    def _record_matches_sticker_groups(
        cls,
        record: MediaRecord,
        groups: Sequence[Sequence[str]],
    ) -> bool:
        if not groups:
            return True
        content = cls._normalize_sticker_text(cls._record_search_content(record))
        return all(any(alias in content for alias in group) for group in groups)

    @classmethod
    def _sticker_relevance(
        cls,
        record: MediaRecord,
        terms: Sequence[str],
    ) -> float:
        fields = (
            (" ".join(record.subjects), 5.0),
            (" ".join(record.actions), 4.5),
            (record.summary, 4.0),
            (" ".join(record.emotions), 4.0),
            (" ".join(record.usage), 3.5),
            (record.description, 2.5),
            (record.extracted_text, 1.5),
        )
        if not terms:
            return 0.0
        normalized_fields = [
            (cls._normalize_sticker_text(value), weight)
            for value, weight in fields
        ]
        term_scores = [
            max(
                (
                    weight / 5.0
                    for value, weight in normalized_fields
                    if term in value
                ),
                default=0.0,
            )
            for term in terms
        ]
        primary_score = term_scores[0]
        supporting_score = sum(sorted(term_scores[1:], reverse=True)[:3]) / 3.0
        return min(primary_score * 0.7 + supporting_score * 0.3, 1.0)

    @staticmethod
    def _normalize_sticker_text(value: str) -> str:
        return re.sub(r"[\W_]+", "", str(value).casefold())

    def _vision_profile(self) -> ModelProfile:
        return self.model_catalog.resolve(self.vision_profile_name)

    def _resolve_storage_path(
        self,
        relative: str,
        *,
        archive_relative: str = "",
        expected_sha256: str = "",
        expected_size: int = 0,
    ) -> Path:
        candidate = (self.root / relative).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise MediaLibraryError("media storage path escaped its root")
        if candidate.is_file():
            return candidate
        if (
            self.archive_root is None
            or not archive_relative
            or not expected_sha256
            or expected_size <= 0
        ):
            raise MediaLibraryError("stored media file is missing")

        archive = (self.archive_root / archive_relative).resolve()
        if archive != self.archive_root and self.archive_root not in archive.parents:
            raise MediaLibraryError("media archive path escaped its root")
        try:
            copy_verified_file(
                archive,
                candidate,
                expected_sha256=expected_sha256,
                expected_size=expected_size,
            )
        except (ColdArchiveError, OSError) as exc:
            raise MediaLibraryError(
                "stored media is unavailable in the hot cache and archive"
            ) from exc
        return candidate

    def _touch_media(self, media_id: int) -> None:
        connection = self.database.store_connection()
        try:
            connection.execute(
                """
                UPDATE media_blobs
                SET last_accessed_at = ?, local_deleted_at = NULL
                WHERE media_id = ?
                """,
                (int(time.time()), int(media_id)),
            )
        finally:
            connection.close()

    @staticmethod
    def _supported_source(source_url: str) -> bool:
        parsed = urlsplit(source_url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        return (
            parsed.scheme.lower() in SUPPORTED_SOURCE_SCHEMES
            and any(
                hostname == suffix or hostname.endswith(f".{suffix}")
                for suffix in QQ_IMAGE_HOST_SUFFIXES
            )
        )

    @staticmethod
    def _is_sticker(segment_type: str, data: Mapping[str, object]) -> bool:
        return segment_type == "mface" or str(data.get("subType") or data.get("sub_type") or "") == "1" or "表情" in str(data.get("summary") or "")

    @staticmethod
    def _extension(mime_type: str) -> str:
        return {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/bmp": ".bmp",
        }.get(mime_type, mimetypes.guess_extension(mime_type) or ".bin")

    @staticmethod
    def _sniff_mime(content: bytes) -> str:
        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if content.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
            return "image/webp"
        if content.startswith(b"BM"):
            return "image/bmp"
        return "application/octet-stream"

    @classmethod
    def _parse_analysis(cls, content: str) -> dict[str, object]:
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
            if cleaned.rstrip().endswith("```"):
                cleaned = cleaned.rstrip()[:-3].rstrip()
        try:
            raw = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise MediaLibraryError("vision model returned invalid JSON") from exc
        if not isinstance(raw, Mapping):
            raise MediaLibraryError("vision model did not return an object")
        summary = " ".join(str(raw.get("summary") or "").split())[:80]
        description = " ".join(str(raw.get("description") or "").split())[:1000]
        if not summary or not description:
            raise MediaLibraryError("vision model omitted summary or description")
        safety = str(raw.get("safety") or "review").strip().lower()
        if safety not in {"safe", "review", "blocked"}:
            safety = "review"
        return {
            "summary": summary,
            "description": description,
            "text": str(raw.get("text") or "").strip()[:4000],
            "subjects": cls._string_list(raw.get("subjects"))[:12],
            "actions": cls._string_list(raw.get("actions"))[:12],
            "emotion": cls._string_list(raw.get("emotion"))[:12],
            "usage": cls._string_list(raw.get("usage"))[:12],
            "is_sticker": bool(raw.get("is_sticker", False)),
            "contains_person": bool(raw.get("contains_person", False)),
            "contains_private_info": bool(raw.get("contains_private_info", False)),
            "safety": safety,
        }

    @staticmethod
    def _string_list(value: object) -> list[str]:
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = [value]
            value = parsed
        if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray, str)):
            return []
        result: list[str] = []
        for item in value:
            cleaned = " ".join(str(item).split()).strip()[:80]
            if cleaned and cleaned not in result:
                result.append(cleaned)
        return result

    @staticmethod
    def _safe_error(error: Exception) -> str:
        text = " ".join(str(error).split()).strip()
        return (text or error.__class__.__name__)[:1000]
