"""Versioned, bounded sandbox RPC. Binary files are never JSON/base64 encoded."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import PurePosixPath
import re
import struct

from .docker import NIX_ATTRIBUTE_PATTERN, RUNTIME_IMAGES, SANDBOX_ID_PATTERN, SandboxError
from .project_volumes import MIN_QUOTA, MAX_QUOTA, MIB
from src.project_content import CHUNK_BYTES, MAX_FILE_BYTES, MAX_MANIFEST_BYTES, MAX_VERSION_FILES


VERSION = 1
HEADER_LIMIT = 1024 * 1024
REQUEST_LIMIT = 64 * 1024
ID_PATTERN = re.compile(r"[a-f0-9]{32}")
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}")
MUTATIONS = frozenset({"create", "ensure_default", "destroy", "exec", "write_file",
                      "install_readonly_file", "start_owned", "stop_owned", "reclaim_stopped",
                      "project_volume_create", "project_volume_mount", "project_volume_unmount",
                      "project_volume_resize", "project_volume_fsck", "project_volume_trim", "project_volume_delete",
                      "project_workspace_start", "project_workspace_stop", "project_workspace_remove",
                      "project_workspace_reserve", "project_workspace_release"})
FIELDS = {
    "health": (set(), set()), "admin_snapshot": (set(), set()),
    "cancel": ({"request_id"}, set()),
    "list": ({"owner"}, set()),
    "create": ({"owner"}, {"runtime", "purpose"}),
    "ensure_default": ({"owner"}, {"runtime"}),
    "destroy": ({"owner", "sandbox_id"}, set()),
    "start_owned": ({"owner", "sandbox_id"}, set()),
    "stop_owned": ({"owner", "sandbox_id"}, set()),
    "reclaim_stopped": ({"owner", "sandbox_id"}, {"not_used_since"}),
    "exec": ({"owner", "sandbox_id", "command"}, {"timeout_seconds", "packages"}),
    "search_nix_packages": ({"query"}, set()),
    "write_file": ({"owner", "sandbox_id", "path"}, {"allow_large"}),
    "install_readonly_file": ({"owner", "sandbox_id", "path"}, set()),
    "read_file": ({"owner", "sandbox_id", "path"}, {"max_bytes"}),
    "export_artifact": ({"owner", "sandbox_id", "path"}, set()),
    "project_volume_status": ({"owner", "project_id"}, {"generation"}),
    "project_volume_create": ({"owner", "project_id", "quota_bytes", "fence"}, {"generation"}),
    "project_volume_mount": ({"owner", "project_id", "fence"}, {"generation", "reservation"}),
    "project_volume_unmount": ({"owner", "project_id", "fence"}, {"generation", "reservation"}),
    "project_volume_resize": ({"owner", "project_id", "quota_bytes", "fence"}, {"generation", "reservation"}),
    "project_volume_fsck": ({"owner", "project_id", "fence"}, {"generation", "reservation"}),
    "project_volume_trim": ({"owner", "project_id", "fence"}, {"generation", "reservation"}),
    "project_volume_delete": ({"owner", "project_id", "fence"}, {"generation", "reservation"}),
    "project_workspace_status": ({"owner", "project_id", "generation"}, set()),
    "project_workspace_start": ({"owner", "project_id", "generation", "fence"}, {"reservation"}),
    "project_workspace_stop": ({"owner", "project_id", "generation", "fence"}, {"reservation"}),
    "project_workspace_remove": ({"owner", "project_id", "generation", "fence"}, {"reservation"}),
    "project_workspace_reserve": ({"owner", "project_id", "generation", "fence", "reservation"}, set()),
    "project_workspace_release": ({"owner", "project_id", "generation", "fence", "reservation"}, set()),
}
PROJECT_FILE_FIELDS = {"owner", "project_id", "generation", "fence", "reservation"}
for _action in ("run", "status"):
    FIELDS["project_rebuild_" + _action] = (PROJECT_FILE_FIELDS | {"manifest_sha256"}, set())
MUTATIONS |= {"project_rebuild_run"}
for _action, _fields, _optional in (
    ("snapshot", set(), {"rebuild_command"}),
    ("page", {"manifest_sha256", "index"}, set()),
    ("read", {"manifest_sha256", "index", "offset"}, {"max_bytes"}),
    ("check", {"manifest_sha256"}, set()),
    ("restore_begin", {"manifest_sha256", "manifest_size"}, set()),
    ("restore_manifest", {"manifest_sha256", "offset"}, set()),
    ("restore_file", {"manifest_sha256", "index", "offset"}, set()),
    ("restore_finish", {"manifest_sha256"}, set()),
    ("restore_check", {"manifest_sha256"}, set()),
    ("prune", {"manifest_sha256", "snapshot_fence"}, set()),
    ("prune_check", {"manifest_sha256", "snapshot_fence"}, set()),
):
    FIELDS["project_files_" + _action] = (PROJECT_FILE_FIELDS | _fields, _optional)
MUTATIONS |= frozenset("project_files_" + action for action in (
    "snapshot", "restore_begin", "restore_manifest", "restore_file", "restore_finish", "prune"
))
UPLOADS = {"write_file", "install_readonly_file", "project_files_restore_manifest", "project_files_restore_file"}


class ProtocolError(SandboxError):
    pass


def encoded(value: dict) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (ValueError, TypeError):
        raise ProtocolError("沙盒请求格式不正确。") from None


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate field")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(value)


async def read_header(reader, *, limit: int = HEADER_LIMIT, timeout: float = 15) -> dict:
    async def receive():
        size = struct.unpack("!I", await reader.readexactly(4))[0]
        if not 0 < size <= limit:
            raise ProtocolError("沙盒消息头超过大小限制。")
        try:
            value = json.loads(await reader.readexactly(size), object_pairs_hook=_unique_pairs,
                               parse_constant=_reject_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise ProtocolError("沙盒消息格式不正确。") from None
        if not isinstance(value, dict):
            raise ProtocolError("沙盒消息必须为对象。")
        return value
    return await asyncio.wait_for(receive(), timeout)


async def write_frame(writer, header: dict, payload: bytes = b"", *, timeout: float = 30) -> None:
    if header.get("payload_size", 0) != len(payload):
        raise ProtocolError("沙盒文件长度不一致。")
    body = encoded(header)
    if len(body) > HEADER_LIMIT:
        raise ProtocolError("沙盒响应超过消息头限制。")

    async def send():
        writer.write(struct.pack("!I", len(body)) + body)
        await writer.drain()
        for offset in range(0, len(payload), 65536):
            writer.write(payload[offset:offset + 65536])
            await writer.drain()
    await asyncio.wait_for(send(), timeout)


def _text(value, maximum: int, *, blank: bool = False) -> bool:
    return isinstance(value, str) and len(value) <= maximum and (blank or bool(value.strip())) and "\0" not in value


def validate_request(request: dict, *, file_limit: int) -> tuple[str, dict]:
    if set(request) != {"schema", "id", "token", "method", "args", "payload_size"}:
        raise ProtocolError("沙盒请求字段不完整或包含未开放字段。")
    if type(request["schema"]) is not int or request["schema"] != VERSION:
        raise ProtocolError("沙盒通道版本不兼容。")
    if not isinstance(request["id"], str) or not ID_PATTERN.fullmatch(request["id"]):
        raise ProtocolError("沙盒请求编号无效。")
    method, args, size = request["method"], request["args"], request["payload_size"]
    if not isinstance(method, str) or method not in FIELDS or not isinstance(args, dict):
        raise ProtocolError("沙盒通道没有开放此操作。")
    required, optional = FIELDS[method]
    if not required <= set(args) or set(args) - required - optional:
        raise ProtocolError("沙盒操作参数不正确。")
    if method == "cancel" and (not isinstance(args["request_id"], str) or not ID_PATTERN.fullmatch(args["request_id"]) or args["request_id"] == request["id"]):
        raise ProtocolError("取消目标编号无效。")
    if type(size) is not int or not 0 <= size <= file_limit or (method not in UPLOADS and size):
        raise ProtocolError("沙盒文件大小不正确或超过传输上限。")
    if "owner" in args and not _text(args["owner"], 200):
        raise ProtocolError("沙盒所属会话无效。")
    for field in ("project_id", "fence", "generation", "snapshot_fence"):
        if field in args and (type(args[field]) is not int or not 1 <= args[field] < 2**53):
            raise ProtocolError("项目编号或操作代次无效。")
    if "reservation" in args and (not isinstance(args["reservation"], str) or not ID_PATTERN.fullmatch(args["reservation"])):
        raise ProtocolError("项目维护凭据无效。")
    if method.startswith(("project_files_", "project_rebuild_")):
        if "rebuild_command" in args and not _text(args["rebuild_command"], 4000, blank=True):
            raise ProtocolError("项目重建命令不正确。")
        if size > CHUNK_BYTES:
            raise ProtocolError("项目文件分块超过传输上限。")
        for field, lower, upper in (("index", 0, MAX_VERSION_FILES), ("offset", 0, MAX_FILE_BYTES),
                                    ("manifest_size", 1, MAX_MANIFEST_BYTES), ("max_bytes", 1, min(CHUNK_BYTES, file_limit))):
            if field in args and (type(args[field]) is not int or not lower <= args[field] <= upper):
                raise ProtocolError("项目文件分块参数无效。")
        if "manifest_sha256" in args and (not isinstance(args["manifest_sha256"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", args["manifest_sha256"])):
            raise ProtocolError("项目清单校验值无效。")
    if "quota_bytes" in args and (type(args["quota_bytes"]) is not int
            or not MIN_QUOTA <= args["quota_bytes"] <= MAX_QUOTA or args["quota_bytes"] % MIB):
        raise ProtocolError("项目卷配额必须为 64 MiB 至 100 GiB，按 MiB 对齐。")
    if "sandbox_id" in args and (not isinstance(args["sandbox_id"], str) or not SANDBOX_ID_PATTERN.fullmatch(args["sandbox_id"])):
        raise ProtocolError("沙盒编号无效。")
    if "runtime" in args and (not isinstance(args["runtime"], str) or args["runtime"] not in RUNTIME_IMAGES):
        raise ProtocolError("沙盒运行环境无效。")
    if "purpose" in args and args["purpose"] not in ("shell", "task"):
        raise ProtocolError("沙盒用途无效。")
    if "path" in args:
        value = args["path"]
        if not _text(value, 1024) or PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts or str(PurePosixPath(value)) == ".":
            raise ProtocolError("文件路径必须在沙盒工作区内。")
    if "command" in args and not _text(args["command"], 32768):
        raise ProtocolError("沙盒命令为空或过长。")
    if "query" in args and not _text(args["query"], 80):
        raise ProtocolError("软件包检索词为空或过长。")
    for key, maximum in (("timeout_seconds", 300), ("max_bytes", file_limit), ("not_used_since", 2**53)):
        if key in args and args[key] is not None and (type(args[key]) is not int or not 0 <= args[key] <= maximum):
            raise ProtocolError("沙盒数值参数不正确。")
    if "allow_large" in args and type(args["allow_large"]) is not bool:
        raise ProtocolError("沙盒写入选项不正确。")
    if "packages" in args and args["packages"] is not None:
        packages = args["packages"]
        if not isinstance(packages, list) or len(packages) > 16 or any(
            not isinstance(item, str) or len(item) > 128 or not NIX_ATTRIBUTE_PATTERN.fullmatch(item) for item in packages
        ):
            raise ProtocolError("沙盒软件包参数不正确。")
    return method, args


def fingerprint(request: dict, payload: bytes) -> str:
    value = {"method": request["method"], "args": request["args"], "payload_sha256": hashlib.sha256(payload).hexdigest()}
    return hashlib.sha256(encoded(value)).hexdigest()
