import unittest
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
