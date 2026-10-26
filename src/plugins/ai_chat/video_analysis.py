from __future__ import annotations

import asyncio
import json
import shutil
import time
import weakref
from collections.abc import Awaitable, Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urljoin, urlsplit

import httpx
from nonebot import logger

from .content_sources import ContentSource, ContentSourceStore
from .media_tools import BilibiliClient
from .paths import CACHE_DIR
from .vision_worker import VisionWorker


ProgressCallback = Callable[[str], Awaitable[None]]


class DeepVideoAnalysisError(RuntimeError):
    pass


class DeepVideoAnalyzer:
    def __init__(
        self,
        source_store: ContentSourceStore,
        vision_worker: VisionWorker,
        *,
        whisper_model_path: str,
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
        whisper_path: str = "whisper-cli",
        frame_count: int = 12,
        max_download_bytes: int = 1024 * 1024 * 1024,
        max_duration_seconds: int = 60 * 60,
        timeout_seconds: int = 1800,
        whisper_threads: int = 8,
        cache_seconds: int = 24 * 60 * 60,
    ) -> None:
        self.source_store = source_store
        self.vision_worker = vision_worker
        self.whisper_model_path = str(whisper_model_path).strip()
        self.ffmpeg_path = str(ffmpeg_path).strip() or "ffmpeg"
        self.ffprobe_path = str(ffprobe_path).strip() or "ffprobe"
        self.whisper_path = str(whisper_path).strip() or "whisper-cli"
        self.frame_count = min(max(int(frame_count), 4), 12)
        self.max_download_bytes = max(int(max_download_bytes), 10 * 1024 * 1024)
        self.max_duration_seconds = max(int(max_duration_seconds), 60)
        self.timeout_seconds = max(int(timeout_seconds), 60)
        self.whisper_threads = min(max(int(whisper_threads), 1), 32)
        self.cache_seconds = max(int(cache_seconds), 60)
        self.cache_root = CACHE_DIR / "deep-video"
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self._locks: weakref.WeakValueDictionary[int, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._validate_runtime()

    async def analyze(
        self,
        source: ContentSource,
        *,
        question: str = "",
        force_refresh: bool = False,
        progress: ProgressCallback | None = None,
    ) -> tuple[dict[str, object], bool]:
        if source.platform != "bilibili":
            raise DeepVideoAnalysisError("深度视频分析目前先支持 B 站。")
        if not force_refresh:
            cached = self.source_store.cached_deep_analysis(
                source,
                max_age_seconds=self.cache_seconds,
            )
            if cached is not None:
                return cached, True
        lock = self._locks.setdefault(source.source_id, asyncio.Lock())
        async with lock:
            refreshed = self.source_store.refresh_source(source.source_id)
            if not force_refresh:
                cached = self.source_store.cached_deep_analysis(
                    refreshed,
                    max_age_seconds=self.cache_seconds,
                )
                if cached is not None:
                    return cached, True
            await self._notify(progress, "正在读取视频流并准备抽帧和音轨。")
            result = await self._analyze_uncached(
                refreshed,
                question=question,
                progress=progress,
            )
            await asyncio.to_thread(
                self.source_store.save_deep_analysis,
                source.source_id,
                result,
            )
            return result, False

    async def analyze_qq_video(
        self,
        source_url: str,
        *,
        question: str = "",
        progress: ProgressCallback | None = None,
    ) -> dict[str, object]:
        if not self._is_allowed_qq_media_url(source_url):
            raise DeepVideoAnalysisError("QQ 视频地址无效或不在允许的媒体域名内。")
        await self._notify(progress, "正在下载 QQ 视频并检查时长。")
        with TemporaryDirectory(prefix="qq-video-", dir=self.cache_root) as directory:
            root = Path(directory)
            video_path = root / "video.bin"
            await self._download_qq_media(source_url, video_path)
            duration = await self._probe_duration(video_path)
            if duration <= 0:
                raise DeepVideoAnalysisError("没有读取到有效视频时长。")
            if duration > self.max_duration_seconds:
                raise DeepVideoAnalysisError(
                    f"视频超过分析上限 {self.max_duration_seconds // 60} 分钟。"
                )
            await self._notify(progress, "视频已下载，正在抽取关键画面并转写音轨。")
            frames, transcript = await asyncio.gather(
                self._extract_frames(video_path, root, duration),
                self._transcribe_audio(video_path, root),
            )
            await self._notify(progress, "画面和音轨已经准备好，正在综合评价。")
            visual = await self.vision_worker.analyze_video_frames(
                frames,
                context=json.dumps(
                    {
                        "source": "QQ 原生视频",
                        "duration_seconds": duration,
                    },
                    ensure_ascii=False,
                ),
                transcript=transcript,
                question=question,
            )
        return {
            "mode": "deep",
            "source": "qq_video",
            "duration_seconds": duration,
            "frame_count": len(frames),
            "transcript": transcript[:12000],
            "visual": visual,
            "analyzed_at": int(time.time()),
            "limitations": [
                "画面结论来自均匀抽取的关键帧，不等于逐帧检查。",
                (
                    "音轨已由本地 Whisper 转写，可能存在专有名词误识别。"
                    if transcript
                    else "本次没有获得可用音轨转写。"
                ),
                "QQ 原视频和分析临时文件已在本轮结束后删除。",
            ],
        }

    async def _analyze_uncached(
        self,
        source: ContentSource,
        *,
        question: str,
        progress: ProgressCallback | None,
    ) -> dict[str, object]:
        client = BilibiliClient(timeout_seconds=30)
        try:
            streams = await client.media_streams(source.canonical_url, max_height=480)
        finally:
            await client.close()
        duration = int(streams.get("duration_seconds") or 0)
        if duration <= 0:
            raise DeepVideoAnalysisError("B站没有返回有效视频时长。")
        if duration > self.max_duration_seconds:
            raise DeepVideoAnalysisError(
                f"视频超过深度分析上限 {self.max_duration_seconds // 60} 分钟。"
            )

        with TemporaryDirectory(prefix="task-", dir=self.cache_root) as directory:
            root = Path(directory)
            video_path = root / "video.m4s"
            audio_path = root / "audio.m4s"
            await self._download(
                str(streams.get("video_url") or ""),
                video_path,
                max_bytes=(self.max_download_bytes * 3) // 4,
            )
            await self._download(
                str(streams.get("audio_url") or ""),
                audio_path,
                max_bytes=self.max_download_bytes // 2,
            )
            if (
                video_path.stat().st_size + audio_path.stat().st_size
                > self.max_download_bytes
            ):
                raise DeepVideoAnalysisError("视频下载超过大小上限。")
            await self._notify(progress, "视频已下载，正在抽取关键画面并转写音轨。")
            frames, transcript = await asyncio.gather(
                self._extract_frames(video_path, root, duration),
                self._transcribe_audio(audio_path, root),
            )
            context = json.dumps(
                {
                    "title": source.title,
                    "author": source.author,
                    "summary": source.summary,
                    "duration_seconds": duration,
                    "stats": source.metadata.get("stats", {}),
                },
                ensure_ascii=False,
            )
            await self._notify(progress, "画面和音轨已经准备好，正在做综合分析。")
            visual = await self.vision_worker.analyze_video_frames(
                frames,
                context=context,
                transcript=transcript,
                question=question,
            )
        return {
            "mode": "deep",
            "bvid": str(streams.get("bvid") or source.remote_id),
            "duration_seconds": duration,
            "frame_count": len(frames),
            "transcript": transcript[:12000],
            "visual": visual,
            "analyzed_at": int(time.time()),
            "limitations": [
                "画面结论来自均匀抽取的关键帧，不等于逐帧检查。",
                (
                    "音轨已由本地 Whisper 转写，可能存在专有名词误识别。"
                    if transcript
                    else "本次没有获得可用音轨转写。"
                ),
            ],
        }

    @staticmethod
    def _check_capacity(max_bytes: int) -> None:
        from src.storage_admission import CapacityError, require_capacity
        try:
            require_capacity(max_bytes)
        except CapacityError as error:
            raise DeepVideoAnalysisError(str(error)) from None

    async def _download(self, url: str, target: Path, *, max_bytes: int) -> None:
        self._check_capacity(max_bytes)
        if not url.startswith(("http://", "https://")):
            raise DeepVideoAnalysisError("B站媒体流地址无效。")
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0",
            "Referer": "https://www.bilibili.com/",
        }
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds),
                follow_redirects=True,
                headers=headers,
            ) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    expected = int(response.headers.get("content-length") or 0)
                    if expected > max_bytes:
                        raise DeepVideoAnalysisError("B站媒体流超过大小上限。")
                    size = 0
                    with target.open("wb") as output:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > max_bytes:
                                raise DeepVideoAnalysisError("B站媒体流超过大小上限。")
                            output.write(chunk)
        except httpx.HTTPError as exc:
            raise DeepVideoAnalysisError("下载 B站媒体流失败。") from exc
        if not target.is_file() or target.stat().st_size <= 0:
            raise DeepVideoAnalysisError("B站媒体流为空。")

    async def _download_qq_media(self, url: str, target: Path) -> None:
        self._check_capacity(self.max_download_bytes)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) "
                "Gecko/20100101 Firefox/130.0"
            )
        }
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds),
                follow_redirects=False,
                headers=headers,
            ) as client:
                current_url = url
                for redirect_count in range(6):
                    if not self._is_allowed_qq_media_url(current_url):
                        raise DeepVideoAnalysisError(
                            "QQ 视频跳转到了不允许的媒体域名。"
                        )
                    async with client.stream("GET", current_url) as response:
                        if response.is_redirect:
                            location = response.headers.get("location", "").strip()
                            if not location or redirect_count >= 5:
                                raise DeepVideoAnalysisError("QQ 视频跳转地址无效。")
                            current_url = urljoin(str(response.url), location)
                            continue
                        response.raise_for_status()
                        expected = int(response.headers.get("content-length") or 0)
                        if expected > self.max_download_bytes:
                            raise DeepVideoAnalysisError("QQ 视频超过大小上限。")
                        size = 0
                        with target.open("wb") as output:
                            async for chunk in response.aiter_bytes():
                                size += len(chunk)
                                if size > self.max_download_bytes:
                                    raise DeepVideoAnalysisError(
                                        "QQ 视频超过大小上限。"
                                    )
                                output.write(chunk)
                        break
                else:
                    raise DeepVideoAnalysisError("QQ 视频跳转次数过多。")
        except httpx.HTTPError as exc:
            raise DeepVideoAnalysisError("下载 QQ 视频失败。") from exc
        if not target.is_file() or target.stat().st_size <= 0:
            raise DeepVideoAnalysisError("下载到的 QQ 视频为空。")

    async def _probe_duration(self, video_path: Path) -> int:
        output = await self._run_capture(
            self.ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
            timeout=min(self.timeout_seconds, 60),
        )
        try:
            return max(int(float(output.strip())), 0)
        except ValueError as exc:
            raise DeepVideoAnalysisError("无法读取视频时长。") from exc

    async def _extract_frames(
        self,
        video_path: Path,
        root: Path,
        duration: int,
    ) -> list[tuple[int, bytes]]:
        pattern = root / "frame-%02d.jpg"
        fps = self.frame_count / max(float(duration), 1.0)
        await self._run(
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video_path),
            "-vf",
            f"fps={fps:.8f},scale=960:-2:force_original_aspect_ratio=decrease",
            "-frames:v",
            str(self.frame_count),
            "-q:v",
            "3",
            str(pattern),
            timeout=min(self.timeout_seconds, 180),
        )
        paths = sorted(root.glob("frame-*.jpg"))[: self.frame_count]
        if not paths:
            raise DeepVideoAnalysisError("视频关键帧提取失败。")
        return [
            (int(index * duration / max(len(paths), 1)), path.read_bytes())
            for index, path in enumerate(paths)
        ]

    async def _transcribe_audio(self, audio_path: Path, root: Path) -> str:
        wav_path = root / "audio.wav"
        output = root / "transcript"
        try:
            await self._run(
                self.ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(audio_path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(wav_path),
                timeout=min(self.timeout_seconds, 180),
            )
            await self._run(
                self.whisper_path,
                "-m",
                self.whisper_model_path,
                "-f",
                str(wav_path),
                "-l",
                "auto",
                "-t",
                str(self.whisper_threads),
                "-otxt",
                "-of",
                str(output),
                "-np",
                "-ng",
                timeout=self.timeout_seconds,
            )
        except DeepVideoAnalysisError as exc:
            logger.warning(f"Deep video audio transcription failed: {exc}")
            return ""
        transcript_path = output.with_suffix(".txt")
        if not transcript_path.is_file():
            return ""
        return " ".join(
            transcript_path.read_text(encoding="utf-8", errors="replace").split()
        )[:12000]

    async def _run(self, *command: str, timeout: int) -> None:
        await self._run_capture(*command, timeout=timeout)

    async def _run_capture(self, *command: str, timeout: int) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise DeepVideoAnalysisError(f"无法启动 {command[0]}。") from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=max(int(timeout), 1)
            )
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise DeepVideoAnalysisError(f"{command[0]} 执行超时。") from exc
        if process.returncode != 0:
            detail = (stderr or stdout).decode("utf-8", errors="replace")[-600:]
            raise DeepVideoAnalysisError(
                f"{command[0]} 执行失败：{detail or process.returncode}"
            )
        return stdout.decode("utf-8", errors="replace")

    async def _notify(
        self,
        progress: ProgressCallback | None,
        text: str,
    ) -> None:
        if progress is None:
            return
        try:
            await progress(text)
        except Exception as exc:
            logger.debug(f"Could not send deep video progress: {exc}")

    def _validate_runtime(self) -> None:
        for executable in (self.ffmpeg_path, self.ffprobe_path, self.whisper_path):
            if not shutil.which(executable):
                raise DeepVideoAnalysisError(f"缺少运行程序：{executable}")
        model = Path(self.whisper_model_path)
        if not model.is_file():
            raise DeepVideoAnalysisError("Whisper 模型文件不存在。")

    @staticmethod
    def _is_allowed_qq_media_url(url: str) -> bool:
        parsed = urlsplit(str(url).strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        hostname = parsed.hostname.rstrip(".").lower()
        return any(
            hostname == suffix or hostname.endswith(f".{suffix}")
            for suffix in ("qq.com", "qq.com.cn", "qpic.cn")
        )
