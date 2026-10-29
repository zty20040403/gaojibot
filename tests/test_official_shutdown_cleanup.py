"""Actual official lease cleanup survives failed health/download diagnostics."""
from dataclasses import fields
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import asyncio
import secrets
import unittest
from unittest.mock import AsyncMock,Mock,patch

from src.bot_storage import DatabaseCalls
from src.local_runtime.runtime_health import SDKFreeHealthWriter
from src.plugins.ai_chat.runtime import AppContext
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.adapters.qq_official.config import OfficialConfig
from src.plugins.ai_chat.adapters.qq_official.runtime import OfficialRuntime
from src.plugins.ai_chat.adapters.qq_official.lease import _locally_current
from tests import test_qq_official_runtime as runtime_support
from tests.postgres_fixture import isolated_postgres_for_test


class OfficialShutdownTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.database=isolated_postgres_for_test(self)
  self.ledger=MessageLedger(self.database)
  self.addCleanup(self.ledger.close)
  self.protocol=runtime_support.ControlledProtocol()
  loaded=runtime_support.loaded_config('TEST_SHUTDOWN_'+secrets.token_hex(4))
  self.runtime=OfficialRuntime(OfficialConfig(enabled=True),self.database,logger=Mock(),
      ledger=self.ledger,protocol_factory=self.protocol,probe_seconds=.01)
  self.addAsyncCleanup(self.runtime.stop)
  with patch.object(OfficialConfig,'load',return_value=loaded):await self.runtime.start()
  await self.protocol.next_ready()
  self.token=self.runtime._token
  self.assertTrue(_locally_current(self.token))
  self.calls=DatabaseCalls();self.addAsyncCleanup(self.calls.close)
  self.context=SimpleNamespace(**{f.name:None for f in fields(AppContext)})
  self.context.__dict__.update(_closed=False,_closing=False,logger=Mock(),official_runtime=self.runtime,
      background_tasks=SimpleNamespace(stop_all=AsyncMock(return_value=0)),
      running_tasks=SimpleNamespace(cancel_all_and_wait=AsyncMock(return_value=0)),db_call=self.calls,
      message_ledger=self.ledger,database=self.database)
 async def shutdown(self):
  with patch.object(self.ledger,'close',wraps=self.ledger.close) as ledger,patch.object(self.database,'close',wraps=self.database.close) as database:
   await AppContext.shutdown(self.context)
   ledger.assert_called_once();database.assert_called_once()
  self.assertTrue(self.context._closed);self.assertIsNone(self.context.official_runtime)
  self.assertFalse(_locally_current(self.token));self.assertIsNone(self.runtime._token)
  self.assertTrue(all(g.stopped.is_set() for g in self.protocol.gateways))
  self.context.background_tasks.stop_all.assert_awaited_once()
  self.context.running_tasks.cancel_all_and_wait.assert_awaited_once()
 async def test_real_writer_path_becomes_directory_and_real_official_lease_still_closes(self):
  with TemporaryDirectory() as directory:
   path=Path(directory)/'private-health.json';writer=SDKFreeHealthWriter(path,lambda:None)
   await writer.start();path.unlink();path.mkdir()
   self.context.desktop_health_writer=writer
   self.context.official_observability=SimpleNamespace(close=AsyncMock(side_effect=RuntimeError('SECRET_TOKEN')))
   await self.shutdown()
   self.assertIsNone(self.context.desktop_health_writer);self.assertIsNone(self.context.official_observability)
   self.assertTrue(writer._closed);self.assertIsNone(writer._task)
   output=str(self.context.logger.mock_calls)
   self.assertNotIn(directory,output);self.assertNotIn('SECRET_TOKEN',output)
 async def test_download_host_cleanup_error_cannot_block_runtime_or_shared_storage(self):
  from src.plugins.ai_chat.adapters.qq_official.download_runtime import DownloadHost
  from src.plugins.ai_chat.adapters.qq_official.host_config import ConfiguredHost
  download=object.__new__(DownloadHost)
  download._lock=asyncio.Lock();download._closed=False;download._ready_until=10;download._service=object();download._task=None
  download._runner=SimpleNamespace(cleanup=AsyncMock(side_effect=OSError('SECRET_URL')))
  host=object.__new__(ConfiguredHost);host.download_host=download
  self.context.official_host=host
  await self.shutdown()
  self.assertIsNone(self.context.official_host);self.assertTrue(download._closed);self.assertIsNone(download._service)
  self.assertFalse(download.reachable());self.assertNotIn('SECRET_URL',str(self.context.logger.mock_calls))
 async def test_cancelled_error_remains_control_flow_but_diagnostic_reference_cleared(self):
  self.context.official_observability=SimpleNamespace(close=AsyncMock(side_effect=asyncio.CancelledError))
  with self.assertRaises(asyncio.CancelledError):await AppContext.shutdown(self.context)
  self.assertIsNone(self.context.official_observability)
  # Explicitly resumed shutdown can finish the still-current resources.
  await self.shutdown()
