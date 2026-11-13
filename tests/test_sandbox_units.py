"""Real guest-shaped files, with systemd commands replaced (no host services)."""
import os
from pathlib import Path
import tempfile
import unittest

from src.plugins.ai_chat import sandbox_units as units

SERVICE = '[Service]\nExecStart=/usr/bin/true\n'


class SandboxUnitsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.registry,self.runtime,self.system = [self.root/name for name in ('registry','runtime','system')]
        for path in (self.registry,self.runtime,self.system):
            path.mkdir()
        self.calls,self.states,self.fail = [],{},set()
        def command(args):
            self.calls.append(args)
            if args[0]=='journalctl':
                return 0,'TEST last failure\n'
            operation,name = args[1],args[-1]
            if operation in {'start','restart'}:
                self.states[name] = 'failed' if name in self.fail else 'active'
                if name in self.fail:
                    return 1,'TEST cannot start'
            if operation=='stop':
                self.states[name] = 'inactive'
            if operation=='show':
                return 0,'LoadState=loaded\nActiveState='+self.states.get(name,'inactive')+'\nSlice='+units.SLICE+'\n'
            return 0,''
        self.manager = units.Units(self.registry,self.runtime,system=(self.system,),command=command)

    def test_add_change_remove_and_idempotent_sync_only_control_owned_units(self):
        source = self.registry/'TEST.service'; source.write_text(SERVICE)
        self.assertTrue(self.manager.sync()['ok'])
        self.assertIn(['systemctl','start','TEST.service'],self.calls)
        config = (self.runtime/'TEST.service.d'/units.DROPIN).read_text()
        self.assertIn('Slice=gaoji-programs.slice',config)
        self.assertIn('GAOJI_PROGRAM=TEST',config)
        limits = (self.runtime/units.SLICE).read_text()
        self.assertIn('MemoryMax=50%',limits); self.assertIn('CPUWeight=10',limits); self.assertIn('IOWeight=10',limits)
        self.calls.clear()
        self.assertTrue(self.manager.sync()['ok'])
        self.assertFalse(any(row[1] in {'start','restart','stop'} for row in self.calls))
        source.write_text(SERVICE+'Environment=TEST=value\n')
        self.assertTrue(self.manager.sync()['ok'])
        self.assertIn(['systemctl','restart','TEST.service'],self.calls)
        source.unlink()
        self.assertTrue(self.manager.sync()['ok'])
        self.assertIn(['systemctl','stop','TEST.service'],self.calls)
        self.assertFalse((self.runtime/'TEST.service').exists())
        self.assertFalse((self.runtime/'TEST.service.d'/units.DROPIN).exists())

    def test_system_units_runtime_units_and_masks_never_receive_files_or_dropins(self):
        (self.system/'TEST_SYSTEM.service').write_text(SERVICE)
        (self.runtime/'TEST_RUNTIME.service').write_text(SERVICE)
        (self.runtime/'TEST_MASK.service').symlink_to('/dev/null')
        names = ('TEST_SYSTEM.service','TEST_RUNTIME.service','TEST_MASK.service','gaoji-units.service')
        for name in names:
            (self.registry/name).write_text(SERVICE)
        (self.registry/'TEST_OK.timer').write_text('[Timer]\nOnActiveSec=60\n')
        result = self.manager.sync()
        self.assertFalse(result['ok']); self.assertEqual(set(result['errors']),set(names))
        for name in names:
            self.assertFalse((self.runtime/(name+'.d')).exists())
            self.assertFalse(any(row[1] in {'start','stop','restart'} and row[-1]==name for row in self.calls))
        self.assertEqual((self.runtime/'TEST_RUNTIME.service').read_text(),SERVICE)
        self.assertIn(['systemctl','start','TEST_OK.timer'],self.calls)

    def test_links_special_files_and_bad_updates_cannot_replace_a_registered_unit(self):
        (self.registry/'TEST.service').write_text(SERVICE)
        self.manager.sync(); before = (self.runtime/'TEST.service').read_bytes()
        (self.registry/'TEST.service').unlink()
        (self.registry/'TEST.service').symlink_to(self.system/'TEST_OTHER')
        os.mkfifo(self.registry/'TEST_FIFO.service')
        (self.registry/'TEST_LARGE.service').write_bytes(b'X'*65537)
        (self.registry/'TEST_BINARY.service').write_bytes(b'TEST\0')
        result = self.manager.sync()
        self.assertFalse(result['ok']); self.assertEqual(len(result['errors']),4)
        self.assertEqual((self.runtime/'TEST.service').read_bytes(),before)
        self.assertNotIn(['systemctl','stop','TEST.service'],self.calls)

    def test_registry_parent_symlink_and_too_many_units_fail_before_effects(self):
        real = self.root/'TEST_REAL'; self.registry.rename(real); self.registry.symlink_to(real)
        with self.assertRaises(OSError):
            self.manager.sync()
        self.assertEqual(self.calls,[])
        self.registry.unlink(); real.rename(self.registry)
        for index in range(33):
            (self.registry/f'TEST_{index}.service').write_text(SERVICE)
        with self.assertRaisesRegex(units.UnitError,'unit_limit_exceeded'):
            self.manager.sync()
        self.assertEqual(self.calls,[])

    def test_failed_start_is_reported_with_recent_logs_and_no_success_claim(self):
        (self.registry/'TEST_FAIL.service').write_text(SERVICE)
        self.fail.add('TEST_FAIL.service')
        result = self.manager.sync()
        self.assertFalse(result['ok'])
        self.assertEqual(result['units'][0]['ActiveState'],'failed')
        self.assertIn('TEST last failure',result['units'][0]['logs'])
        self.assertIn('TEST_FAIL.service',result['errors'])

    def test_disabled_service_and_timer_stay_paused_until_host_policy_enables_them(self):
        for suffix in ('service','timer'):
            (self.registry/('TEST.'+suffix)).write_text(SERVICE if suffix=='service' else '[Timer]\nOnActiveSec=60\n')
        result = self.manager.sync(disabled=['TEST'])
        self.assertTrue(result['ok'])
        self.assertTrue(all(not item['enabled'] and item['ActiveState']=='inactive' for item in result['units']))
        self.assertFalse(any(row[1] in {'start','restart'} for row in self.calls))
        for suffix in ('service','timer'):
            name = 'TEST.'+suffix
            self.assertTrue((self.runtime/('.gaoji-paused-'+name)).exists())
            self.assertIn('ConditionPathExists=!',(self.runtime/(name+'.d')/units.DROPIN).read_text())
        self.calls.clear()
        self.assertTrue(self.manager.sync()['ok'])
        for suffix in ('service','timer'):
            self.assertIn(['systemctl','restart','TEST.'+suffix],self.calls)
        self.assertTrue(self.manager.sync(enabled=False)['ok'])
        self.assertTrue(all(value=='inactive' for value in self.states.values()))

    def test_invalid_host_policy_fails_before_any_systemd_change(self):
        for policy in ({'enabled':'true'},{'disabled':['../TEST']},{'disabled':['TEST']*257}):
            with self.assertRaisesRegex(units.UnitError,'invalid_unit_policy'):
                self.manager.sync(**policy)
        self.assertEqual(self.calls,[])

    def test_tool_bundle_contains_the_standalone_sync_command(self):
        import subprocess
        import sys
        from src.plugins.ai_chat import sandbox_tools_setup
        entry = next(item for item in sandbox_tools_setup.files() if item['path'].endswith('/sandbox_units.py'))
        self.assertEqual(entry['content'],Path(units.__file__).read_text())
        self.assertIn('exec /usr/bin/python3 /usr/local/share/gaoji/sandbox_gaoji.py units',sandbox_tools_setup.INSTALL)
        result = subprocess.run([sys.executable,'-I',units.__file__,'--help'],capture_output=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        import json
        from src.plugins.ai_chat.vm_sandbox import VmSandboxManager
        prepared = json.loads(VmSandboxManager._cloud_config(conversation=True,prepared=True).split('\n',1)[1])
        paths = {item['path']:item for item in prepared['write_files']}
        for name in ('gaoji','gaoji-units'):
            self.assertEqual(paths['/usr/local/bin/'+name]['permissions'],'0755')

    def test_unmanaged_dropin_and_system_slice_are_not_overwritten(self):
        (self.registry/'TEST.service').write_text(SERVICE)
        folder = self.runtime/'TEST.service.d'; folder.mkdir()
        (folder/units.DROPIN).write_text('TEST_UNMANAGED')
        self.assertFalse(self.manager.sync()['ok'])
        self.assertFalse((self.runtime/'TEST.service').exists())
        self.assertEqual((folder/units.DROPIN).read_text(),'TEST_UNMANAGED')
        (self.system/units.SLICE).write_text('[Slice]\n')
        with self.assertRaisesRegex(units.UnitError,'system_slice_conflict'):
            self.manager.sync()
