"""Execute only harmless local child processes; never operate host systemd."""
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock,patch

from src.plugins.ai_chat import sandbox_command_process as command

ID = 'a'*32


class SandboxCommandProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root/'state'; self.state.mkdir()
        self.output = self.root/'work'/'.gaoji'/'out'
        self.output.parent.mkdir(parents=True)

    def execute(self, script, **options):
        return command.execute(dict(command_id=ID,program='TEST_PROGRAM',argv=[sys.executable,'-c',script]),
            state=self.state,output=self.output,cwd=str(self.root),identity=(os.getuid(),os.getgid()),**options)

    def test_output_is_visible_while_running_and_job_credentials_are_not_inherited(self):
        gate = self.root/'TEST_GATE'
        script = ("import os,sys,time,pathlib;print('TEST_FIRST',flush=True);end=time.monotonic()+5\n"
            f"while not pathlib.Path({str(gate)!r}).exists() and time.monotonic()<end: time.sleep(.01)\n"
            "print('TEST_LAST',os.environ['GAOJI_PROGRAM'],os.environ.get('GAOJI_JOB_TOKEN','absent'));sys.exit(7)")
        with patch.dict(os.environ,{'GAOJI_JOB_TOKEN':'TEST_SECRET'}),ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.execute,script)
            until = time.monotonic()+4
            path = self.output/(ID+'.txt')
            try:
                while time.monotonic()<until:
                    if path.exists() and 'TEST_FIRST' in path.read_text():
                        break
                    time.sleep(.01)
                self.assertIn('TEST_FIRST',path.read_text())
                self.assertFalse(future.done())
                self.assertFalse((self.state/(ID+'.result')).exists())
            finally:
                gate.touch()
            result = future.result(timeout=6)
        self.assertEqual((result['exit_code'],result['error']),(7,''))
        self.assertIn('TEST_LAST TEST_PROGRAM absent',path.read_text())
        self.assertEqual(json.loads((self.state/(ID+'.result')).read_text()),result)
        self.assertEqual(result['output_bytes'],len(path.read_bytes()))
        before = (self.state/(ID+'.result')).read_bytes()
        with self.assertRaises(FileExistsError):
            self.execute("print('TEST_NEVER_REPEATED')")
        self.assertEqual((self.state/(ID+'.result')).read_bytes(),before)
        self.assertNotIn('NEVER_REPEATED',path.read_text())

    def test_root_service_drops_user_group_and_supplementary_groups_for_child(self):
        child = Mock(stdout=io.BytesIO(b'TEST output'),wait=Mock(return_value=0),poll=Mock(return_value=0))
        with patch.object(os,'geteuid',return_value=0),patch.object(os,'fchown'), \
                patch.object(command.subprocess,'Popen',return_value=child) as launch:
            result = command.execute(dict(command_id=ID,program='TEST_PROGRAM',argv=['TEST_EXECUTABLE']),
                state=self.state,output=self.output,cwd=str(self.root),identity=(1234,1235))
        options = launch.call_args.kwargs
        self.assertEqual((options['user'],options['group'],options['extra_groups']),(1234,1235,[]))
        self.assertNotIn('shell',options)
        self.assertEqual(options['env']['GAOJI_PROGRAM'],'TEST_PROGRAM')
        self.assertNotIn('GAOJI_JOB_TOKEN',options['env'])
        self.assertEqual(result['exit_code'],0)

    def test_late_systemd_payload_cannot_spawn_after_stop_tombstone(self):
        (self.state/(ID+'.stop')).write_text('{"cancelled":true}')
        with patch.object(command.subprocess,'Popen') as launch:
            result = self.execute("print('TEST_MUST_NOT_RUN')")
        launch.assert_not_called()
        self.assertEqual((result['error'],result['exit_code']),('command_cancelled',None))

    def test_output_cap_stops_copying_and_reports_partial_result(self):
        result = self.execute("import sys;sys.stdout.write('X'*4096);sys.stdout.flush()",limit=1024)
        self.assertEqual(result['error'],'command_output_limit')
        self.assertEqual(result['output_bytes'],1024)
        self.assertEqual((self.output/(ID+'.txt')).stat().st_size,1024)

    def test_symlink_directory_and_existing_file_are_not_followed_or_overwritten(self):
        victim = self.root/'TEST_VICTIM'; victim.write_text('TEST_KEEP')
        self.output.mkdir(); (self.output/(ID+'.txt')).symlink_to(victim)
        result = self.execute("print('TEST_SHOULD_NOT_RUN')")
        self.assertEqual(result['error'],'command_process_FileExistsError')
        self.assertEqual(victim.read_text(),'TEST_KEEP')
        (self.state/(ID+'.result')).unlink()
        (self.output/(ID+'.txt')).unlink(); self.output.rmdir()
        self.output.symlink_to(self.state,target_is_directory=True)
        result = self.execute("print('TEST_SHOULD_NOT_RUN')")
        self.assertTrue(result['error'].startswith('command_process_'))
        self.assertFalse((self.state/(ID+'.txt')).exists())

    def test_invalid_payload_and_missing_executable_fail_explicitly(self):
        with self.assertRaisesRegex(ValueError,'invalid_command_payload'):
            command.execute(dict(command_id='../TEST',program='TEST',argv=['true']))
        result = command.execute(dict(command_id=ID,program='TEST',argv=['/TEST_NONEXISTENT_COMMAND']),
            state=self.state,output=self.output,cwd=str(self.root),identity=(os.getuid(),os.getgid()))
        self.assertIsNone(result['exit_code'])
        self.assertEqual(result['error'],'command_process_FileNotFoundError')
