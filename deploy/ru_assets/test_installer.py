import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import installer


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.compose = self.root / 'compose.yaml'
        self.original = b'services:\n  panel:\n    volumes:\n      - panel:/opt/hiddify-manager/data\n'
        self.compose.write_bytes(self.original)
        self.assets = self.root / 'assets'
        self.assets.mkdir()
        self.env = self.root / '.env'
        self.env.write_text('SECRET=hidden')
        self.calls = []
        self.snapshot = {'project': 'gamelife-hiddify', 'service': 'panel',
                         'config_files': str(self.compose), 'working_dir': str(self.root),
                         'image': installer.IMAGE, 'running': True, 'health': 'healthy',
                         'ports': {'443/tcp': [{'HostPort': '28443'}]},
                         'mounts': [],
                         'networks': {name: {'Aliases': list(aliases), 'IPAMConfig': {}}
                                      for name, aliases in installer.NETWORKS.items()}}

    def tearDown(self):
        self.temp.cleanup()

    def runner(self, command):
        self.calls.append(command)
        if 'up' in command:
            self.snapshot['mounts'] = ([{'Destination': installer.TARGET, 'Type': 'bind',
                                         'RW': False, 'Source': str(self.assets)}]
                                       if installer.MARKER in self.compose.read_text() else [])

    @patch.object(installer, 'check_assets')
    def test_install_retains_recreate_file_and_has_private_backup(self, _):
        self.assertTrue(installer.install(self.compose, self.assets, self.env, self.runner, lambda: None,
                                          lambda: self.snapshot, lambda: False))
        updated = self.compose.read_text()
        self.assertIn(installer.MARKER, updated)
        self.assertIn(str(self.assets) + ':' + installer.TARGET + ':ro', updated)
        backups = list((self.assets / 'compose-backups').iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.original)
        self.assertFalse(installer.install(self.compose, self.assets, self.env, self.runner, lambda: None,
                                           lambda: self.snapshot, lambda: False))

    @patch.object(installer, 'check_assets')
    def test_failed_health_restores_original_and_recreates(self, _):
        checks = iter([False, True])
        def health():
            if not next(checks):
                raise ValueError('unhealthy')
        with self.assertRaisesRegex(RuntimeError, 'rollback completed'):
            installer.install(self.compose, self.assets, self.env, self.runner, health, lambda: self.snapshot, lambda: False)
        self.assertEqual(self.compose.read_bytes(), self.original)
        self.assertEqual(sum('up' in cmd for cmd in self.calls), 2)

    @patch.object(installer, 'check_assets')
    def test_unsafe_compose_does_not_change_original(self, _):
        self.compose.write_text('services: {}\n')
        with self.assertRaises(ValueError):
            installer.install(self.compose, self.assets, self.env, self.runner, lambda: None, lambda: self.snapshot, lambda: False)
        self.assertFalse(self.calls)
        self.assertEqual(self.compose.read_text(), 'services: {}\n')

    @patch.object(installer, 'check_assets')
    def test_unexpected_labels_or_networks_fail_before_changes(self, _):
        self.snapshot['project'] = 'other'
        with self.assertRaisesRegex(ValueError, 'labels'):
            installer.install(self.compose, self.assets, self.env, self.runner, lambda: None, lambda: self.snapshot, lambda: False)
        self.assertFalse(self.calls)
        self.assertEqual(self.compose.read_bytes(), self.original)

    @patch.object(installer, 'check_assets')
    def test_existing_template_dependency_prevents_any_mutation(self, _):
        with self.assertRaisesRegex(ValueError, 'depend'):
            installer.install(self.compose, self.assets, self.env, self.runner, lambda: None, lambda: self.snapshot, lambda: True)
        self.assertFalse(self.calls)
        self.assertEqual(self.compose.read_bytes(), self.original)

    @patch.object(installer, 'check_assets')
    def test_unknown_dependency_during_failure_does_not_remove_mount(self, _):
        states = iter([False, None])
        def health():
            raise ValueError('unhealthy')
        with self.assertRaisesRegex(RuntimeError, 'rollback unsafe'):
            installer.install(self.compose, self.assets, self.env, self.runner, health, lambda: self.snapshot, lambda: next(states))
        self.assertIn(installer.MARKER, self.compose.read_text())
        self.assertEqual(sum('up' in cmd for cmd in self.calls), 1)

    def test_missing_gateway_is_restored_with_expected_alias(self):
        installer.restore_gateway({'networks': {}}, self.runner)
        self.assertEqual(self.calls, [['docker', 'network', 'connect', '--alias', 'gamelife-vpn-origin',
                                      'gamelife-vpn-gateway', installer.CONTAINER]])

    def test_transform_is_idempotent(self):
        content = installer.transform(self.original.decode(), self.assets)
        self.assertEqual(content, installer.transform(content, self.assets))
        with self.assertRaises(ValueError):
            installer.transform(content.replace(installer.MARKER, ''), self.assets)


if __name__ == '__main__':
    unittest.main()
