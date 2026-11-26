import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

import builtin_geo as b
import sync
from reduce import reduce_dat
from test_sync import dat


def digest(data):
    return hashlib.sha256(data).hexdigest()


class MergeTests(unittest.TestCase):
    def test_updates_only_requested_entry_and_is_idempotent(self):
        original = dat({'RU', 'PRIVATE', 'US'})
        managed = dat({'RU', 'PRIVATE', 'RU-BLOCKED'}).replace(b'test', b'next')
        result = b.merge(original, managed, 'RU')
        self.assertEqual(reduce_dat(result, frozenset({'RU'})), reduce_dat(managed, frozenset({'RU'})))
        self.assertEqual(reduce_dat(result, frozenset({'PRIVATE', 'US'})),
                         reduce_dat(original, frozenset({'PRIVATE', 'US'})))
        self.assertEqual(b.merge(result, managed, 'RU'), result)

    def test_missing_duplicate_and_corrupt_records_are_rejected(self):
        valid = dat({'RU', 'PRIVATE'})
        with self.assertRaises(ValueError):
            b.merge(dat({'PRIVATE'}), valid, 'RU')
        with self.assertRaises(ValueError):
            b.merge(valid + dat({'RU'}), valid, 'RU')
        with self.assertRaises(ValueError):
            b.merge(valid, dat({'RU'})[:-1], 'RU')

    def test_native_validation_is_isolated(self):
        captured = []
        def runner(args, **kwargs):
            captured.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0)
        config = b'{"test":"private-fixture"}'
        b.validate_candidate(config, {'geoip.dat': b'a', 'geosite.dat': b'b'},
                             {'geoip.dat': b'c', 'geosite.dat': b'd'}, runner)
        args, kwargs = captured[0]
        self.assertEqual(args[args.index('--network') + 1], 'none')
        self.assertIn('--read-only', args)
        self.assertEqual(kwargs['stdout'], subprocess.DEVNULL)
        with tarfile.open(fileobj=io.BytesIO(kwargs['input'])) as tar:
            self.assertEqual(tar.extractfile('config.json').read(), config)
            self.assertEqual(tar.extractfile('gamelife/current/geosite.dat').read(), b'd')


@unittest.skipUnless(os.name == 'posix', 'deployment target is Linux')
class AtomicWriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.previous = {'geoip.dat': dat({'RU', 'PRIVATE'}),
                         'geosite.dat': dat({'CATEGORY-RU', 'PRIVATE'})}
        self.candidate = {name: data.replace(b'test', b'next') for name, data in self.previous.items()}
        for name, data in self.previous.items():
            (self.root / name).write_bytes(data)
        self.script = b.WRITE.replace("base='/opt/hiddify-manager/services/xray/bin'", 'base=' + repr(str(self.root)))

    def tearDown(self):
        self.tmp.cleanup()

    def run_writer(self, previous=None):
        previous = previous or self.previous
        arguments = [digest(previous[n]) for n in b.CODES] + [digest(self.candidate[n]) for n in b.CODES]
        return subprocess.run([sys.executable, '-c', self.script, *arguments], input=b.archive(self.candidate),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    def test_atomically_installs_valid_candidates(self):
        self.assertEqual(self.run_writer().returncode, 0)
        for name, data in self.candidate.items():
            self.assertEqual((self.root / name).read_bytes(), data)

    def test_changed_default_is_not_overwritten(self):
        changed = b'unexpected concurrent change'
        (self.root / 'geosite.dat').write_bytes(changed)
        self.assertNotEqual(self.run_writer().returncode, 0)
        self.assertEqual((self.root / 'geoip.dat').read_bytes(), self.previous['geoip.dat'])
        self.assertEqual((self.root / 'geosite.dat').read_bytes(), changed)

    def test_symlink_is_not_followed(self):
        link = self.root / 'geoip.dat'
        link.unlink()
        link.symlink_to(self.root / 'geosite.dat')
        self.assertNotEqual(self.run_writer().returncode, 0)
        self.assertEqual((self.root / 'geosite.dat').read_bytes(), self.previous['geosite.dat'])


@unittest.skipUnless(os.name == 'posix', 'deployment target is Linux')
class BuiltinUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        revisions = self.root / 'revisions'; revisions.mkdir()
        target = revisions / ('a' * 64); target.mkdir()
        (self.root / 'current').symlink_to('revisions/' + 'a' * 64)
        (self.root / '.sync.lock').write_bytes(b'')
        (self.root / '.sync.lock').chmod(0o600)
        for name, codes in sync.RULES.items():
            (target / name).write_bytes(dat(codes).replace(b'test', b'new!'))
        self.default = {'geoip.dat': dat({'RU', 'PRIVATE', 'US'}).replace(b'test', b'old!'),
                        'geosite.dat': dat({'CATEGORY-RU', 'PRIVATE', 'OTHER'}).replace(b'test', b'old!')}
        self.calls = []
        self.runtime = {'pid': 42}

    def tearDown(self):
        self.tmp.cleanup()

    def runner(self, args, **kwargs):
        self.calls.append((args, kwargs))
        output = b''
        if args[:2] == ['docker', 'inspect']:
            output = ('e' * 64).encode()
        elif args[:2] == ['docker', 'exec'] and 'cat' in args:
            output = b'{"test":"private-fixture"}' if args[-1] == b.CONFIG else self.default[Path(args[-1]).name]
        return subprocess.CompletedProcess(args, 0, stdout=output)

    def test_updates_ru_categories_with_validated_complete_config(self):
        self.assertTrue(b.update(self.root, inspect=lambda: dict(self.runtime), runner=self.runner))
        writer = [item for item in self.calls if item[0][:3] == ['docker', 'exec', '-i']]
        self.assertEqual(len(writer), 1)
        with tarfile.open(fileobj=io.BytesIO(writer[0][1]['input'])) as tar:
            for name, code in b.CODES.items():
                actual = tar.extractfile(name).read()
                managed = (self.root / 'current' / name).read_bytes()
                self.assertEqual(reduce_dat(actual, frozenset({code})), reduce_dat(managed, frozenset({code})))
        self.assertTrue(any(item[0][:2] == ['docker', 'run'] for item in self.calls))

    def test_process_change_after_validation_prevents_write(self):
        identities = iter(({'pid': 42}, {'pid': 43}))
        with self.assertRaises(RuntimeError):
            b.update(self.root, inspect=lambda: next(identities), runner=self.runner)
        self.assertFalse(any(item[0][:3] == ['docker', 'exec', '-i'] for item in self.calls))

    def test_unchanged_categories_need_no_write(self):
        for name, code in b.CODES.items():
            managed = (self.root / 'current' / name).read_bytes()
            self.default[name] = b.merge(self.default[name], managed, code)
        self.assertFalse(b.update(self.root, inspect=lambda: dict(self.runtime), runner=self.runner))
        self.assertFalse(any(item[0][:3] == ['docker', 'exec', '-i'] for item in self.calls))


if __name__ == '__main__':
    unittest.main()
