import unittest
from unittest.mock import patch
from pathlib import Path
import tempfile
import os
from gateway import (BACKUP_BEGIN, BEGIN, END, backup_block, public_html,
                     install, site_block, transform)


ROOT = Path(__file__).resolve().parents[1]


class SiteTests(unittest.TestCase):
    def setUp(self):
        self.html = public_html()
        self.original = (ROOT / 'Caddyfile.standalone').read_text(encoding='utf-8')

    def test_page_contains_real_game_and_no_external_assets(self):
        self.assertIn('function nextGeneration', self.html)
        self.assertIn('id="world"', self.html)
        self.assertNotIn('/*__LIFE_SCRIPT__*/', self.html)
        self.assertNotRegex(self.html, r'<(?:script|link)[^>]+(?:src|href)="https?://')

    def test_preserves_other_handlers_and_is_idempotent(self):
        private = '\t# private route remains untouched\n\thandle /example-private/* {\n\t\trespond 404\n\t}\n'
        config = self.original.replace('\thandle / {', private + '\thandle / {')
        result = transform(config, self.html)
        self.assertIn(private, result)
        self.assertIn('reverse_proxy http://gamelife-vpn-origin:80', result)
        self.assertEqual(result.count(BEGIN), 1)
        self.assertEqual(result.count(END), 1)
        self.assertEqual(transform(result, self.html), result)

    def test_geodata_handler_is_exact_and_uses_public_directory(self):
        block = site_block(self.html)
        self.assertIn('handle /assets/geodata/geoip.dat {', block)
        self.assertIn('handle /assets/geodata/geosite.dat {', block)
        self.assertIn('handle /assets/geodata/geoip-ru-blocked.srs {', block)
        self.assertIn('handle /assets/geodata/geosite-ru-blocked.srs {', block)
        self.assertIn('root * /data/public-geodata/current', block)
        self.assertIn('rewrite * /geoip.dat', block)
        self.assertIn('rewrite * /geosite.dat', block)
        self.assertIn('file_server\n', block)
        self.assertNotIn('file_server browse', block)

    def test_rejects_ambiguous_or_partial_handlers(self):
        with self.assertRaises(ValueError):
            transform(self.original + self.original, self.html)
        with self.assertRaises(ValueError):
            transform(BEGIN + self.original, self.html)
        with self.assertRaises(ValueError):
            site_block('LIFE_PAGE 200')

    def test_backup_routes_are_restricted_and_idempotent(self):
        routes = {'version': 1, 'host': 'node.example.test', 'parent_host': 'parent.example.test',
                  'parent_path': 'parent_path', 'aliases': ['parent_path', 'legacy_path']}
        result = transform(self.original, self.html, routes)
        self.assertEqual(result.count(BACKUP_BEGIN), 1)
        self.assertIn('uri replace /legacy_path/ /parent_path/ 1', result)
        self.assertIn('reverse_proxy https://parent.example.test', result)
        self.assertEqual(transform(result, self.html, routes), result)
        for key, invalid in [('parent_host', 'host.test\nrespond 200'),
                             ('parent_path', '../admin'), ('aliases', ['evil/path'])]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                backup_block({**routes, key: invalid})

    def test_existing_site_alias_survives_updates(self):
        routes = {'version': 1, 'host': 'node.example.test', 'parent_host': 'parent.example.test',
                  'parent_path': 'parent_path', 'aliases': ['parent_path', 'legacy_path']}
        grouped = self.original.replace('\ngamelife.shop {',
                                        '\nnode.example.test, old.example.test {', 1)
        result = transform(grouped, self.html, routes)
        self.assertIn('\nnode.example.test, old.example.test {', result)
        self.assertEqual(result.count('log vpn {'), 1)
        self.assertIn('uri replace /legacy_path/ /parent_path/ 1', result)
        self.assertEqual(transform(result, self.html, routes), result)

    @unittest.skipUnless(hasattr(os, 'geteuid'), 'installer runs on Linux')
    def test_failed_health_check_restores_live_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            gateway = root / 'gateway'
            gateway.mkdir()
            config = gateway / 'Caddyfile'
            config.write_text(self.original, encoding='utf-8')
            calls = []
            def fake_docker(command, **_):
                calls.append(command)
            def fail():
                raise RuntimeError('synthetic health failure')
            with patch('gateway.docker', side_effect=fake_docker):
                with self.assertRaises(RuntimeError):
                    install(root, verify=fail)
            self.assertEqual(config.read_text(encoding='utf-8'), self.original)
            self.assertEqual(sum('reload' in call for call in calls), 2)
            self.assertEqual(len(list(gateway.glob('Caddyfile.before-life-*'))), 1)


if __name__ == '__main__':
    unittest.main()
