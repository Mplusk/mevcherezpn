import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from opt_in import FILES, MARKER, SALT, is_enabled


@unittest.skipUnless(os.name == 'posix' and os.geteuid() == 0, 'root-owned Linux files required')
class OptInTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'assets'
        self.root.mkdir(mode=0o755)
        self.revisions = self.root / 'revisions'
        self.revisions.mkdir(mode=0o755)
        data = {name: ('public-' + name).encode() for name in FILES}
        hashes = {name: hashlib.sha256(value).hexdigest() for name, value in data.items()}
        revision = hashlib.sha256(SALT + json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        target = self.revisions / revision
        target.mkdir(mode=0o755)
        for name, value in data.items():
            (target / name).write_bytes(value)
            (target / name).chmod(0o644)
        (self.root / 'current').symlink_to('revisions/' + revision)
        self.marker = self.root / MARKER

    def tearDown(self):
        self.temp.cleanup()

    def enable(self):
        self.marker.write_bytes(b'v1\n')
        self.marker.chmod(0o600)

    def test_absent_marker_keeps_overlay_disabled(self):
        self.assertFalse(is_enabled(self.root))
        self.assertFalse(is_enabled(self.root / 'missing'))

    def test_valid_marker_and_assets_enable_overlay(self):
        self.enable()
        self.assertTrue(is_enabled(self.root))

    def test_marker_mode_or_content_fail_closed(self):
        self.enable()
        self.marker.chmod(0o644)
        with self.assertRaises(ValueError):
            is_enabled(self.root)
        self.marker.chmod(0o600)
        self.marker.write_bytes(b'wrong\n')
        with self.assertRaises(ValueError):
            is_enabled(self.root)

    def test_marker_symlink_or_hardlink_refused(self):
        self.enable()
        copy = self.root / 'copy'
        os.link(self.marker, copy)
        with self.assertRaises(ValueError):
            is_enabled(self.root)
        copy.unlink()
        self.marker.unlink()
        self.marker.symlink_to('current/geoip.dat')
        with self.assertRaises(OSError):
            is_enabled(self.root)

    def test_bad_assets_fail_closed(self):
        self.enable()
        (self.root / 'current' / 'geosite.dat').write_bytes(b'bad')
        with self.assertRaises(ValueError):
            is_enabled(self.root)
        (self.root / 'current').unlink()
        (self.root / 'current').symlink_to('/etc')
        with self.assertRaises(ValueError):
            is_enabled(self.root)


if __name__ == '__main__':
    unittest.main()
