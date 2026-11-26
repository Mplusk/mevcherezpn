import os
import hashlib
import json
import subprocess
from pathlib import Path
import tempfile
import unittest
import zlib

import sync


def varint(n):
    out = bytearray()
    while n > 127:
        out.append((n & 127) | 128); n >>= 7
    out.append(n)
    return bytes(out)


def dat(codes):
    result = bytearray()
    for code in sorted(codes):
        b = code.encode('ascii')
        # A valid protobuf entry with one valid nested entry message.
        body = b'\x0a' + varint(len(b)) + b + b'\x12\x06\x0a\x04test'
        result.extend(b'\x0a' + varint(len(body)) + body)
    return bytes(result)


@unittest.skipUnless(os.name == 'posix', 'deployment target is Linux')
class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'ru-assets'
        self.data = {name: dat(wanted) for name, wanted in sync.RULES.items()}
        self.data.update({name: b'SRS\x01' + zlib.compress(b'valid data')
                          for name in sync.FILES if name.endswith('.srs')})

    def tearDown(self):
        self.temp.cleanup()

    def source(self, name):
        if name == 'manifest.json':
            hashes = {key: hashlib.sha256(value).hexdigest() for key, value in self.data.items()}
            revision = hashlib.sha256(b'gamelife-geo-manifest-v1\x00' + json.dumps(hashes, sort_keys=True).encode()).hexdigest()
            return json.dumps({'version': 1, 'revision': revision, 'sha256': hashes}).encode()
        return self.data[name]

    def test_all_assets_published_as_single_immutable_revision(self):
        revision = sync.sync(self.root, self.source, lambda _: None)
        self.assertEqual(os.readlink(self.root / 'current'), 'revisions/' + revision)
        for name in sync.FILES:
            self.assertEqual((self.root / 'current' / name).read_bytes(), self.data[name])
        self.assertEqual(sync.sync(self.root, self.source, lambda _: None), revision)

    def test_failed_download_preserves_previous_revision(self):
        first = sync.sync(self.root, self.source, lambda _: None)
        def broken(name):
            if name.endswith('.srs'):
                raise TimeoutError()
            return self.source(name)
        with self.assertRaises(TimeoutError):
            sync.sync(self.root, broken, lambda _: None)
        self.assertEqual(os.readlink(self.root / 'current'), 'revisions/' + first)

    def test_invalid_asset_preserves_previous_revision(self):
        first = sync.sync(self.root, self.source, lambda _: None)
        corrupt = dict(self.data, **{'geosite-ru-blocked.srs': b'SRS\x01broken'})
        with self.assertRaises((ValueError, zlib.error)):
            sync.sync(self.root, lambda name: self.source(name) if name == 'manifest.json' else corrupt[name], lambda _: None)
        self.assertEqual(os.readlink(self.root / 'current'), 'revisions/' + first)

    def test_xray_rejection_preserves_previous_revision(self):
        first = sync.sync(self.root, self.source, lambda _: None)
        def reject(_):
            raise ValueError('Xray rejects candidate')
        with self.assertRaises(ValueError):
            sync.sync(self.root, self.source, reject)
        self.assertEqual(os.readlink(self.root / 'current'), 'revisions/' + first)

    def test_engine_validation_uses_isolated_pinned_container(self):
        calls = []
        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0)
        sync.validate_xray(self.data, runner)
        args, options = calls[0]
        self.assertEqual(args[args.index('--network') + 1], 'none')
        self.assertIn('--read-only', args)
        self.assertIn(sync.IMAGE, args)
        self.assertTrue(options['input'])

    def test_unsafe_current_link_rejected(self):
        sync.sync(self.root, self.source, lambda _: None)
        (self.root / 'current').unlink()
        (self.root / 'current').symlink_to('/etc')
        with self.assertRaises(ValueError):
            sync.sync(self.root, self.source, lambda _: None)
        self.assertEqual(os.readlink(self.root / 'current'), '/etc')

    def test_untrusted_url_and_bad_srs_rejected(self):
        with self.assertRaises(ValueError):
            sync.fetch('../secret')
        with self.assertRaises(ValueError):
            sync.validate('geoip-ru-blocked.srs', b'SRS\x02bad')

    def test_mixed_manifest_revisions_preserve_previous_revision(self):
        first = sync.sync(self.root, self.source, lambda _: None)
        calls = []
        def inconsistent(name):
            calls.append(name)
            if name == 'manifest.json' and calls.count(name) % 2 == 0:
                altered = dict(self.data, **{'geoip.dat': self.data['geoip.dat'] + b'x'})
                hashes = {key: hashlib.sha256(value).hexdigest() for key, value in altered.items()}
                revision = hashlib.sha256(b'gamelife-geo-manifest-v1\x00' + json.dumps(hashes, sort_keys=True).encode()).hexdigest()
                return json.dumps({'version': 1, 'revision': revision, 'sha256': hashes}).encode()
            return self.source(name)
        with self.assertRaisesRegex(ValueError, 'version changed'):
            sync.sync(self.root, inconsistent, lambda _: None)
        self.assertEqual(calls.count('manifest.json'), 6)
        self.assertEqual(os.readlink(self.root / 'current'), 'revisions/' + first)

    def test_failed_live_notification_restores_previous_revision(self):
        first = sync.sync(self.root, self.source, lambda _: None)
        self.data['geosite.dat'] = dat(sync.RULES['geosite.dat']) + dat({'EXTRA'})
        calls = []
        def fail(_revision):
            calls.append('commit')
            raise RuntimeError('watcher failed')
        with self.assertRaisesRegex(RuntimeError, 'selector restored'):
            sync.sync(self.root, self.source, lambda _: None, protected=(first,),
                      on_commit=fail, on_rollback=lambda: calls.append('rollback'))
        self.assertEqual(os.readlink(self.root / 'current'), 'revisions/' + first)
        self.assertEqual(calls, ['commit', 'rollback'])

    def test_cleanup_keeps_the_directory_watched_by_the_running_core(self):
        first = sync.sync(self.root, self.source, lambda _: None)
        revisions = self.root / 'revisions'
        for number in range(1, 11):
            path = revisions / ('%064x' % number)
            path.mkdir(mode=0o755)
            for name in sync.FILES:
                (path / name).write_bytes(b'public')
            os.utime(path, ns=(number, number))
        old = revisions / first
        os.utime(old, ns=(1, 1))
        sync.cleanup(revisions, '%064x' % 10, protected=(first,))
        self.assertTrue(old.is_dir())

    def test_cleanup_retains_active_and_recent_and_never_follows_symlinks(self):
        active = sync.sync(self.root, self.source, lambda _: None)
        revisions = self.root / 'revisions'
        for number in range(1, 11):
            path = revisions / (('%064x' % number))
            path.mkdir(mode=0o755)
            for name in sync.FILES:
                (path / name).write_bytes(b'public')
            os.utime(path, ns=(number, number))
        external = Path(self.temp.name) / 'outside'
        external.mkdir()
        link = revisions / ('f' * 64)
        link.symlink_to(external)
        sync.cleanup(revisions, active)
        self.assertTrue((revisions / active).is_dir())
        self.assertTrue((self.root / 'current').resolve().is_dir())
        self.assertTrue((revisions / ('%064x' % 10)).is_dir())
        self.assertFalse((revisions / ('%064x' % 1)).exists())
        self.assertTrue(link.is_symlink())
        self.assertTrue(external.is_dir())


if __name__ == '__main__':
    unittest.main()
