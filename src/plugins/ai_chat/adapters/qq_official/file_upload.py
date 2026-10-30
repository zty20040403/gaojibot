"""QQ native file upload: small base64 bodies or sequential presigned parts."""
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

MAX_FILE_BYTES = 100 * 1024 * 1024
SMALL_FILE_BYTES = 5 * 1024 * 1024
DAILY_BYTES = 2 * 1024 * 1024 * 1024
HASH_PREFIX_BYTES = 10_002_432
logger = logging.getLogger(__name__)


class FileUploadError(GenerationError):
    pass


def file_type(filename):
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return 1 if mime.startswith("image/") else 4


@dataclass(frozen=True)
class UploadedFile:
    file_info: str = field(repr=False)
    expires_at: int


class FileUploader:
    def __init__(self, rest, *, clock=time.time, client=None):
        self.rest, self.clock = rest, clock
        self.client = client or httpx.AsyncClient(timeout=120, trust_env=False)
        self._owns_client = client is None
        self.cache = {}
        self._lock = asyncio.Lock()
        self._day, self._bytes = None, 0

    async def close(self):
        if self._owns_client:
            await self.client.aclose()

    async def _request(self, path, payload):
        try:
            return await self.rest.request("POST", path, payload)
        except QQAPIError as error:
            if error.status != 401:
                raise
            self.rest.token_manager.invalidate()
            return await self.rest.request("POST", path, payload)

    async def _multipart(self, path, content, metadata):
        prepared = await self._request(path + "/upload_prepare", metadata)
        upload_id, block_size = prepared["upload_id"], int(prepared["block_size"])
        for part in sorted(prepared["parts"], key=lambda part: part["index"]):
            index = int(part["index"])
            block = content[index * block_size:(index + 1) * block_size]
            # Presigned storage requests never receive the QQ Authorization header.
            response = await self.client.put(part["presigned_url"], content=block)
            response.raise_for_status()
            await self._request(path + "/upload_part_finish", {
                "upload_id": upload_id, "part_index": index,
                "block_size": str(len(block)), "md5": hashlib.md5(block).hexdigest(),
            })
        return await self._request(path + "/files", {
            "upload_id": upload_id, "file_type": metadata["file_type"],
            "file_name": metadata["file_name"], "srv_send_msg": False,
        })

    async def upload(self, endpoint, content, filename):
        if not filename:
            raise FileUploadError("发送文件需要文件名。")
        size = len(content)
        if size > MAX_FILE_BYTES:
            raise FileUploadError("文件超过 100 MB，无法上传。")
        sha = hashlib.sha256(content).hexdigest()
        key = (endpoint.key, sha, filename)
        collection = "groups" if endpoint.kind == "group" else "users"
        path = f"/v2/{collection}/{quote(endpoint.native_conversation_id, safe='')}"
        metadata = dict(file_type=file_type(filename), file_name=filename,
            file_size=str(size), md5=hashlib.md5(content).hexdigest(),
            sha1=hashlib.sha1(content).hexdigest(),
            md5_10m=hashlib.md5(content[:HASH_PREFIX_BYTES]).hexdigest())
        async with self._lock:
            now = int(self.clock())
            self.cache = {k: v for k, v in self.cache.items() if v.expires_at > now}
            if key in self.cache:
                return self.cache[key]
            if self._day != now // 86400:
                self._day, self._bytes = now // 86400, 0
            if self._bytes + size > DAILY_BYTES:
                raise FileUploadError("今天上传的文件已达到 2 GB，请明天再试。")
            started = time.monotonic()
            try:
                if size <= SMALL_FILE_BYTES:
                    response = await self._request(path + "/files", {
                        "file_type": metadata["file_type"], "file_name": filename,
                        "file_data": base64.b64encode(content).decode("ascii"), "srv_send_msg": False,
                    })
                else:
                    response = await self._multipart(path, content, metadata)
            except QQAPIError as error:
                if error.code == 50015014:
                    try:
                        await self._request(path + "/upload_prepare", metadata)
                    except QQAPIError as diagnostic:
                        error = diagnostic
                logger.warning("QQ upload bytes=%d elapsed=%.2f result=rejected code=%s",
                               size, time.monotonic() - started, error.code)
                detail = {40093003: "群文件超过上限，请清理群文件空间。",
                          850012: "腾讯拒绝了文件上传参数。",
                          40034006: "文件内容未通过腾讯审核。"}.get(error.code, "腾讯未接受文件上传。")
                raise FileUploadError(f"{detail}（错误码：{error.code or error.status}）") from None
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
                logger.warning("QQ upload bytes=%d elapsed=%.2f result=failed error=%s",
                               size, time.monotonic() - started, type(error).__name__)
                raise FileUploadError("上传未完成，文件还没有发送，可以重试。") from None
            info = response.get("file_info")
            if not isinstance(info, str) or not info:
                raise FileUploadError("腾讯没有返回文件信息，文件还没有发送，可以重试。")
            result = UploadedFile(info, int(self.clock()) + min(86400, int(response.get("ttl", 86400))))
            self.cache[key] = result
            self._bytes += size
            logger.info("QQ upload bytes=%d elapsed=%.2f result=uploaded", size, time.monotonic() - started)
            return result
