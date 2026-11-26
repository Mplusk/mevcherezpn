import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import install_hot as i
from test_probe_nodes import profiles


@unittest.skipUnless(os.name == 'posix', 'deployment target is Linux')
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'; self.source.mkdir(mode=0o700)
        for name in i.FILES:
            (self.source / name).write_bytes(('fixture:' + name).encode())
        self.opt = self.root / 'opt'; self.opt.mkdir(mode=0o700)
        (self.opt / 'sync.py').write_bytes(b'original sync')
        (self.opt / 'reduce.py').write_bytes(b'original reduce')
        self.profile_dir = self.root / 'private'; self.profile_dir.mkdir(mode=0o700)
        self.systemd = self.root / 'systemd'; self.systemd.mkdir(mode=0o700)
        self.assets = self.root / 'assets'; self.assets.mkdir(mode=0o700)
        from contextlib import ExitStack
        self.stack = ExitStack()
        for name, value in {'OPT': self.opt, 'HOT': self.opt / 'hot', 'PROFILE_DIR': self.profile_dir,
                            'PROFILE': self.profile_dir / 'profiles.json', 'SYSTEMD': self.systemd,
                            'SERVICE': self.systemd / 'gamelife-hot-bootstrap.service',
                            'TIMER': self.systemd / 'gamelife-hot-bootstrap.timer', 'ASSETS': self.assets,
                            'OLD_SYNC': hashlib.sha256(b'original sync').hexdigest(),
                            'OLD_REDUCE': hashlib.sha256(b'original reduce').hexdigest()}.items():
            self.stack.enter_context(patch.object(i, name, value))
        self.stack.enter_context(patch.object(i, 'validate_identity'))
        self.stack.enter_context(patch.object(i.enable_hot, 'locks', return_value=[]))
        def systemctl(args, **kwargs):
            return subprocess.CompletedProcess(args, 1 if 'is-enabled' in args else 0)
        self.stack.enter_context(patch.object(i.subprocess, 'run', side_effect=systemctl))

    def tearDown(self):
        self.stack.close()
        self.tmp.cleanup()

    def test_install_is_versioned_and_keeps_old_updater(self):
        revision = i.install('de', profiles(), self.source)
        self.assertEqual(os.readlink(i.HOT / 'current'), 'releases/' + revision)
        self.assertEqual((self.opt / 'sync.py').read_bytes(), b'original sync')
        self.assertEqual(i.PROFILE.stat().st_mode & 0o777, 0o600)
        self.assertIn('00:00 UTC', i.TIMER.read_text())
        self.assertIn('Persistent=false', i.TIMER.read_text())
        self.assertEqual(i.install('de', profiles(), self.source), revision)

    def test_early_failure_preserves_existing_private_file(self):
        previous = b'previous private contents'
        i.PROFILE.write_bytes(previous); i.PROFILE.chmod(0o600)
        i.HOT.mkdir(mode=0o755)
        i.HOT.chmod(0o755)
        with self.assertRaises(ValueError):
            i.install('de', profiles(), self.source)
        self.assertEqual(i.PROFILE.read_bytes(), previous)

    def test_failed_timer_enable_rolls_back_only_own_files(self):
        previous = b'previous private contents'
        i.PROFILE.write_bytes(previous); i.PROFILE.chmod(0o600)
        def failed_enable(args, **kwargs):
            if 'is-enabled' in args:
                return subprocess.CompletedProcess(args, 1)
            if 'enable' in args:
                raise subprocess.CalledProcessError(1, args)
            return subprocess.CompletedProcess(args, 0)
        with patch.object(i.subprocess, 'run', side_effect=failed_enable):
            with self.assertRaises(subprocess.CalledProcessError):
                i.install('de', profiles(), self.source)
        self.assertEqual(i.PROFILE.read_bytes(), previous)
        self.assertFalse(i.SERVICE.exists())
        self.assertFalse(i.TIMER.exists())
        self.assertFalse((i.HOT / 'current').exists())

    def test_each_node_has_a_separate_slot(self):
        self.assertEqual(len(set(i.MINUTE.values())), 4)
        for name in i.MINUTE:
            self.assertIn('Persistent=false', i.timer_text(name).decode())


if __name__ == '__main__':
    unittest.main()
