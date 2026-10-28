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
BROWSER_CACHE_DIRS = frozenset(
    f".gaoji/browser/{profile}{cache}"
    for profile in ("", "Default/")
    for cache in ("Cache", "Code Cache", "GPUCache")
)


def rebuild_spec(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"command", "locks", "excluded"}
        or not isinstance(value["command"], str)
        or not 1 <= len(value["command"]) <= 4000
        or any(ord(c) < 32 for c in value["command"])
        or not isinstance(value["locks"], list)
        or not 2 <= len(value["locks"]) <= 8
        or not isinstance(value["excluded"], list)
        or len(value["excluded"]) > 512
    ):
        raise ValueError("Invalid project rebuild specification")
    locked = []
    for item in value["locks"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "sha256"}
            or not isinstance(item["path"], str)
            or project_path(item["path"]) != item["path"]
            or not isinstance(item["sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])
        ):
            raise ValueError("Invalid rebuild input")
        locked.append(item["path"])
    if locked != sorted(set(locked)):
        raise ValueError("Rebuild inputs must be unique and sorted")
    paths = value["excluded"]
    for path in paths:
        if not isinstance(path, str) or project_path(path) != path:
            raise ValueError("Invalid rebuildable path")
        if (
            ".git" in path.split("/")
            or path.split("/")[0] == "lost+found"
            or any(p == path or p.startswith(path + "/") for p in locked)
            or path == ".gaoji"
            or (
                path.startswith(".gaoji/")
                and path not in BROWSER_CACHE_DIRS
            )
        ):
            raise ValueError("Rebuildable path hides retained metadata")
    if paths != sorted(set(paths)) or len({p.casefold() for p in paths}) != len(paths):
        raise ValueError("Rebuildable paths must be unique and sorted")
    if any(any(p.startswith(q + "/") for q in paths if p != q) for p in paths):
        raise ValueError("Rebuildable roots must not overlap")
    return value


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


def retained_manifest(files, *, rebuild=None, browser_cache=None):
    document = {"format": 1, "files": files}
    if rebuild is not None:
        document.update(format=2, rebuild=rebuild)
    if browser_cache is not None:
        document.update(format=3, rebuild=rebuild, browser_cache=browser_cache)
    return document


def excluded_paths(manifest):
    spec = manifest.get("rebuild")
    return sorted((spec["excluded"] if spec else []) + manifest.get("browser_cache", []))


def manifest_bytes(manifest, *, quota_bytes):
    if (
        not isinstance(manifest, dict)
        or set(manifest)
        != (
            {"format", "files", "rebuild", "browser_cache"}
            if manifest.get("format") == 3
            else {"format", "files", "rebuild"}
            if manifest.get("format") == 2
            else {"format", "files"}
        )
        or type(manifest["format"]) is not int
        or manifest["format"] not in (1, 2, 3)
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
    if manifest["format"] == 3:
        caches = manifest["browser_cache"]
        if (not isinstance(caches, list) or len(caches) > len(BROWSER_CACHE_DIRS)
                or any(not isinstance(path, str) or path not in BROWSER_CACHE_DIRS for path in caches)
                or caches != sorted(set(caches))):
            raise ValueError("Invalid browser cache roots")
    if manifest["format"] == 2 or manifest.get("rebuild") is not None:
        spec = rebuild_spec(manifest["rebuild"])
        files = {item["path"]: item["sha256"] for item in manifest["files"]}
        if any(files.get(item["path"]) != item["sha256"] for item in spec["locks"]):
            raise ValueError("Rebuild inputs are not retained at their pinned hashes")
    excluded = excluded_paths(manifest)
    if len(excluded) != len(set(excluded)) or any(
        p.startswith(q + "/") for p in excluded for q in excluded if p != q
    ):
        raise ValueError("Rebuildable and browser cache roots must not overlap")
    if any(
        item["path"] == root or item["path"].startswith(root + "/")
        for item in manifest["files"] for root in excluded
    ):
        raise ValueError("Rebuildable data cannot also be retained")
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
