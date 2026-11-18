from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .build_version import build_commit
from .handler_constants import BOT_VERSION


MAX_READ_LINES = 240
MAX_SEARCH_MATCHES = 100
ALLOWED_TOP_LEVEL_FILES = {
    ".env.example",
    "README.md",
    "bot.py",
    "pyproject.toml",
    "requirements.txt",
}
ALLOWED_DIRECTORIES = {"docs", "nix", "src", "tests", "skills", "tools"}
ALLOWED_SUFFIXES = {".html", ".md", ".nix", ".py", ".sh", ".toml", ".txt"}


@dataclass(frozen=True)
class SourceMatch:
    path: str
    line: int
    text: str


class SelfSource:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def paths(self, prefix: str = "", *, limit: int = 200) -> tuple[list[str], bool]:
        normalized = prefix.strip().lstrip("./")
        candidates = [
            path
            for path in self._source_paths()
            if not normalized or path.startswith(normalized)
        ]
        bounded = min(max(int(limit), 1), 1000)
        return candidates[:bounded], len(candidates) > bounded

    def read(
        self,
        path: str,
        *,
        start_line: int = 1,
        end_line: int = 120,
    ) -> dict[str, object]:
        relative, target = self._resolve_allowed(path)
        start = max(int(start_line), 1)
        end = min(max(int(end_line), start), start + MAX_READ_LINES - 1)
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        actual_end = min(end, len(lines))
        numbered = "\n".join(
            f"{number} | {lines[number - 1]}"
            for number in range(start, actual_end + 1)
        )
        return {
            "path": relative,
            "start_line": start,
            "end_line": actual_end,
            "total_lines": len(lines),
            "content": numbered,
        }

    def search(
        self,
        query: str,
        *,
        path_prefix: str = "",
        limit: int = 20,
    ) -> list[SourceMatch]:
        needle = query.strip()
        if not needle:
            raise ValueError("源码搜索词不能为空。")
        bounded = min(max(int(limit), 1), MAX_SEARCH_MATCHES)
        paths, _truncated = self.paths(path_prefix, limit=1000)
        matches: list[SourceMatch] = []
        for relative in paths:
            target = self.root / relative
            for number, line in enumerate(
                target.read_text(encoding="utf-8", errors="replace").splitlines(),
                start=1,
            ):
                if needle.casefold() not in line.casefold():
                    continue
                matches.append(
                    SourceMatch(
                        path=relative,
                        line=number,
                        text=line.strip()[:500],
                    )
                )
                if len(matches) >= bounded:
                    return matches
        return matches

    def identity(self) -> dict[str, object]:
        digest = hashlib.sha256()
        total_bytes = 0
        paths = self._source_paths()
        for relative in paths:
            payload = (self.root / relative).read_bytes()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(payload)
            total_bytes += len(payload)
        return {
            "version": BOT_VERSION,
            "commit": build_commit(),
            "sha256": digest.hexdigest(),
            "file_count": len(paths),
            "byte_count": total_bytes,
        }

    def _source_paths(self) -> list[str]:
        paths: list[str] = []
        for path in self.root.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(self.root).as_posix()
            if self._is_allowed_relative(relative):
                paths.append(relative)
        return sorted(paths)

    def _resolve_allowed(self, path: str) -> tuple[str, Path]:
        raw = path.strip().replace("\\", "/")
        if not raw or raw.startswith("/"):
            raise ValueError("必须提供仓库内的相对路径。")
        lexical = Path(raw)
        if any(part in {"", ".", ".."} for part in lexical.parts):
            raise ValueError("源码路径不能包含目录跳转。")
        candidate = self.root / lexical
        current = self.root
        for part in lexical.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("不能通过符号链接读取源码。")
        target = candidate.resolve()
        try:
            relative = target.relative_to(self.root).as_posix()
        except ValueError as exc:
            raise ValueError("不能读取仓库外路径。") from exc
        if not target.is_file() or target.is_symlink():
            raise ValueError("源码文件不存在。")
        if not self._is_allowed_relative(relative):
            raise ValueError("这个文件不在自查源码白名单中。")
        return relative, target

    @staticmethod
    def _is_allowed_relative(relative: str) -> bool:
        path = Path(relative)
        if relative in ALLOWED_TOP_LEVEL_FILES:
            return True
        if not path.parts or path.parts[0] not in ALLOWED_DIRECTORIES:
            return False
        if any(part.startswith(".") or part == "__pycache__" for part in path.parts):
            return False
        return path.suffix.lower() in ALLOWED_SUFFIXES
