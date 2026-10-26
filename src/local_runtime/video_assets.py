"""Fixed, checksummed video model installation. No user URL or host path API."""
from __future__ import annotations

import hashlib
import http.client
import os
from pathlib import Path
import re
import shutil
import stat
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .files import file_lock, read_json, write_json
from .validation import InstanceError


class VideoAssetError(InstanceError):
    pass


MODEL_NAME = "Whisper Base 多语言"
MODEL_FILE = "ggml-base.bin"
MODEL_BYTES = 147951465
MODEL_SHA256 = "60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe"
MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-base.bin"
CONTAINER_MODEL_PATH = "/opt/gaoji-models/" + MODEL_FILE


def _trusted_url(url: str) -> bool:
    value = urlsplit(url)
    host = (value.hostname or "").lower()
    return value.scheme == "https" and not value.username and not value.password and value.port in {None, 443} and any(
        host == suffix or host.endswith("." + suffix) for suffix in ("huggingface.co", "hf.co"))


class ModelRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _trusted_url(newurl):
            raise VideoAssetError("转写模型下载跳转到未支持的地址，已停止")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _regular(path: Path) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise VideoAssetError("转写模型路径不是本实例的普通文件")
    return info


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class VideoAssets:
    def __init__(self, instance: Path):
        self.root = instance / "assets"
        self.target = self.root / MODEL_FILE
        self.part = self.root / (MODEL_FILE + ".part")
        self.record = self.root / "video-model.json"

    def status(self) -> dict:
        result = {"model": MODEL_NAME, "total_bytes": MODEL_BYTES, "downloaded_bytes": 0, "state": "not_installed"}
        try:
            self._directory()
            if not self.root.exists():
                return result
            info = _regular(self.target)
            record = read_json(self.record) if self.record.exists() else {}
            if info and info.st_size == MODEL_BYTES and record.get("sha256") == MODEL_SHA256 and record.get("mtime_ns") == info.st_mtime_ns:
                return {**result, "state": "installed", "downloaded_bytes": MODEL_BYTES}
            partial = _regular(self.part)
            if partial and 0 < partial.st_size <= MODEL_BYTES:
                return {**result, "state": "partial", "downloaded_bytes": partial.st_size}
            return {**result, "state": "needs_verification" if info else "not_installed"}
        except (OSError, ValueError):
            return {**result, "state": "invalid"}

    def _directory(self) -> None:
        if self.root.is_symlink() or self.root.exists() and not self.root.is_dir():
            raise VideoAssetError("转写模型目录不可用")

    def prepare(self) -> None:
        self._directory()
        self.root.mkdir(mode=0o755, exist_ok=True)

    def ensure(self, progress=None, *, admit=None) -> Path:
        self.prepare()
        with file_lock(self.root / ".video.lock"):
            return self._install(progress or (lambda _received, _total: None), admit=admit)

    def _installed(self) -> Path:
        self.target.chmod(0o444)
        write_json(self.record, {"sha256": MODEL_SHA256, "bytes": MODEL_BYTES, "mtime_ns": self.target.stat().st_mtime_ns})
        return self.target

    def _install(self, progress, *, admit=None) -> Path:
        info = _regular(self.target)
        if info and info.st_size == MODEL_BYTES and _digest(self.target) == MODEL_SHA256:
            progress(MODEL_BYTES, MODEL_BYTES)
            return self._installed()
        partial = _regular(self.part)
        offset = partial.st_size if partial else 0
        if offset > MODEL_BYTES:
            raise VideoAssetError("未完成的转写模型大小异常，请先核对本实例模型目录")
        if offset == MODEL_BYTES:
            if _digest(self.part) == MODEL_SHA256:
                os.replace(self.part, self.target)
                return self._installed()
            self.part.unlink()
            offset = 0
        if admit is not None:
            # A range request may be answered with the complete file.
            admit(MODEL_BYTES + 64 * 1024**2)
        if shutil.disk_usage(self.root).free < MODEL_BYTES - offset + 256 * 1024**2:
            raise VideoAssetError("转写模型下载空间不足，需另留至少 256 MiB 余量；不会删除用户数据")
        if not _trusted_url(MODEL_URL):
            raise VideoAssetError("转写模型下载源无效")
        headers = {"Accept-Encoding": "identity", "User-Agent": "Gaoji-Desktop-Installer"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        progress(offset, MODEL_BYTES)
        deadline = time.monotonic() + 900
        try:
            opener = urllib.request.build_opener(ModelRedirects())
            with opener.open(urllib.request.Request(MODEL_URL, headers=headers), timeout=30) as response:
                if not _trusted_url(response.url):
                    raise VideoAssetError("转写模型来源不受支持")
                if response.status == 206:
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                    if not match or int(match[1]) != offset or int(match[3]) != MODEL_BYTES or not offset <= int(match[2]) < MODEL_BYTES:
                        raise VideoAssetError("转写模型续传范围不正确，已保留未完成下载")
                elif response.status == 200:
                    offset = 0
                else:
                    raise VideoAssetError("转写模型下载服务暂不可用，重试会继续同一份下载")
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isdigit() or int(length) > MODEL_BYTES - offset):
                    raise VideoAssetError("转写模型下载大小与固定版本不符")
                if response.headers.get("Content-Encoding", "identity") not in {"", "identity"}:
                    raise VideoAssetError("转写模型下载编码不受支持")
                _regular(self.part)
                flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_APPEND if offset else os.O_TRUNC)
                with os.fdopen(os.open(self.part, flags, 0o600), "wb") as output:
                    received = offset
                    reported = -1
                    while True:
                        if time.monotonic() >= deadline:
                            raise VideoAssetError("转写模型下载超时，未完成文件已保留，可重试续传")
                        data = response.read1(1024**2)
                        if not data:
                            break
                        received += len(data)
                        if received > MODEL_BYTES:
                            raise VideoAssetError("转写模型超过固定大小，已停止下载")
                        output.write(data)
                        percent = received * 100 // MODEL_BYTES
                        if percent != reported:
                            progress(received, MODEL_BYTES)
                            reported = percent
                    output.flush()
                    os.fsync(output.fileno())
            if received != MODEL_BYTES:
                raise VideoAssetError("转写模型下载未完成，重试会继续下载")
            if _digest(self.part) != MODEL_SHA256:
                self.part.unlink()
                raise VideoAssetError("转写模型校验失败，未启用；请重试下载")
            os.replace(self.part, self.target)
            return self._installed()
        except (OSError, http.client.HTTPException, urllib.error.URLError):
            raise VideoAssetError("服务器无法下载转写模型，未完成文件已保留；请检查网络后重试") from None
