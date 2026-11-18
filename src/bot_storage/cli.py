from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

from .database import PostgresDatabase
from .media_cleanup import LegacyMediaCleanup
from .schema import HEAD_REVISION


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gaoji-db",
        description="Manage the bot PostgreSQL schema and media cleanup.",
        epilog="Historical SQLite/JSON imports: docs/postgresql-migration.md",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("upgrade", help="upgrade PostgreSQL to the schema head")
    subcommands.add_parser("current", help="show the current Alembic revision")
    subcommands.add_parser("check", help="check connectivity and schema revision")

    cleanup_parser = subcommands.add_parser(
        "cleanup-legacy-media",
        help="preview or explicitly remove inert ordinary-image media",
    )
    cleanup_parser.add_argument("--apply", action="store_true")
    cleanup_parser.add_argument("--confirm", default="")
    cleanup_parser.add_argument("--report", type=Path)

    args = parser.parse_args(argv)
    try:
        if args.command == "upgrade":
            command.upgrade(_alembic_config(), "head")
            return 0
        if args.command == "current":
            command.current(_alembic_config(), verbose=True)
            return 0
        if args.command == "check":
            database = PostgresDatabase(_dsn(), schema=_schema())
            try:
                database.healthcheck()
                database.require_revision(HEAD_REVISION)
            finally:
                database.close()
            print(f"PostgreSQL ready: schema={_schema()} revision={HEAD_REVISION}")
            return 0
        if args.command == "cleanup-legacy-media":
            database = PostgresDatabase(_dsn(), schema=_schema())
            try:
                database.require_revision(HEAD_REVISION)
                cleanup = LegacyMediaCleanup(
                    database,
                    media_root=_required_path("AI_MEDIA_ROOT"),
                    archive_root=_optional_path("AI_ARCHIVE_ROOT"),
                )
                report = (
                    cleanup.apply(args.confirm)
                    if args.apply
                    else cleanup.preview()
                )
            finally:
                database.close()
            _emit_report(report, args.report)
            return 0

    except Exception as exc:
        print(f"database command failed: {exc}", file=sys.stderr)
        return 1


def _alembic_config() -> Config:
    return Config(str(PROJECT_ROOT / "alembic.ini"))


def _dsn() -> str:
    value = os.getenv("AI_POSTGRES_DSN", "").strip()
    if not value:
        raise RuntimeError("AI_POSTGRES_DSN is required")
    return value


def _schema() -> str:
    return os.getenv("AI_POSTGRES_SCHEMA", "qq_bot").strip() or "qq_bot"


def _required_path(name: str) -> Path:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return Path(value)


def _optional_path(name: str) -> Path | None:
    value = os.getenv(name, "").strip()
    return Path(value) if value else None


def _emit_report(report: dict[str, object], path: Path | None) -> None:
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(encoded)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(encoded + "\n", encoding="utf-8")
        temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
