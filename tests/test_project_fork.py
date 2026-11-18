import errno
import json
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.plugins.ai_chat import sandbox_project_fork as forks
from src.plugins.ai_chat.pi_engine import _PREPARE_PY


class ProjectForkTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.work = Path(root.name).resolve() / 'work'
        self.source = self.work / 'projects' / 'TEST_tables'
        self.source.mkdir(parents=True)
        for job_id in (1, 2):
            (self.work / '.gaoji' / 'jobs' / f'job-{job_id}').mkdir(parents=True)

    def test_independent_job_directories_never_replace_existing_outputs(self):
        (self.source / 'sub').mkdir()
        a, b = (forks.fork_project(self.work, self.source.name, i) for i in (1, 2))
        (a / 'sub' / 'result').write_text('TEST A')
        self.assertFalse((b / 'sub' / 'result').exists())
        self.assertFalse((self.source / 'sub' / 'result').exists())
        with self.assertRaises(FileExistsError):
            forks.fork_project(self.work, self.source.name, 1)
        self.assertEqual((a / 'sub' / 'result').read_text(), 'TEST A')

    def test_pi_preparation_uses_the_job_copy_not_the_shared_project(self):
        conf = self.work / 'conf'
        conf.mkdir()
        (conf / 'prompt-template.md').write_text('workdir=@WORKDIR@ notes=@NOTES@')
        (conf / 'meta.json').write_text(json.dumps({'job_id': 1, 'fork_project': self.source.name}))
        out = self.work / '.gaoji/jobs/job-1'

        def clone(args, *, check):
            self.assertEqual(args, ['gaoji-project-fork', self.source.name, '1'])
            self.assertTrue(check)
            forks.fork_project(self.work, args[1], int(args[2]))

        with patch.object(sys, 'argv', ['prepare.py', str(conf), str(out)]), patch('subprocess.run', side_effect=clone):
            exec(_PREPARE_PY.replace("'/work", f"'{self.work}"), {})
        self.assertEqual((conf / 'workdir').read_text(), str(out / 'project'))
        self.assertIn(str(out / 'project'), (conf / 'job-system.md').read_text())

    def test_rejects_paths_links_and_hidden_entries(self):
        for name in ('../other', '/work', '.', '', 'a/b'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                forks.fork_project(self.work, name, 1)
        (self.source / 'link').symlink_to(self.work)
        with self.assertRaises(ValueError):
            forks.fork_project(self.work, self.source.name, 1)
        (self.source / 'link').unlink()
        (self.source / '.hidden').mkdir()
        with self.assertRaises(ValueError):
            forks.fork_project(self.work, self.source.name, 1)
        self.assertEqual(list((self.work / '.gaoji/jobs/job-1').iterdir()), [])

    def test_unsupported_filesystem_never_falls_back_to_full_copy(self):
        (self.source / 'data.csv').write_text('TEST data')
        with patch.object(forks.fcntl, 'ioctl', side_effect=OSError(errno.EOPNOTSUPP, 'unsupported')) as clone:
            with self.assertRaises(OSError) as failed:
                forks.fork_project(self.work, self.source.name, 1)
        self.assertEqual(failed.exception.errno, errno.EOPNOTSUPP)
        self.assertEqual(clone.call_args.args[1], forks.FICLONE)
        self.assertEqual(list((self.work / '.gaoji/jobs/job-1').iterdir()), [])
