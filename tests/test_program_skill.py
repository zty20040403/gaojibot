"""Run the documented persistence helper without a VM or any network."""
import json
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch


class ProgramSkillTests(unittest.TestCase):
    def setUp(self):
        document = (Path(__file__).resolve().parents[1]/'skills/programs.md').read_text()
        self.blocks = re.findall(r'```python\n(.*?)\n```',document,re.S)
        self.assertEqual(len(self.blocks),4)
        self.client = Mock()
        self.namespace = {}
        with patch.dict(sys.modules,{'sandbox_gaoji':SimpleNamespace(Client=lambda:self.client)}),patch.object(sys,'path',list(sys.path)):
            exec(compile(self.blocks[0],'programs.md:helper','exec'),self.namespace)
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)/'state.json'

    def test_all_example_scripts_compile(self):
        for index,block in enumerate(self.blocks):
            compile(block,f'programs.md:example{index}','exec')

    def test_unknown_write_persists_id_and_only_queries_status_after_restart(self):
        self.client.request.return_value = 502,{'error':'TEST_INTERRUPTED'}
        with patch.dict('os.environ',{'GAOJI_PROGRAM':'TEST_PROGRAM'}),self.assertRaisesRegex(RuntimeError,'保留 pending'):
            self.namespace['announce'](self.path,{'cursor':123},'TEST_TEXT')
        saved = json.loads(self.path.read_text())
        identity = saved['pending']['request_id']
        self.assertEqual(saved['cursor'],123)
        self.client.request.return_value = 200,{'state':'ambiguous'}
        with self.assertRaisesRegex(RuntimeError,'尚未确认'):
            self.namespace['load'](self.path)
        self.assertEqual(json.loads(self.path.read_text())['pending']['request_id'],identity)
        self.client.request.return_value = 200,{'state':'completed'}
        self.assertEqual(self.namespace['load'](self.path),{'cursor':123})
        methods = [call.args[0] for call in self.client.request.call_args_list]
        self.assertEqual(methods,['POST','GET','GET'])
        self.assertIn(identity,self.client.request.call_args.args[1])
        self.assertEqual(list(self.path.parent.iterdir()),[self.path])

    def test_accepted_is_not_completed_and_success_preserves_seen_position(self):
        with patch.dict('os.environ',{'GAOJI_PROGRAM':'TEST_PROGRAM'}):
            self.client.request.return_value = 202,{'state':'pending'}
            with self.assertRaises(RuntimeError):
                self.namespace['announce'](self.path,{'seen':'TEST_VERSION'},'TEST_WAIT')
            self.assertIn('pending',json.loads(self.path.read_text()))
            self.client.request.return_value = 200,{'state':'completed'}
            self.namespace['load'](self.path)
            self.namespace['announce'](self.path,{'seen':'TEST_NEXT'},'TEST_DONE')
        self.assertEqual(json.loads(self.path.read_text()),{'seen':'TEST_NEXT'})
