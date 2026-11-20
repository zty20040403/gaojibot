import os
import stat
import sys
import tarfile
import unittest
from contextlib import chdir
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from nix import work_snapshot as snaps


@unittest.skipUnless(sys.platform == 'linux', 'host nspawn work snapshots require Linux')
class WorkSnapshotTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.source = root / 'containers'
        self.dest = root / 'backups'
        self.dest.mkdir(mode=0o700)
        self.day = date(2026, 1, 1)
        self.a, self.b = (self.source / scope / 'work' for scope in ('s000001', 's000002'))
        for work in [self.a, self.b]:
            work.mkdir(parents=True)
            (work / 'file').write_text(work.parent.name)
            (work.parent / 'metadata').write_text('TEST_NOT_WORK')
        self.enterContext(chdir(self.dest))
        self.addCleanup(os.umask, os.umask(0o077))

    def test_restore_isolated_scopes_without_dereferencing_links(self):
        (self.a / 'empty').mkdir()
        (self.a / 'link').symlink_to(self.b / 'file')
        (self.a / 'relative').symlink_to('file')
        os.mkfifo(self.a / 'pipe')
        (self.source / 'not-a-scope').symlink_to(self.b.parent)
        snaps.enter_destination(self.dest)
        snaps.snapshot(self.source, self.day)
        for work in [self.a, self.b]:
            scope = work.parent.name
            bundle = self.dest / f'{scope}_{self.day}.tar'
            self.assertEqual(stat.S_IMODE(bundle.stat().st_mode), 0o600)
            recovered = self.dest.parent / f'restore-{scope}'
            recovered.mkdir(mode=0o700)
            with tarfile.open(bundle) as archive:
                expected = {'file'}
                if work == self.a:
                    expected |= {'empty', 'link', 'relative'}
                    for name in ['link', 'relative']:
                        member = archive.getmember(name)
                        self.assertTrue(member.issym())
                        self.assertEqual(member.linkname, os.readlink(work / name))
                self.assertEqual(set(archive.getnames()), expected)
                archive.extractall(recovered, members=[m for m in archive if m.isfile() or m.isdir()], filter='data')
            self.assertEqual((recovered / 'file').read_text(), scope)
            self.assertFalse(os.path.lexists(recovered / 'link'))
            self.assertEqual((work / 'file').read_text(), scope)
        self.assertTrue((self.dest.parent / 'restore-s000001/empty').is_dir())

    def test_failed_scope_keeps_previous_complete_archive_and_rotates_by_utc_day(self):
        snaps.snapshot(self.source, self.day)
        old = self.dest / f's000001_{self.day}.tar'
        original = old.read_bytes()
        os.link(self.b.parent / 'metadata', self.a / 'unsafe')
        (self.b / 'file').write_text('TEST_UPDATED')
        with self.assertRaises(ExceptionGroup):
            snaps.snapshot(self.source, self.day)
        self.assertEqual(old.read_bytes(), original)
        self.assertFalse((self.dest / '.pending').exists())
        with tarfile.open(self.dest / f's000002_{self.day}.tar') as archive:
            self.assertEqual(archive.extractfile('file').read(), b'TEST_UPDATED')
        (self.a / 'unsafe').unlink()
        self.b.parent.rename(self.source / 'removed')
        snaps.snapshot(self.source, self.day + timedelta(days=29))
        self.assertTrue(old.exists())
        snaps.snapshot(self.source, self.day + timedelta(days=30))
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()), ['s000001_2026-01-30.tar', 's000001_2026-01-31.tar'])

    def test_symlink_work_or_destination_ancestor_is_not_followed(self):
        self.a.rename(self.a.parent / 'original')
        self.a.symlink_to(self.b, target_is_directory=True)
        with self.assertRaises(ExceptionGroup):
            snaps.snapshot(self.source, self.day)
        self.assertFalse((self.dest / f's000001_{self.day}.tar').exists())
        alias = self.dest.parent / 'alias'
        alias.symlink_to(self.dest.parent, target_is_directory=True)
        with self.assertRaises(OSError):
            snaps.enter_destination(alias / self.dest.name)
        self.dest.chmod(0o755)
        with self.assertRaises(PermissionError):
            snaps.enter_destination(self.dest)
