"""Actual temporary guest-like files, without touching the host /run or /proc."""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from src.plugins.ai_chat import sandbox_identity

BOOT = '00000000-0000-4000-8000-000000000001'
TOKEN = 'gvm_'+'T'*43
URL = 'http://10.0.2.2:18640'


class SandboxIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory,self.boot_file = self.root/'run'/'gaoji',self.root/'TEST_BOOT_ID'
        self.boot_file.write_text(BOOT+'\n')
        self.payload = dict(op='install',token=TOKEN,boot_id=BOOT,base_url=URL)

    def run_guest(self, payload):
        return sandbox_identity.run(payload,directory=self.directory,boot_file=self.boot_file)

    def test_install_is_private_atomic_and_returns_only_fingerprint(self):
        self.assertIsNone(self.run_guest({'op':'inspect'})['token_hash'])
        result = self.run_guest(self.payload)
        self.assertEqual(result,dict(boot_id=BOOT,token_hash=hashlib.sha256(TOKEN.encode()).hexdigest(),base_url=URL))
        self.assertNotIn(TOKEN,json.dumps(result))
        self.assertEqual((self.directory/'token').read_text(),TOKEN)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode),0o700)
        self.assertEqual(stat.S_IMODE((self.directory/'token').stat().st_mode),0o600)
        self.assertEqual(self.run_guest({'op':'inspect'}),result)
        newer = dict(self.payload,token='gvm_'+'N'*43)
        self.run_guest(newer)
        self.assertEqual((self.directory/'token').read_text(),newer['token'])
        self.assertFalse(list(self.directory.glob('.token-*')))

    def test_wrong_boot_and_wrong_host_url_never_publish_a_token(self):
        for change in ({'boot_id':'00000000-0000-4000-8000-000000000002'},
                {'base_url':'https://example.test'},{'base_url':URL+'/redirect'},
                {'base_url':'http://TEST:SECRET@10.0.2.2:18640'}, {'token':'TEST_SECRET'}):
            with self.assertRaises(ValueError):
                self.run_guest(dict(self.payload,**change))
            self.assertFalse((self.directory/'token').exists())

    def test_special_files_and_links_are_not_read(self):
        self.run_guest(self.payload)
        token = self.directory/'token'
        token.unlink()
        victim = self.root/'TEST_VICTIM'; victim.write_text(TOKEN)
        token.symlink_to(victim)
        with self.assertRaises(OSError):
            self.run_guest({'op':'inspect'})
        # Atomic install replaces the link itself, not its target.
        self.run_guest(dict(self.payload,token='gvm_'+'N'*43))
        self.assertEqual(victim.read_text(),TOKEN)
        token.unlink(); os.mkfifo(token)
        with self.assertRaises(ValueError):
            self.run_guest({'op':'inspect'})
        token.unlink(); token.write_text('T'*129)
        with self.assertRaises(ValueError):
            self.run_guest({'op':'inspect'})

    def test_directory_symlink_is_refused(self):
        self.directory.parent.mkdir(parents=True)
        victim = self.root/'TEST_OTHER'; victim.mkdir()
        self.directory.symlink_to(victim,target_is_directory=True)
        with self.assertRaises(OSError):
            self.run_guest(self.payload)
        self.assertEqual(list(victim.iterdir()),[])

    def test_cli_errors_do_not_print_payload_or_traceback(self):
        result = subprocess.run([sys.executable,sandbox_identity.__file__],capture_output=True,
            input=json.dumps({'op':'install','token':'TEST_SECRET'}).encode(),timeout=10)
        self.assertEqual(result.returncode,1)
        self.assertEqual(json.loads(result.stdout),{'error':'vm_token_io_failed'})
        self.assertNotIn(b'TEST_SECRET',result.stdout+result.stderr)
        self.assertNotIn(b'Traceback',result.stdout+result.stderr)
