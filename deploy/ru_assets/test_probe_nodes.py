import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import node_install
import probe_nodes


def profiles():
    return {name: {'protocol': 'vless', 'settings': {'vnext': [{'address': host}]},
                   'streamSettings': {'network': 'ws', 'security': 'tls'}}
            for name, (host, _) in node_install.NODES.items()}


@unittest.skipUnless(os.name == 'posix', 'deployment target is Linux')
class ProbeTests(unittest.TestCase):
    def test_private_profiles_are_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'profiles.json'
            path.write_text(json.dumps(profiles()))
            path.chmod(0o600)
            self.assertEqual(set(probe_nodes.read_profiles(path)), {'de', 'pl', 'nl'})
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                probe_nodes.read_profiles(path)
            path.chmod(0o600)
            bad = profiles()
            bad['de']['settings']['vnext'][0]['address'] = 'unexpected.example'
            path.write_text(json.dumps(bad))
            with self.assertRaises(ValueError):
                probe_nodes.read_profiles(path)

    def test_probe_failure_is_not_treated_as_success(self):
        def copy(args, **kwargs):
            Path(args[-1]).write_bytes(b'executable fixture')
            return subprocess.CompletedProcess(args, 0)
        with patch.object(probe_nodes.subprocess, 'run', side_effect=copy):
            with patch.object(probe_nodes.node_install, 'probe', return_value=False):
                with self.assertRaisesRegex(RuntimeError, 'probe failed'):
                    probe_nodes.probe(['de'], profiles=profiles(), sleep=lambda _: None)


if __name__ == '__main__':
    unittest.main()
