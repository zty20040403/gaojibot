"""Per-conversation self directory (/work/.gaoji/self/) with fast host mirror & git sync.

Stores per-group impression cards (people/*.md), promises (promises.md), and
stances (stances.md) without adding any database tables. Reads come from a fast
per-scope mirror under AI_STATE_DIR so cold/sleeping containers are never woken
for normal chat turns; writes sync and commit into `/work/.gaoji/self/` git repo.
Corrupted or missing files always degrade to empty so chat never fails.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import time

from .stances import MAX_STANCES_BYTES
from .tools.memory import looks_like_secret

MAX_CARD_CHARS = 600
MAX_PERSON_CARDS_IN_TURN = 6
MAX_PROMISES_IN_TURN = 5

_SAFE_SLUG_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff_-]+")
_ACTOR_MARKER_RE = re.compile(r"<!--\s*actor:([^\s>]+)\s*-->")
_HEADER_NAME_RE = re.compile(r"^#\s*(.+?)\s*$", re.MULTILINE)


def scope_digest(scope_key: str) -> str:
    return hashlib.sha256(scope_key.encode("utf-8")).hexdigest()[:24]


def person_filename(actor_key: str, display: str) -> str:
    slug = _SAFE_SLUG_RE.sub("_", (display or "").strip()).strip("_-")[:24] or "member"
    suffix = hashlib.sha256(actor_key.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{suffix}.md"


def parse_actor_marker(text: str) -> str:
    match = _ACTOR_MARKER_RE.search(text or "")
    return match.group(1).strip() if match else ""


def parse_card_name(text: str) -> str:
    match = _HEADER_NAME_RE.search(text or "")
    return match.group(1).strip() if match else ""


@dataclass(frozen=True)
class PromiseItem:
    promise_id: str
    due_at: int
    due_iso: str
    who: str
    what: str
    done: bool = False
    unit_name: str = ""

    def format_line(self) -> str:
        mark = "x" if self.done else " "
        unit = f" | unit:{self.unit_name}" if self.unit_name else ""
        return f"- [{mark}] `{self.promise_id}` | due:{self.due_at} ({self.due_iso}) | {self.who or '群友'} | {self.what}{unit}"


_PROMISE_LINE_RE = re.compile(
    r"^-\s*\[([ xX])\]\s*`([a-z0-9_-]+)`\s*\|\s*due:(\d+)\s*\(([^)]*)\)\s*\|\s*([^|]*?)\s*\|\s*(.+?)(?:\s*\|\s*unit:([^\s|]+))?\s*$"
)


def parse_promises(text: str) -> list[PromiseItem]:
    items: list[PromiseItem] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _PROMISE_LINE_RE.match(line)
        if not match:
            continue
        items.append(
            PromiseItem(
                promise_id=match.group(2),
                due_at=int(match.group(3)),
                due_iso=match.group(4).strip(),
                who=match.group(5).strip(),
                what=match.group(6).strip(),
                done=match.group(1).lower() == "x",
                unit_name=(match.group(7) or "").strip(),
            )
        )
    return items


def render_promises(items: list[PromiseItem]) -> str:
    header = "# 承诺清单\n\n"
    return header + "\n".join(item.format_line() for item in items) + ("\n" if items else "")


_CONTAINER_SYNC_SCRIPT = """
import base64, json, os, subprocess, sys, time
from pathlib import Path

payload = json.loads(sys.stdin.read())
root = Path('/work/.gaoji/self')
deadline = time.monotonic() + 25

def git(*args, allowed=(0,)):
    try:
        rc = subprocess.run(['git', '-C', str(root), *args], timeout=max(0, deadline - time.monotonic())).returncode
    except subprocess.TimeoutExpired:
        sys.exit('git ' + args[0] + ' timed out')
    if rc not in allowed:
        sys.exit('git ' + args[0] + ' failed')
    return rc

(root / 'people').mkdir(parents=True, exist_ok=True)
if not (root / '.git').exists():
    git('init', '-q')
    git('config', 'user.name', 'gaoji')
    git('config', 'user.email', 'gaoji@localhost')

for rel, b64 in (payload.get('writes') or {}).items():
    p = (root / rel).resolve()
    if root.resolve() not in p.parents:
        continue
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name('.' + p.name + '.tmp')
    tmp.write_bytes(base64.b64decode(b64))
    os.replace(tmp, p)

for rel in (payload.get('deletes') or []):
    p = (root / rel).resolve()
    if root.resolve() not in p.parents:
        continue
    if p.is_file():
        p.unlink()

