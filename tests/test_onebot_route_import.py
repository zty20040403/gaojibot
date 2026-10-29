"""Check real imports in clean interpreters, not source-code approximations."""
import subprocess
import sys
import unittest


class RouteImportTests(unittest.TestCase):
    def test_protocol_metadata_does_not_import_onebot_or_nonebot(self):
        subprocess.run([sys.executable, '-c', '''
import sys
from src.plugins.ai_chat.adapters.onebot_route import PhysicalCall, TransportObservation
from src.plugins.ai_chat.adapters.onebot_migration_runtime import SourceJournal
assert not any(name == 'nonebot' or name.startswith('nonebot.') for name in sys.modules)
'''], check=True, capture_output=True, text=True)

    def test_explicit_onebot_adapter_import_returns_real_stable_subclass(self):
        subprocess.run([sys.executable, '-c', '''
from src.plugins.ai_chat.adapters.onebot_route import RouteGuardedOneBotAdapter
from src.plugins.ai_chat.adapters.onebot_route import RouteGuardedOneBotAdapter as again
from nonebot.adapters.onebot.v11 import Adapter
assert RouteGuardedOneBotAdapter is again
assert issubclass(RouteGuardedOneBotAdapter, Adapter)
assert RouteGuardedOneBotAdapter.get_name() == Adapter.get_name()
'''], check=True, capture_output=True, text=True)
