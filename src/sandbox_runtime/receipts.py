"""Private execution receipts, separate from the Bot's PostgreSQL data.

Only compact mutation results are retained, never uploaded or exported files.
Expired result bodies become tombstones so an old retry cannot execute again.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import stat
import time

from .protocol import HEADER_LIMIT, ProtocolError, encoded


class Receipts:
    def __init__(self, directory: Path):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("Sandbox receipt directory must be private and owned by the runner")
        path = directory / "receipts.sqlite3"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError("Sandbox receipt file must be private")
        finally:
            os.close(fd)
        self.connection = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.connection.execute("PRAGMA journal_mode=DELETE")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL, response TEXT, updated_at INTEGER NOT NULL)")
        self.connection.execute("CREATE INDEX IF NOT EXISTS requests_expiry ON requests(status,updated_at)")
        self.connection.execute("UPDATE requests SET status='uncertain' WHERE status='running'")
        self.expire()

    def expire(self, *, now: int | None = None) -> None:
        cutoff = (int(time.time()) if now is None else now) - 3600
        self.connection.execute("UPDATE requests SET status='expired', response=NULL WHERE status IN ('completed','failed') AND updated_at < ?", (cutoff,))

    def lookup(self, identifier: str, digest: str) -> dict | None:
        self.expire()
        row = self.connection.execute("SELECT fingerprint,status,response FROM requests WHERE id=?", (identifier,)).fetchone()
        if row is None:
            return None
        if row[0] != digest:
            raise ProtocolError("请求编号已用于不同的沙盒操作，已拒绝重复执行。")
        if row[1] in {"uncertain", "running"}:
            raise ProtocolError("上次沙盒操作已开始但结果尚未确认；请先检查沙盒状态，不要直接重做。")
        if row[1] == "expired":
            raise ProtocolError("上次沙盒操作回执已过期，已阻止重复执行；请先检查现状。")
        return json.loads(row[2])

    def begin(self, identifier: str, digest: str) -> None:
        self.connection.execute("INSERT INTO requests VALUES (?,?,'running',NULL,?)", (identifier, digest, int(time.time())))

    def finish(self, identifier: str, response: dict) -> None:
        payload = encoded(response)
        if len(payload) > HEADER_LIMIT:
            raise ProtocolError("沙盒回执过大，执行结果尚未确认。")
        self.connection.execute("UPDATE requests SET status=?,response=?,updated_at=? WHERE id=? AND status='running'",
            ("completed" if response["ok"] else "failed", payload.decode(), int(time.time()), identifier))

    def close(self):
        self.connection.close()