msg = payload.get('message') or 'update self'
git('add', '-A')
if git('diff', '--cached', '--quiet', allowed=(0, 1)) == 1:
    git('commit', '-q', '-m', msg)
"""


class SelfFilesStore:
    """Thread-safe host mirror + container git sync for per-group self files."""

    def __init__(self, state_dir: str | Path, *, logger=None) -> None:
        self.base_dir = Path(state_dir) / "self"
        self.logger = logger
        self._bg_tasks: set[asyncio.Task] = set()

    def scope_dir(self, scope_key: str) -> Path:
        return self.base_dir / scope_digest(scope_key)

    def _safe_read(self, path: Path) -> str:
        try:
            if not path.is_file() or path.is_symlink():
                return ""
            return path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""

    def _atomic_write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)

    # --- People impression cards ---

    def _find_person_file(self, scope_key: str, *, actor_key: str = "", name: str = "") -> Path | None:
        people_dir = self.scope_dir(scope_key) / "people"
        if not people_dir.is_dir():
            return None
        needle_actor = actor_key.strip()
        needle_name = name.strip().casefold()
        for entry in sorted(people_dir.iterdir(), key=lambda p: p.name):
            if not entry.is_file() or entry.suffix != ".md":
                continue
            text = self._safe_read(entry)
            if not text:
                continue
            marker = parse_actor_marker(text)
            if needle_actor and marker == needle_actor:
                return entry
            header = parse_card_name(text).casefold()
            if needle_name and (header == needle_name or entry.stem.casefold().startswith(needle_name + "-")):
                return entry
        return None

    def read_person(self, scope_key: str, *, actor_key: str = "", name: str = "") -> str:
        path = self._find_person_file(scope_key, actor_key=actor_key, name=name)
        if path is None:
            return ""
        text = self._safe_read(path)
        return "\n".join(line for line in text.splitlines() if not _ACTOR_MARKER_RE.fullmatch(line.strip())).strip()

    def write_person(self, scope_key: str, *, actor_key: str, display: str, body: str) -> tuple[str, str]:
        clean_body = (body or "").strip()[:MAX_CARD_CHARS]
        if not clean_body:
            raise ValueError("印象内容不能为空。")
        if looks_like_secret(clean_body):
            raise ValueError("印象卡不能保存密码、密钥或验证码。")
        existing = self._find_person_file(scope_key, actor_key=actor_key)
        people_dir = self.scope_dir(scope_key) / "people"
        target = existing or (people_dir / person_filename(actor_key, display))
        clean_display = " ".join((display or "群友").split())[:40] or "群友"
        rendered = f"# {clean_display}\n<!-- actor:{actor_key} -->\n{clean_body}\n"
        self._atomic_write(target, rendered)
        rel = f"people/{target.name}"
        return rel, rendered

    def forget_person(self, scope_key: str, *, actor_key: str = "", name: str = "") -> str | None:
        path = self._find_person_file(scope_key, actor_key=actor_key, name=name)
        if path is None:
            return None
        rel = f"people/{path.name}"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return None
        return rel

    def select_impressions_for_batch(
        self,
        scope_key: str,
        speakers: list[tuple[str, str]],
        *,
        max_cards: int = MAX_PERSON_CARDS_IN_TURN,
    ) -> list[str]:
        """Load impression cards ONLY for speakers appearing in the current batch."""
        if not speakers:
            return []
        selected: list[str] = []
        seen_paths: set[Path] = set()
        for actor_key, display in speakers:
            path = self._find_person_file(scope_key, actor_key=actor_key, name=display)
            if path is None or path in seen_paths:
                continue
            text = self._safe_read(path)
            if not text:
                continue
            cleaned = "\n".join(
                line for line in text.splitlines() if not _ACTOR_MARKER_RE.fullmatch(line.strip())
            ).strip()
            if not cleaned:
                continue
            seen_paths.add(path)
            selected.append(cleaned)
            if len(selected) >= max_cards:
                break
        return selected

    def list_people_summary(self, scope_key: str) -> list[dict[str, str]]:
        people_dir = self.scope_dir(scope_key) / "people"
        if not people_dir.is_dir():
            return []
        result: list[dict[str, str]] = []
        for entry in sorted(people_dir.iterdir(), key=lambda p: p.name):
            if not entry.is_file() or entry.suffix != ".md":
                continue
            text = self._safe_read(entry)
            if not text:
                continue
            result.append({
                "file": entry.name,
                "actor_key": parse_actor_marker(text),
                "name": parse_card_name(text) or entry.stem,
                "card": text[:MAX_CARD_CHARS],
            })
        return result

    # --- Consolidation watermark metadata ---

    def meta(self, scope_key: str) -> dict[str, object]:
        path = self.scope_dir(scope_key) / ".meta.json"
        raw = self._safe_read(path)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def save_meta(self, scope_key: str, data: dict[str, object]) -> None:
        path = self.scope_dir(scope_key) / ".meta.json"
        self._atomic_write(path, json.dumps(data, ensure_ascii=False))

    # --- Promises & Stances helpers ---

    def read_promises(self, scope_key: str) -> list[PromiseItem]:
        raw = self._safe_read(self.scope_dir(scope_key) / "promises.md")
        return parse_promises(raw)

    def write_promises(self, scope_key: str, items: list[PromiseItem]) -> tuple[str, str]:
        rendered = render_promises(items)
        self._atomic_write(self.scope_dir(scope_key) / "promises.md", rendered)
        return "promises.md", rendered

    def relevant_promises(
        self,
        scope_key: str,
        *,
        query_text: str = "",
        now: int | None = None,
        limit: int = MAX_PROMISES_IN_TURN,
    ) -> list[PromiseItem]:
        current = int(time.time()) if now is None else int(now)
        open_items = [item for item in self.read_promises(scope_key) if not item.done]
        if not open_items:
            return []
        tokens = {tok for tok in re.split(r"\s+", query_text.strip().casefold()) if len(tok) >= 2}
        scored: list[tuple[int, int, PromiseItem]] = []
        for item in open_items:
            due_soon = 0 if item.due_at <= current + 86400 else 1
            topic_hit = 0
            hay = f"{item.who} {item.what}".casefold()
            if tokens and any(tok in hay for tok in tokens):
                topic_hit = -1
            if due_soon == 0 or topic_hit < 0 or item.due_at <= current:
                scored.append((topic_hit, item.due_at, item))
        scored.sort(key=lambda entry: (entry[0], entry[1]))
        return [entry[2] for entry in scored[:limit]]

    def read_stances(self, scope_key: str) -> str:
        path = self.scope_dir(scope_key) / "stances.md"
        try:
            if path.is_symlink() or path.stat().st_size > MAX_STANCES_BYTES:
                return ""
        except OSError:
            return ""
        return self._safe_read(path)

    def write_stances(self, scope_key: str, text: str) -> tuple[str, str]:
        content = text.strip() + "\n" if text.strip() else ""
        if len(content.encode("utf-8")) > MAX_STANCES_BYTES:
            raise ValueError("立场文件超过大小限制。")
        self._atomic_write(self.scope_dir(scope_key) / "stances.md", content)
        return "stances.md", content

    # --- Container git sync ---

    async def sync_to_container(
        self,
        sandbox_manager,
        owner: str,
        *,
        writes: dict[str, str] | None = None,
        deletes: list[str] | None = None,
        commit_message: str = "update self",
        ensure_started: bool = False,
    ) -> bool:
        if sandbox_manager is None or not owner:
            return False
        try:
            sandboxes = await sandbox_manager.list_owned(owner)
            default_sb = next((sb for sb in sandboxes if sb.get("purpose") == "conversation"), None)
            if default_sb is None:
                if not ensure_started:
                    return False
                default_sb = await sandbox_manager.ensure_default(owner)
            sid = str(default_sb["id"])
            if not ensure_started and not await sandbox_manager._running(sid):
                return False
            if ensure_started and not await sandbox_manager._running(sid):
                await sandbox_manager.start(owner, sid)
            payload = {
                "writes": {
                    rel: base64.b64encode(content.encode("utf-8")).decode("ascii")
                    for rel, content in (writes or {}).items()
                },
                "deletes": list(deletes or ()),
                "message": commit_message,
            }
            code, _, err = await sandbox_manager._guest_exec_raw(
                sid,
                "/run/current-system/sw/bin/python3",
                ["-c", _CONTAINER_SYNC_SCRIPT],
                timeout=30,
                input_bytes=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            )
            if code != 0 and self.logger is not None:
                self.logger.warning("self git sync failed sid=%s err=%s", sid, err.decode("utf-8", "replace")[:200])
            return code == 0
        except Exception as exc:
            if self.logger is not None:
                self.logger.warning("self git sync skipped owner=%s error=%s", owner, type(exc).__name__)
            return False

    def schedule_sync(
        self,
        sandbox_manager,
        owner: str,
        *,
        writes: dict[str, str] | None = None,
        deletes: list[str] | None = None,
        commit_message: str = "update self",
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(
            self.sync_to_container(
                sandbox_manager,
                owner,
                writes=writes,
                deletes=deletes,
                commit_message=commit_message,
                ensure_started=False,
            )
        )
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
