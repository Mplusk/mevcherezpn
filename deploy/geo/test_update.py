import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

import update


def revision(files):
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.files = {'geoip.dat': b'first test data', 'geosite.dat': b'second test data',
                      'geosite-ru-blocked.srs': b'SRS\x01' + zlib.compress(b'site rule'),
                      'geoip-ru-blocked.srs': b'SRS\x01' + zlib.compress(b'ip rule')}

    def test_archives_only_public_fixed_files(self):
        payload = update.make_tar(self.files)
        with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
            self.assertEqual(archive.getnames(), list(update.DAT_NAMES + update.SRS_NAMES))
            self.assertTrue(all(member.isfile() for member in archive.getmembers()))
            for member in archive.getmembers():
                self.assertEqual(archive.extractfile(member).read(), self.files[member.name])

    def test_push_verifies_revision_and_transfers_expected_hashes(self):
        calls = []
        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0)
        update.push(self.files, revision(self.files), runner)
        args, kwargs = calls[0]
        self.assertEqual(args[:4], ['docker', 'exec', '-i', update.CONTAINER])
        self.assertEqual(args[-4:], [hashlib.sha256(self.files[name]).hexdigest()
                                     for name in update.DAT_NAMES + update.SRS_NAMES])
        self.assertTrue(kwargs['input'])
        with self.assertRaises(ValueError):
            update.push(self.files, 'a' * 64, runner)
        self.assertEqual(len(calls), 1)

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
