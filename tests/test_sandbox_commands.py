"""Real temporary manifests/output; substitute systemd, never operate host units."""
import json
from pathlib import Path
import tempfile
import unittest

from src.plugins.ai_chat import sandbox_commands as commands

ID,BOOT = 'a'*32,'TEST_BOOT'


class SandboxCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.calls,self.units = [],{}
        self.lost_start,self.lingering = False,False
        def command(args):
            self.calls.append(args)
            if args[0]=='systemd-run':
                unit = next(item.removeprefix('--unit=') for item in args if item.startswith('--unit='))
                self.units[unit] = dict(LoadState='loaded',ActiveState='active',SubState='running',Result='success',ExecMainCode='0',MainPID='123',ControlPID='0',TasksCurrent='1')
                return (1 if self.lost_start else 0),'TEST start response'
            if args[1]=='daemon-reload':
                return 0,''
            unit = args[-1]
            if args[1]=='stop':
                self.units[unit].update(ActiveState='inactive',SubState='dead',Result='success',MainPID='0',ControlPID='0',TasksCurrent='1' if self.lingering else '0')
                return 0,''
            state = self.units.get(unit,dict(LoadState='not-found',ActiveState='inactive',ExecMainCode='0'))
            return 0,'\n'.join(key+'='+value for key,value in state.items())
        self.command = command
        self.worker = self.reopen()

    def reopen(self):
        return commands.Commands(state=self.root/'state',output=self.root/'output',runtime=self.root/'runtime',system=(),
            command=self.command,clock=lambda:1000,boot=lambda:BOOT)

    def request(self, operation='start', **changes):
        data = dict(op=operation,command_id=ID,boot_id=BOOT)
        if operation=='start':
            data.update(argv=['printf','TEST'],program='TEST_PROGRAM',deadline_at=1060)
        return self.worker.handle(data|changes)

    def test_start_is_once_only_without_shell_and_success_survives_unit_collection(self):
        self.assertEqual(self.request()['state'],'running')
        self.worker = self.reopen()
        self.assertEqual(self.request()['state'],'running')
        starts = [row for row in self.calls if row[0]=='systemd-run']
        self.assertEqual(len(starts),1)
        for option in ('--property=RuntimeMaxSec=60','--property=KillMode=control-group','--property=Slice=gaoji-commands.slice','--property=Restart=no'):
            self.assertIn(option,starts[0])
        self.assertEqual(starts[0][-3:],['/usr/bin/python3',commands.SCRIPT,ID])
        self.assertIn('MemoryMax=50%',(self.root/'runtime'/'gaoji-commands.slice').read_text())
        output = self.root/'output'; output.mkdir(); (output/(ID+'.txt')).write_text('HEAD'+'X'*5000+'TAIL')
        (self.root/'state'/(ID+'.result')).write_text(json.dumps(dict(exit_code=7,elapsed_seconds=12,error='')))
        result = self.request('status')
        self.assertEqual((result['state'],result['stopped'],result['exit_code']),('failed',True,7))
        self.assertTrue(result['output_head'].startswith('HEAD')); self.assertTrue(result['output_tail'].endswith('TAIL'))
        self.assertLessEqual(len(result['output_head'])+len(result['output_tail']),4096)
        self.units.clear()
        self.assertEqual(self.request('status'),result)
        self.assertEqual(self.request(),result)
        self.assertEqual(len([row for row in self.calls if row[0]=='systemd-run']),1)

    def test_stop_before_start_leaves_a_tombstone_and_never_launches(self):
        stopped = self.request('stop')
        self.assertEqual((stopped['state'],stopped['stopped']),('cancelled',True))
        self.assertEqual(self.request(),stopped)
        self.assertFalse(any(row[0]=='systemd-run' for row in self.calls))
        self.assertTrue((self.root/'state'/(ID+'.stop')).exists())

    def test_lost_start_does_not_retry_and_stop_requires_empty_cgroup(self):
        self.lost_start = True
        with self.assertRaisesRegex(commands.CommandError,'start_unconfirmed'):
            self.request()
        self.assertEqual(self.request()['state'],'running')
        self.lingering = True
        with self.assertRaisesRegex(commands.CommandError,'stop_unconfirmed'):
            self.request('stop')
        self.assertFalse((self.root/'state'/(ID+'.final')).exists())
        self.lingering = False
        self.assertTrue(self.request('stop')['stopped'])
        self.assertEqual(len([row for row in self.calls if row[0]=='systemd-run']),1)

    def test_timeout_preserves_reason_even_when_stop_clears_systemd_result(self):
        self.request()
        next(iter(self.units.values())).update(ActiveState='failed',Result='timeout',ExecMainCode='2')
        result = self.request('status')
        self.assertEqual((result['state'],result['stopped'],result['error']),('failed',True,'command_timeout'))
        self.assertIsNone(result['exit_code'])
        self.units.clear()
        self.assertEqual(self.request('status'),result)

    def test_bad_deadline_boot_or_manifest_never_restarts_a_unit(self):
        for changes in ({'deadline_at':1000},{'deadline_at':22601},{'command_id':'../TEST'},{'argv':[]}):
            with self.assertRaises((commands.CommandError,ValueError)):
                self.request(**changes)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.request(boot_id='TEST_REPLACED')['error'],'vm_boot_replaced')
        self.assertFalse((self.root/'state').exists())
        self.request()
        with self.assertRaisesRegex(commands.CommandError,'intent_conflict'):
            self.request(argv=['TEST_DIFFERENT'])
        self.assertEqual(len([row for row in self.calls if row[0]=='systemd-run']),1)

    def test_prepared_vm_tool_bundle_contains_controller_and_process_payload(self):
        from src.plugins.ai_chat import sandbox_tools_setup
        entries = {item['path']:item for item in sandbox_tools_setup.files()}
        for name in ('sandbox_commands.py','sandbox_command_process.py','sandbox_units.py'):
            self.assertIn('/usr/local/share/gaoji/'+name,entries)
        self.assertEqual(entries[commands.SCRIPT]['permissions'],'0644')

    def test_symlink_output_is_reported_unavailable_and_unmanaged_slice_blocks_start(self):
        runtime = self.root/'runtime'; runtime.mkdir(); (runtime/'gaoji-commands.slice').write_text('TEST_UNMANAGED')
        with self.assertRaises(RuntimeError):
            self.request()
        self.assertFalse((self.root/'state'/(ID+'.json')).exists())
        (runtime/'gaoji-commands.slice').unlink()
        self.request()
        output = self.root/'output'; output.mkdir(); (output/(ID+'.txt')).symlink_to(self.root/'state'/(ID+'.json'))
        self.assertEqual(self.request('status')['output_error'],'command_output_unavailable')
