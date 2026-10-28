"""Exercise real imports and NoneBot loading in fresh interpreters, never stubs."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_MODULES = (
    "conversation_scope", "message_ir", "message_lowering", "tasks",
    "platform_types", "adapters.platform_types", "application.chat_orchestrator",
    "application.commands",
)


class PlatformImportBoundaryTests(unittest.TestCase):
    def run_clean(self, source: str) -> subprocess.CompletedProcess[str]:
        with TemporaryDirectory() as directory:
            state = Path(directory) / "never-created"
            result = subprocess.run(
                [sys.executable, "-c", source], cwd=ROOT, capture_output=True, text=True,
                timeout=45, env={
                    **os.environ, "PYTHON_DOTENV_DISABLED": "1",
                    "AI_POSTGRES_DSN": "", "AI_STATE_DIR": str(state),
                    "AI_CACHE_DIR": str(state / "cache"),
                },
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(state.exists(), "import or plugin registration created runtime state")
            return result

    def test_public_imports_without_framework_database_network_or_runtime(self):
        source = '''
import importlib
import importlib.abc
import sys
import threading

class RejectPlatformSDK(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "nonebot" or fullname.startswith("nonebot."):
            raise AssertionError("public imports reached platform SDK: " + fullname)
        return None

sys.meta_path.insert(0, RejectPlatformSDK())

def audit(event, args):
    if event in {"socket.connect", "sqlite3.connect"}:
        raise AssertionError("public import attempted runtime I/O: " + event)
sys.addaudithook(audit)
before_threads = set(threading.enumerate())
for module in MODULES:
    importlib.import_module("src.plugins.ai_chat." + module)
import src.plugins.ai_chat as package
from src.plugins.ai_chat.platform_types import AccountKey
from src.plugins.ai_chat.adapters.platform_types import AccountKey as AdapterAccountKey
assert AccountKey is AdapterAccountKey
assert not hasattr(package, "_runtime")
assert not hasattr(package, "driver")
assert not hasattr(package, "__plugin__")
assert "src.plugins.ai_chat.onebot_plugin" not in sys.modules
assert "src.plugins.ai_chat.matcher_handlers" not in sys.modules
assert "src.plugins.ai_chat.onebot_runtime" not in sys.modules
assert not any(name == "nonebot" or name.startswith("nonebot.") for name in sys.modules)
assert set(threading.enumerate()) == before_threads
print("neutral imports verified")
'''.replace("MODULES", repr(PUBLIC_MODULES))
        self.assertIn("neutral imports verified", self.run_clean(source).stdout)

    def test_real_nonebot_loader_after_public_imports_registers_original_handlers_once(self):
        source = '''
import asyncio
import importlib
import sys
import tomllib
from pathlib import Path
from unittest.mock import AsyncMock, patch
configured_plugins = tomllib.loads(Path("pyproject.toml").read_text())["tool"]["nonebot"]["plugins"]
assert configured_plugins == ["src.plugins.ai_chat.onebot_plugin"]
for module in MODULES:
    importlib.import_module("src.plugins.ai_chat." + module)
assert "nonebot" not in sys.modules
import nonebot
from nonebot.adapters.onebot.v11 import Adapter
from nonebot.matcher import matchers
from nonebot.message import _event_preprocessors
nonebot.init(_env_file=())
driver = nonebot.get_driver()
driver.register_adapter(Adapter)
startup_before = list(driver._lifespan._startup_funcs)
shutdown_before = list(driver._lifespan._shutdown_funcs)
assert not any(matchers.values())
assert not _event_preprocessors
with patch("sqlite3.connect", side_effect=AssertionError("registration opened SQLite")), patch(
    "src.bot_storage.PostgresDatabase.__init__", side_effect=AssertionError("registration opened PostgreSQL")
):
    loaded = nonebot.load_plugin(configured_plugins[0])
assert loaded is not None
plugin = loaded.module
assert loaded.module_name == "src.plugins.ai_chat.onebot_plugin"
assert len(loaded.matcher) == 34
assert sum(len(items) for items in matchers.values()) == 34
assert all(len(matcher.handlers) == 1 for matcher in loaded.matcher)
assert len(_event_preprocessors) == 1
assert next(iter(_event_preprocessors)).call is plugin.ignore_disabled_group_event
assert driver._lifespan._startup_funcs == [*startup_before, plugin.startup_app_context]
assert driver._lifespan._shutdown_funcs == [*shutdown_before, plugin.shutdown_app_context]
assert not plugin._runtime.ready
assert not plugin._runtime.state_dir.exists()
assert not plugin._runtime.cache_dir.exists()
again = importlib.import_module("src.plugins.ai_chat.onebot_plugin")
assert again is plugin
try:
    nonebot.load_plugin("src.plugins.ai_chat.onebot_plugin")
except RuntimeError as error:
    assert "Plugin already exists" in str(error)
else:
    raise AssertionError("NoneBot duplicate-plugin guard was bypassed")
assert sum(len(items) for items in matchers.values()) == 34
assert len(_event_preprocessors) == 1

async def check_lifecycle_delegation():
    runtime = plugin._runtime
    with patch.object(runtime, "start", new_callable=AsyncMock) as start, patch.object(
        runtime, "shutdown", new_callable=AsyncMock
    ) as shutdown, patch.object(runtime, "preprocess", new_callable=AsyncMock) as preprocess:
        await plugin.startup_app_context()
        start.assert_awaited_once_with(nonebot.get_app())
        bot, event = object(), object()
        await plugin.ignore_disabled_group_event(bot, event)
        preprocess.assert_awaited_once_with(bot, event)
        await plugin.shutdown_app_context()
        shutdown.assert_awaited_once_with()
asyncio.run(check_lifecycle_delegation())
print("real plugin lifecycle verified")
'''.replace("MODULES", repr(PUBLIC_MODULES))
        result = self.run_clean(source)
        self.assertIn("real plugin lifecycle verified", result.stdout)
        self.assertNotIn("Failed to import", result.stdout + result.stderr)

    def test_shared_package_import_after_nonebot_init_does_not_register_plugin(self):
        source = '''
import nonebot
from nonebot.matcher import matchers
from nonebot.message import _event_preprocessors
nonebot.init(_env_file=())
driver = nonebot.get_driver()
import src.plugins.ai_chat as package
import src.plugins.ai_chat.adapters
assert not hasattr(package, "_runtime")
assert not any(matchers.values())
assert not _event_preprocessors
assert not driver._lifespan._startup_funcs
assert not driver._lifespan._shutdown_funcs
print("no implicit registration verified")
'''
        self.assertIn("no implicit registration verified", self.run_clean(source).stdout)
