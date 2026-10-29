import json,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from prometheus_client import CollectorRegistry,generate_latest
from src.plugins.ai_chat.adapters.qq_official.official_metrics import official_metrics,observe

class MetricTests(unittest.TestCase):
 def test_labels_finite_and_scraping_never_changes_events(self):
  registry=CollectorRegistry();metrics=official_metrics(registry)
  self.assertIs(metrics,official_metrics(registry))
  for value in ('SECRET_OPENID','https://private/?token=secret','token'):
   metrics.observe('send',value,mode=value,seconds=.02)
   metrics.observe(value,value)
  output=generate_latest(registry).decode()
  self.assertNotIn('SECRET',output);self.assertNotIn('private',output);self.assertIn('result="other"',output)
  self.assertEqual(output,generate_latest(registry).decode())
 def test_observer_exception_does_not_change_transport(self):
  class Broken:
   def observe(self,*args,**kwargs):raise RuntimeError('SECRET')
  observe(Broken(),'send','accepted')
 def test_unknown_is_not_accepted(self):
  registry=CollectorRegistry();metrics=official_metrics(registry);metrics.observe('send','unknown',mode='passive')
  output=generate_latest(registry).decode();self.assertIn('result="unknown"} 1.0',output);self.assertNotIn('result="accepted"',output)

class WriterTests(unittest.IsolatedAsyncioTestCase):
 async def test_official_and_dual_use_same_real_health_and_shutdown_false(self):
  from src.local_runtime.runtime_health import SDKFreeHealthWriter
  value={'healthy':True,'lease':{'current':True},'gateway':{'state':'ready','message_gap':False}}
  service=SimpleNamespace(snapshot=lambda:value)
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'health.json';writer=SDKFreeHealthWriter(p,lambda:service,legacy_getter=lambda:{'qq':'TEST_LEGACY','connected':True})
   await writer.start();data=json.loads(p.read_text())
   self.assertTrue(data['official']['healthy']);self.assertTrue(data['connected'])
   value['healthy']=False;value['lease']['current']=False;writer.record()
   data=json.loads(p.read_text());self.assertFalse(data['official']['healthy']);self.assertFalse(data['official']['lease_current'])
   await writer.close();data=json.loads(p.read_text());self.assertFalse(data['connected']);self.assertFalse(data['official']['gateway_ready'])
 def test_fresh_process_blocks_nonebot_import_during_writer_lifecycle(self):
  import os,subprocess,textwrap
  root=Path(__file__).resolve().parents[1]
  source=textwrap.dedent("""
   import asyncio,importlib.abc,sys,tempfile
   from pathlib import Path
   class BlockNoneBot(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
     if fullname=='nonebot' or fullname.startswith('nonebot.'):
      raise AssertionError('SDK import forbidden')
   sys.meta_path.insert(0,BlockNoneBot())
   from src.local_runtime.runtime_health import SDKFreeHealthWriter
   async def run():
    with tempfile.TemporaryDirectory() as directory:
     writer=SDKFreeHealthWriter(Path(directory)/'health.json',lambda:None)
     await writer.start();await writer.close()
   asyncio.run(run())
  """)
  result=subprocess.run([sys.executable,'-c',source],cwd=root,env={**os.environ,'PYTHON_DOTENV_DISABLED':'1'},capture_output=True,text=True,timeout=10,check=False)
  self.assertEqual(result.returncode,0,result.stderr)

class GatewayEventTests(unittest.TestCase):
 def test_actual_gateway_state_changes_emit_once_and_sink_failure_is_nonfatal(self):
  from src.qq_official.gateway import GatewayConnection
  registry=CollectorRegistry();metrics=official_metrics(registry)
  connection=GatewayConnection(SimpleNamespace());connection.observer=metrics
  for state in ('connecting','identifying','ready','ready','resuming','ready'):connection.state=state
  output=generate_latest(registry).decode()
  self.assertIn('boundary="gateway",result="ready"} 2.0',output)
  class Broken:
   def observe(self,*args,**kwargs):raise RuntimeError('SECRET')
  connection.observer=Broken();connection.state='closed';self.assertEqual(connection.state,'closed')
