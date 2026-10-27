"""Portable retained-project paths and manifests shared by Bot and private helper."""

import hashlib
import json
from pathlib import PurePosixPath
import re
import unicodedata

MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_VERSION_FILES = 20000
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024


def project_path(value: str) -> str:
    value = unicodedata.normalize("NFC", value)
    parts = value.split("/")
    if (
        not value
        or len(value.encode()) > 1024
        or len(parts) > 64
        or "\\" in value
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
        or any(
            part in {"", ".", ".."}
            or ":" in part
            or len(part.encode()) > 255
            or part.endswith((" ", "."))
            or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)
            for part in parts
        )
    ):
        raise ValueError("Invalid project-relative path")
    return str(PurePosixPath(value))


def manifest_bytes(manifest, *, quota_bytes):
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"format", "files"}
        or type(manifest["format"]) is not int
        or manifest["format"] != 1
        or not isinstance(manifest["files"], list)
        or len(manifest["files"]) > MAX_VERSION_FILES
    ):
        raise ValueError("Invalid retained-file manifest")
    names, total = set(), 0
    for item in manifest["files"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "sha256", "size", "mode"}
            or not isinstance(item["path"], str)
            or project_path(item["path"]) != item["path"]
            or not isinstance(item["sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])
            or type(item["size"]) is not int
            or not 0 <= item["size"] <= MAX_FILE_BYTES
            or type(item["mode"]) is not int
            or item["mode"] not in {0o644, 0o755}
            or item["path"].casefold() in names
        ):
            raise ValueError("Invalid or duplicate retained file")
        names.add(item["path"].casefold())
        total += item["size"]
    if total > quota_bytes:
        raise ValueError("Retained files exceed project capacity")
    for name in names:
        if any(
            "/".join(name.split("/")[:i]) in names
            for i in range(1, len(name.split("/")))
        ):
            raise ValueError("A retained file cannot also be a directory")
    if manifest["files"] != sorted(manifest["files"], key=lambda item: item["path"]):
        raise ValueError("Retained manifest must be sorted")
    result = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    if len(result) > MAX_MANIFEST_BYTES:
        raise ValueError("Retained manifest is too large")
    return result


def manifest_digest(manifest, *, quota_bytes):
    return hashlib.sha256(manifest_bytes(manifest, quota_bytes=quota_bytes)).hexdigest()
