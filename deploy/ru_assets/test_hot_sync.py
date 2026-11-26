import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import zlib

import hot_sync
import sync
from test_sync import dat


@unittest.skipUnless(os.name == 'posix', 'deployment target is Linux')
class HotSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'assets'
        self.data = {name: dat(codes) for name, codes in sync.RULES.items()}
        self.data.update({name: b'SRS\x01' + zlib.compress(b'safe asset') for name in hot_sync.SRS})
        self.old = sync.sync(self.root, self.source, lambda _: None)
        self.identity = {'xray_pid': 2, 'xray_start': 3, 'core_pid': 4, 'core_start': 5, 'watched': self.old}

    def tearDown(self):
        self.temp.cleanup()

    def source(self, name):
        if name == 'manifest.json':
            hashes = {key: hashlib.sha256(value).hexdigest() for key, value in self.data.items()}
            rev = hashlib.sha256(b'gamelife-geo-manifest-v1\x00' + json.dumps(hashes, sort_keys=True).encode()).hexdigest()
            return json.dumps({'version': 1, 'revision': rev, 'sha256': hashes}).encode()
        return self.data[name]

    def changed_data(self):
        self.data['geosite.dat'] = dat(sync.RULES['geosite.dat']) + dat({'EXTRA'})
        self.data[hot_sync.SRS[0]] = b'SRS\x01' + zlib.compress(b'new asset')

    def test_notification_replaces_same_bytes_on_watched_old_revision(self):
        directory = self.root / 'revisions' / self.old
        before = {name: (directory / name).read_bytes() for name in hot_sync.SRS}
        inodes = {name: (directory / name).stat().st_ino for name in hot_sync.SRS}
        hot_sync.notify_watcher(self.root, self.old)
        for name in hot_sync.SRS:
            self.assertEqual((directory / name).read_bytes(), before[name])
            self.assertNotEqual((directory / name).stat().st_ino, inodes[name])

    def test_new_revision_notifies_watcher_and_preserves_old_directory(self):
        self.changed_data()
        calls = []
        def notify(root, watched):
            calls.append((watched, os.readlink(root / 'current')))
            hot_sync.notify_watcher(root, watched)
        new = hot_sync.run(self.root, self.source, lambda _: None,
                           inspect=lambda: dict(self.identity), notify=notify)
        self.assertNotEqual(new, self.old)
        self.assertEqual(calls, [(self.old, 'revisions/' + new)])
        self.assertTrue((self.root / 'revisions' / self.old).is_dir())

    def test_failed_notification_restores_selector_and_notifies_again(self):
        self.changed_data()
        calls = []
        def notify(root, watched):
            calls.append(os.readlink(root / 'current'))
            if len(calls) == 1:
                raise RuntimeError('notification failed')
        with self.assertRaisesRegex(RuntimeError, 'selector restored'):
            hot_sync.run(self.root, self.source, lambda _: None,
                         inspect=lambda: dict(self.identity), notify=notify)
        self.assertEqual(os.readlink(self.root / 'current'), 'revisions/' + self.old)
        self.assertEqual(calls[-1], 'revisions/' + self.old)

    def test_process_change_during_publish_aborts_and_rearms_current_watcher(self):
        self.changed_data()
        changed = dict(self.identity, core_pid=6, core_start=7)
        identities = iter((dict(self.identity), changed, changed))
        calls = []
        with self.assertRaisesRegex(RuntimeError, 'selector restored'):
            hot_sync.run(self.root, self.source, lambda _: None,
                         inspect=lambda: next(identities), notify=lambda root, watched: calls.append(watched))
        self.assertEqual(os.readlink(self.root / 'current'), 'revisions/' + self.old)
        self.assertEqual(calls, [self.old])

    def test_unsafe_watcher_directory_is_rejected(self):
        with self.assertRaises(ValueError):
            hot_sync.notify_watcher(self.root, '../outside')
        watched = self.root / 'revisions' / self.old
        (watched / hot_sync.SRS[0]).unlink()
        (watched / hot_sync.SRS[0]).symlink_to('/etc/passwd')
        with self.assertRaises(ValueError):
            hot_sync.notify_watcher(self.root, self.old)


if __name__ == '__main__':
    unittest.main()
