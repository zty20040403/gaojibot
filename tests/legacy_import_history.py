"""Exercise the published importer without restoring it to the host package."""
from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory

from tests import test_qq_official_outbox_migration as migration

REVISION = "23f452c9c970956207a5b6d46022cddf6731eb41"
ROOT = Path(__file__).resolve().parents[1]


class HistoricalImport:
    @classmethod
    def setUpClass(cls):
        (ROOT / ".cache").mkdir(exist_ok=True)
        temporary = TemporaryDirectory(dir=ROOT / ".cache")
        cls.addClassCleanup(temporary.cleanup)
        cls.history = Path(temporary.name)
        archive = subprocess.check_output(
            ["git", "archive", REVISION, "src/__init__.py", "src/bot_storage",
             "migrations", "alembic.ini"], cwd=ROOT,
        )
        with tarfile.open(fileobj=io.BytesIO(archive)) as source:
            source.extractall(cls.history, filter="data")
        name = "src.bot_storage._historical_import"
        spec = importlib.util.spec_from_file_location(
            name, cls.history / "src/bot_storage/legacy_migration.py",
        )
        cls.legacy = importlib.util.module_from_spec(spec)
        sys.modules[name] = cls.legacy
        cls.addClassCleanup(sys.modules.pop, name)
        spec.loader.exec_module(cls.legacy)

    def cli(self, root, schema, *args, expected=0):
        env = {**os.environ, "AI_POSTGRES_DSN": migration.checked_test_dsn(),
               "AI_POSTGRES_SCHEMA": schema, "PYTHON_DOTENV_DISABLED": "1",
               "PYTHONPATH": "", "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run(
            [sys.executable, "-m", "src.bot_storage.cli", *args], cwd=root,
            env=env, text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result.stdout + result.stderr if expected else result.stdout
