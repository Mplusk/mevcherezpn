import hashlib
import io
import json
import subprocess
import os
import shlex
import tarfile
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

import update


def revision(files):
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    return hashlib.sha256(b'gamelife-geo-manifest-v1\x00' + json.dumps(hashes, sort_keys=True).encode()).hexdigest()


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.files = {'geoip.dat': b'first test data', 'geosite.dat': b'second test data',
                      'geosite-ru-blocked.srs': b'SRS\x01' + zlib.compress(b'site rule'),
                      'geoip-ru-blocked.srs': b'SRS\x01' + zlib.compress(b'ip rule')}

    def test_archives_only_public_fixed_files(self):
        payload = update.make_tar(self.files)
        with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
            self.assertEqual(archive.getnames(), list(update.DAT_NAMES + update.SRS_NAMES) + ['manifest.json'])
            self.assertTrue(all(member.isfile() for member in archive.getmembers()))
            for member in archive.getmembers():
                content = archive.extractfile(member).read()
                if member.name == 'manifest.json':
                    manifest = json.loads(content)
                    self.assertEqual(manifest['version'], 1)
                    self.assertEqual(manifest['revision'], revision(self.files))
                    self.assertEqual(manifest['sha256'], {name: hashlib.sha256(data).hexdigest()
                                                          for name, data in self.files.items()})
                else:
                    self.assertEqual(content, self.files[member.name])

    def test_push_verifies_revision_and_transfers_expected_hashes(self):
        calls = []
        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0)
        update.push(self.files, revision(self.files), runner)
        args, kwargs = calls[0]
        self.assertEqual(args[:4], ['docker', 'exec', '-i', update.CONTAINER])
        self.assertEqual(args[-5:-1], [hashlib.sha256(self.files[name]).hexdigest()
                                     for name in update.DAT_NAMES + update.SRS_NAMES])
        self.assertEqual(args[-1], hashlib.sha256(update.manifest_bytes(self.files)).hexdigest())
        self.assertTrue(kwargs['input'])
        with self.assertRaises(ValueError):
            update.push(self.files, 'a' * 64, runner)
        self.assertEqual(len(calls), 1)

    def test_manifest_is_deterministic_and_changes_with_files(self):
        first = update.manifest_bytes(self.files)
        self.assertEqual(first, update.manifest_bytes(dict(reversed(list(self.files.items())))))
        changed = dict(self.files, **{'geoip.dat': b'new data'})
        self.assertNotEqual(first, update.manifest_bytes(changed))
        with self.assertRaises(ValueError):
            update.manifest_bytes({'geoip.dat': b'incomplete'})

    @unittest.skipIf(os.name == 'nt', 'container publisher uses a POSIX shell')
    def test_shell_publishes_manifest_atomically_and_rejects_tampering(self):
        with tempfile.TemporaryDirectory(prefix='geodata-publisher-') as directory:
            root = Path(directory) / 'public-geodata'
            script = update.CONTAINER_SCRIPT.replace('root=/data/public-geodata', 'root=' + shlex.quote(str(root)), 1)
            def command(files):
                args = [revision(files)] + [hashlib.sha256(files[name]).hexdigest()
                                            for name in update.DAT_NAMES + update.SRS_NAMES]
                args.append(hashlib.sha256(update.manifest_bytes(files)).hexdigest())
                return ['sh', '-c', script, '--', *args]
            # An existing pre-manifest revision may contain the exact same four
            # data files; the manifest protocol must publish a new revision.
            old_hashes = {name: hashlib.sha256(data).hexdigest() for name, data in self.files.items()}
            old_revision = hashlib.sha256(json.dumps(old_hashes, sort_keys=True).encode()).hexdigest()
            old_dir = root / 'revisions' / old_revision
            old_dir.mkdir(parents=True)
            for name, data in self.files.items():
                (old_dir / name).write_bytes(data)
            (root / 'current').symlink_to('revisions/' + old_revision)
            self.assertNotEqual(old_revision, revision(self.files))
            result = subprocess.run(command(self.files), input=update.make_tar(self.files), capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            current = root / 'current'
            self.assertTrue(current.is_symlink())
            first = os.readlink(current)
            self.assertNotEqual(first, 'revisions/' + old_revision)
            self.assertFalse((old_dir / 'manifest.json').exists())
            self.assertEqual(json.loads((current / 'manifest.json').read_bytes())['revision'], revision(self.files))
            changed = dict(self.files, **{'geoip.dat': b'new data'})
            with tarfile.open(fileobj=io.BytesIO(update.make_tar(changed))) as archive:
                items = [(item.name, archive.extractfile(item).read()) for item in archive]
            payload = io.BytesIO()
            with tarfile.open(fileobj=payload, mode='w') as archive:
                for name, data in items:
                    if name == 'manifest.json':
                        data = b'{"version":1}'
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
            result = subprocess.run(command(changed), input=payload.getvalue(), capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(os.readlink(current), first)

    def test_failed_publication_is_failure(self):
        def failure(args, **kwargs):
            return subprocess.CompletedProcess(args, 1)
        with self.assertRaises(RuntimeError):
            update.push(self.files, revision(self.files), failure)

    def test_unknown_source_rejected_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(update.urllib.request, 'build_opener') as opener:
                with self.assertRaises(ValueError):
                    update.download('https://attacker.invalid/geosite.dat', Path(directory) / 'file')
                opener.assert_not_called()

    def test_bad_or_truncated_srs_never_published(self):
        for data in (b'', b'SRS\x02bad', b'SRS\x01bad', self.files['geoip-ru-blocked.srs'][:-1],
                     self.files['geoip-ru-blocked.srs'] + b'trailing'):
            files = dict(self.files, **{'geoip-ru-blocked.srs': data})
            with self.assertRaises((ValueError, zlib.error)):
                update.push(files, revision(files), lambda *args, **kwargs: self.fail('published invalid SRS'))

    def test_download_failure_never_calls_publication(self):
        with patch.object(update, 'download', side_effect=TimeoutError), patch.object(update, 'push') as push:
            with self.assertRaises(TimeoutError):
                update.run()
            push.assert_not_called()

    def test_redirect_rejected(self):
        with self.assertRaises(ValueError):
            update.NoRedirect().redirect_request(None, None, 302, None, None, 'https://example.invalid')

if __name__ == '__main__':
    unittest.main()
