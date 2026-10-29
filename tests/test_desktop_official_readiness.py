import json
import tempfile
import time
import unittest
from pathlib import Path
from src.local_runtime.readiness import connection_status, official_connection

class OfficialReadinessTests(unittest.TestCase):
    def test_strict_actual_heartbeat_and_dual_intersection(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); (root/'state').mkdir()
            path=root/'state/desktop-health.json'
            config={'settings':{'platforms':{'onebot':False,'qq_official':True}, 'bot_qq':''}}
            services=[{'name':'bot','state':'running'}]
            record={'updated_at':time.time(),'official':{'healthy':True,'lease_current':True,'gateway_ready':True}}
            path.write_text(json.dumps(record))
            self.assertEqual(connection_status(root,config,services),'connected')
            self.assertEqual(connection_status(root,config,[]),'not_running')
            for key in record['official']:
                bad={**record,'official':dict(record['official'])};bad['official'][key]=1
                path.write_text(json.dumps(bad));self.assertEqual(official_connection(root),'unavailable')
            path.write_text(json.dumps(record|{'updated_at':float('nan')}));self.assertEqual(official_connection(root),'unavailable')
            path.write_text(json.dumps(record|{'updated_at':time.time()-20}));self.assertEqual(official_connection(root),'stale')
            config['settings']['platforms']['onebot']=True;config['settings']['bot_qq']='12345678'
            path.write_text(json.dumps(record|{'qq':'12345678','connected':False}));self.assertEqual(connection_status(root,config,services),'disconnected')
            path.write_text(json.dumps(record|{'qq':'12345678','connected':True}));self.assertEqual(connection_status(root,config,services),'connected')
