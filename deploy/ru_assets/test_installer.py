import tempfile
import unittest
import copy
import json
from pathlib import Path
from unittest.mock import patch

import installer


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.compose = self.root / 'compose.yaml'
        self.original = (b'services:\n  panel:\n    volumes:\n      - panel:/opt/hiddify-manager/data\n'
                         b'    networks:\n      ingress: {}\n      storage: {}\n'
                         b'networks:\n  ingress:\n  storage:\n    internal: true\n')
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
            active = installer.MARKER in self.compose.read_text()
            self.snapshot['mounts'] = ([{'Destination': installer.TARGET, 'Type': 'bind',
                                         'RW': False, 'Source': str(self.assets)}]
                                       if active else [])
            self.snapshot['networks']['gamelife-vpn-gateway']['Aliases'] = (
                ['gamelife-vpn-origin', 'panel', 'gamelife-hiddify-panel-1'] if active else ['gamelife-vpn-origin'])

    def install(self, *args):
        return installer.install(*args, baseline=lambda *_: None)

    @patch.object(installer, 'check_assets')
    def test_install_retains_recreate_file_and_has_private_backup(self, _):
        self.assertTrue(self.install(self.compose, self.assets, self.env, self.runner, lambda: None,
                                          lambda: self.snapshot, lambda: False))
        updated = self.compose.read_text()
        self.assertIn(installer.MARKER, updated)
        self.assertIn(str(self.assets) + ':' + installer.TARGET + ':ro', updated)
        backups = list((self.assets / 'compose-backups').iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.original)
        self.assertFalse(self.install(self.compose, self.assets, self.env, self.runner, lambda: None,
                                           lambda: self.snapshot, lambda: False))

    @patch.object(installer, 'check_assets')
    def test_failed_health_restores_original_and_recreates(self, _):
        checks = iter([False, True])
        def health():
            if not next(checks):
                raise ValueError('unhealthy')
        with self.assertRaisesRegex(RuntimeError, 'rollback completed'):
            self.install(self.compose, self.assets, self.env, self.runner, health, lambda: self.snapshot, lambda: False)
        self.assertEqual(self.compose.read_bytes(), self.original)
        self.assertEqual(sum('up' in cmd for cmd in self.calls), 2)

    @patch.object(installer, 'check_assets')
    def test_unsafe_compose_does_not_change_original(self, _):
        self.compose.write_text('services: {}\n')
        with self.assertRaises(ValueError):
            self.install(self.compose, self.assets, self.env, self.runner, lambda: None, lambda: self.snapshot, lambda: False)
        self.assertFalse(self.calls)
        self.assertEqual(self.compose.read_text(), 'services: {}\n')

    @patch.object(installer, 'check_assets')
    def test_unexpected_labels_or_networks_fail_before_changes(self, _):
        self.snapshot['project'] = 'other'
        with self.assertRaisesRegex(ValueError, 'labels'):
            self.install(self.compose, self.assets, self.env, self.runner, lambda: None, lambda: self.snapshot, lambda: False)
        self.assertFalse(self.calls)
        self.assertEqual(self.compose.read_bytes(), self.original)

    @patch.object(installer, 'check_assets')
    def test_existing_template_dependency_prevents_any_mutation(self, _):
        with self.assertRaisesRegex(ValueError, 'depend'):
            self.install(self.compose, self.assets, self.env, self.runner, lambda: None, lambda: self.snapshot, lambda: True)
        self.assertFalse(self.calls)
        self.assertEqual(self.compose.read_bytes(), self.original)

    @patch.object(installer, 'check_assets')
    def test_unknown_dependency_during_failure_does_not_remove_mount(self, _):
        states = iter([False, None])
        def health():
            raise ValueError('unhealthy')
        with self.assertRaisesRegex(RuntimeError, 'rollback unsafe'):
            self.install(self.compose, self.assets, self.env, self.runner, health, lambda: self.snapshot, lambda: next(states))
        self.assertIn(installer.MARKER, self.compose.read_text())
        self.assertEqual(sum('up' in cmd for cmd in self.calls), 1)

    def test_missing_gateway_is_restored_with_expected_alias(self):
        installer.restore_gateway({'networks': {}}, self.runner)
        self.assertEqual(self.calls, [['docker', 'network', 'connect', '--alias', 'gamelife-vpn-origin',
                                      'gamelife-vpn-gateway', installer.CONTAINER]])

    def baseline_data(self):
        self.snapshot['mounts'] = [{'Type': 'volume', 'Name': 'gamelife-hiddify_panel',
                                    'Destination': '/opt/hiddify-manager/data', 'RW': True}]
        self.snapshot['ports'] = {'443/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '28443'}]}
        return {'services': {'panel': {'image': installer.IMAGE,
                                      'environment': {'DB_PASSWORD': 'dummy-secret'},
                                      'ports': [{'host_ip': '127.0.0.1', 'target': 443, 'published': '28443'}],
                                      'volumes': [{'type': 'volume', 'source': 'panel',
                                                   'target': '/opt/hiddify-manager/data'}]}},
                'volumes': {'panel': {'name': 'gamelife-hiddify_panel'}}}

    def test_runtime_comparison_checks_env_image_ports_and_mounts_without_leaks(self):
        config = self.baseline_data()
        def run(cfg, env):
            def get(args):
                return json.dumps(cfg if 'compose' in args else ['OTHER=image-default'] if 'image' in args else env).encode()
            return installer.compare_runtime(self.compose, self.env, self.snapshot, get)
        run(config, ['DB_PASSWORD=dummy-secret', 'OTHER=image-default'])
        wrong = copy.deepcopy(config); wrong['services']['panel']['image'] = 'other'
        for cfg, env in [(config, ['DB_PASSWORD=wrong-secret', 'OTHER=image-default']), (wrong, ['DB_PASSWORD=dummy-secret', 'OTHER=image-default']),
                         (config, ['DB_PASSWORD=dummy-secret', 'OTHER=image-default', 'UNDECLARED=unexpected'])]:
            with self.assertRaises(ValueError) as caught:
                run(cfg, env)
            self.assertNotIn('secret', str(caught.exception))
        wrong = copy.deepcopy(config); wrong['services']['panel']['ports'][0]['published'] = '28444'
        with self.assertRaises(ValueError):
            run(wrong, ['DB_PASSWORD=dummy-secret', 'OTHER=image-default'])
        wrong = copy.deepcopy(config); wrong['services']['panel']['volumes'][0]['target'] = '/wrong'
        with self.assertRaises(ValueError):
            run(wrong, ['DB_PASSWORD=dummy-secret', 'OTHER=image-default'])

    def test_transform_is_idempotent(self):
        content = installer.transform(self.original.decode(), self.assets)
        self.assertEqual(content, installer.transform(content, self.assets))
        with self.assertRaises(ValueError):
            installer.transform(content.replace(installer.MARKER, ''), self.assets)


if __name__ == '__main__':
    unittest.main()
