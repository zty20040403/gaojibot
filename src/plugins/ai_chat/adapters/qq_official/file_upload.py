"""QQ native file upload: <=5 MiB base64 single-shot or >5 MiB multipart."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import hashlib
import logging
import mimetypes
import time
from urllib.parse import quote

import httpx

from src.qq_official.errors import QQAPIError
from ...application.owned_sandbox import GenerationError

MAX_FILE_BYTES = 200 * 1024 * 1024
SMALL_FILE_BYTES = 5 * 1024 * 1024
HASH_PREFIX_BYTES = 10_002_432
logger = logging.getLogger(__name__)

_UPLOAD_REASONS = {
    40093003: "群文件超过上限，请清理群文件空间。",
    40093001: "分片上传会话无效或已过期，请重试。",
    40093002: "分片上传校验失败，请重试。",
    40093006: "文件大小超过腾讯单文件上限（200 MB）。",
    40093011: "图片或文件名参数不符合腾讯要求。",
    850012: "腾讯拒绝了文件上传参数（大小或摘要不合法）。",
    40034006: "文件内容未通过腾讯安全审核。",
    40034105: "这个群没开主动发言，请群主打开。",
}


class FileUploadError(GenerationError):
    pass


def file_type(filename: str) -> int:
    if filename.lower().endswith(".silk"):
        return 3
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return 1 if mime.startswith("image/") else 4


@dataclass(frozen=True)
class UploadedFile:
    file_info: str = field(repr=False)
    expires_at: int


class FileUploader:
    def __init__(self, rest, *, clock=time.time, client=None):
        self.rest, self.clock = rest, clock
        self.client = client or httpx.AsyncClient(timeout=240, trust_env=False)
        self._owns_client = client is None
        self.cache: dict[tuple[str, str, str], UploadedFile] = {}
        self._lock = asyncio.Lock()

    async def close(self):
        if self._owns_client:
            await self.client.aclose()

    async def _request(self, path, payload, current=None):
        if current is not None:
            await current()
        try:
            return await self.rest.request("POST", path, payload)
        except QQAPIError as error:
            if error.category != "authentication" or not error.confirmed_not_accepted or error.stage == "token":
                raise
            if current is not None:
                await current()
            return await self.rest.request("POST", path, payload)

    async def _multipart(self, path, content, metadata, current=None):
        part_meta = (
            {**metadata, "file_name": "picture"}
            if metadata["file_type"] == 1
            else metadata
        )
        prepared = await self._request(path + "/upload_prepare", part_meta, current)
        upload_id, block_size = prepared["upload_id"], int(prepared["block_size"])
        parts = sorted(prepared["parts"], key=lambda item: int(item["index"]))
        for pos, part in enumerate(parts):
            index = int(part["index"])
            block = content[pos * block_size:(pos + 1) * block_size]
            if current is not None:
                await current()
            response = await self.client.put(part["presigned_url"], content=block)
            response.raise_for_status()
            await self._request(path + "/upload_part_finish", {
                "upload_id": upload_id,
                "part_index": index,
                "block_size": str(len(block)),
                "md5": hashlib.md5(block).hexdigest(),
            }, current)
        return await self._request(path + "/files", {
            "upload_id": upload_id,
            "file_type": part_meta["file_type"],
            "file_name": part_meta["file_name"],
            "srv_send_msg": False,
        }, current)

    async def upload(self, endpoint, content: bytes, filename: str, *, current=None) -> UploadedFile:
        if not filename:
            raise FileUploadError("发送文件需要文件名。")
        size = len(content)
        if size <= 0:
            raise FileUploadError("文件为空，无法上传。")
        if size > MAX_FILE_BYTES:
            raise FileUploadError("文件超过 200 MB，无法上传。")
        sha = hashlib.sha256(content).hexdigest()
        key = (endpoint.key, sha, filename)
        collection = "groups" if endpoint.kind == "group" else "users"
        path = f"/v2/{collection}/{quote(endpoint.native_conversation_id, safe='')}"
        metadata = dict(
            file_type=file_type(filename),
            file_name=filename,
            file_size=str(size),
            md5=hashlib.md5(content).hexdigest(),
            sha1=hashlib.sha1(content).hexdigest(),
            md5_10m=hashlib.md5(content[:HASH_PREFIX_BYTES]).hexdigest(),
        )
        async with self._lock:
            if current is not None:
                await current()
            now = int(self.clock())
            self.cache = {k: v for k, v in self.cache.items() if v.expires_at > now}
            if key in self.cache:
                return self.cache[key]
            started = time.monotonic()
            deadline = asyncio.timeout(300)
            try:
                async with deadline:
                    try:
                        if size <= SMALL_FILE_BYTES:
                            response = await self._request(path + "/files", {
                                "file_type": metadata["file_type"],
                                "file_name": filename,
                                "file_data": base64.b64encode(content).decode("ascii"),
                                "srv_send_msg": False,
                            }, current)
                        else:
                            response = await self._multipart(path, content, metadata, current)
                    except QQAPIError as error:
                        if error.category == "native_file_unavailable" and error.confirmed_not_accepted:
                            try:
                                await self._request(path + "/upload_prepare", metadata, current)
                            except QQAPIError as diagnostic:
                                error = diagnostic
                        if not error.confirmed_not_accepted:
                            raise FileUploadError("上传未确认，远端结果未知，请先核实，不要直接重复上传。") from None
                        logger.warning("QQ upload bytes=%d elapsed=%.2f result=rejected code=%s",
                                       size, time.monotonic() - started, error.code)
                        detail = _UPLOAD_REASONS.get(error.code, f"腾讯未接受文件上传（{error.category}）。")
                        raise FileUploadError(f"{detail}（错误码：{error.code or error.status}）") from None
                    except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
                        logger.warning("QQ upload bytes=%d elapsed=%.2f result=failed error=%s",
                                       size, time.monotonic() - started, type(error).__name__)
                        raise FileUploadError("上传未确认，远端结果未知，请先核实，不要直接重复上传。") from None
                    info = response.get("file_info")
                    if not isinstance(info, str) or not info:
                        raise FileUploadError("腾讯没有返回文件信息，远端上传结果未知，请先核实，不要直接重复上传。")
            except TimeoutError as error:
                if not deadline.expired():
                    raise
                raise FileUploadError("上传超时，远端结果未知，请先核实，不要直接重复上传。") from error
            result = UploadedFile(info, int(self.clock()) + min(86400, int(response.get("ttl", 86400))))
            self.cache[key] = result
            logger.info("QQ upload bytes=%d elapsed=%.2f result=uploaded", size, time.monotonic() - started)
            return result
