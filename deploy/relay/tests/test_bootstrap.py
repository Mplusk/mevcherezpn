import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bootstrap import bootstrap, make_routes
from renderer import _UUID, render
from test_renderer import fixture


SOURCE = Path(__file__).resolve().parents[1]
MANIFEST = {'parent_host': 'parent.example.test', 'parent_path': 'parent_path',
            'nl_path': 'legacy_path'}


class RouteManifestTests(unittest.TestCase):
    def test_protected_routes_include_all_three_aliases(self):
        routes = make_routes(MANIFEST, 'gateway.example.test', 'node_path', 'admin_path')
        self.assertEqual(routes, fixture())

    def test_admin_route_cannot_be_used_as_subscription(self):
        with self.assertRaises(ValueError):
            make_routes(MANIFEST, 'gateway.example.test', 'node_path', 'node_path')


@unittest.skipUnless(hasattr(os, 'geteuid'), 'bootstrap runs on Linux')
class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.directory = self.root / 'subscription-relay-backup'
        self.directory.mkdir(mode=0o700)
        state = self.root / 'node-access.json'
        state.write_text(json.dumps({'owner_url': 'https://gateway.example.test/admin_path/'
                                     '11111111-2222-4333-8444-555555555555/'}))
        state.chmod(0o600)
        legacy = render(fixture(), privacy_logs=False).replace(_UUID, '[0-9a-fA-F-]{36}')
        (self.directory / 'Caddyfile').write_text(legacy)
        self.response = (200, json.dumps({'categories': [{'fields': [
            {'key': 'proxy_path_client', 'value': 'node_path'}]}]}).encode())

    def tearDown(self):
        self.temp.cleanup()

    @patch('bootstrap.request')
    def test_existing_relay_is_registered_without_changing_its_config(self, request):
        request.return_value = self.response
        before = (self.directory / 'Caddyfile').read_bytes()
        self.assertEqual(bootstrap(MANIFEST, self.root, SOURCE), fixture())
        self.assertEqual((self.directory / 'Caddyfile').read_bytes(), before)
        self.assertEqual(json.loads((self.directory / 'routes.json').read_text()), fixture())
        self.assertEqual(bootstrap(MANIFEST, self.root, SOURCE), fixture())
        self.assertTrue((self.root / 'relay-ci' / 'dispatcher.py').is_file())

    @patch('bootstrap.request')
    def test_unknown_manual_config_is_not_overwritten(self, request):
        request.return_value = self.response
        (self.directory / 'Caddyfile').write_text('custom config')
        with self.assertRaises(RuntimeError):
            bootstrap(MANIFEST, self.root, SOURCE)
        self.assertFalse((self.directory / 'routes.json').exists())
        self.assertEqual((self.directory / 'Caddyfile').read_text(), 'custom config')


if __name__ == '__main__':
    unittest.main()
