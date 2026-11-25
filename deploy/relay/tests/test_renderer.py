import re
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from renderer import render, validate_routes


def fixture():
    return {'version': 1, 'host': 'gateway.example.test',
            'parent_host': 'parent.example.test', 'parent_path': 'parent_path',
            'aliases': ['parent_path', 'legacy_path', 'node_path']}


class RendererTests(unittest.TestCase):
    def test_renders_only_known_paths_and_parent_upstream(self):
        config = render(fixture())
        self.assertIn('https://gateway.example.test:8443', config)
        self.assertIn('reverse_proxy https://parent.example.test', config)
        self.assertIn('uri replace /legacy_path/ /parent_path/ 1', config)
        self.assertIn('uri replace /node_path/ /parent_path/ 1', config)
        self.assertIn('respond 404', config)
        self.assertNotIn('tls_insecure_skip_verify', config)
        self.assertIn('exclude http.log.error', config)
        self.assertIn('request delete', config)

    def test_rejects_injection_and_ambiguous_routes(self):
        for key, value in [('host', 'example.test\nrespond 200'),
                           ('parent_host', 'evil.example/test'),
                           ('parent_path', '../admin'),
                           ('aliases', ['parent_path', 'node/path']),
                           ('aliases', ['parent_path', 'parent_path']),
                           ('aliases', ['node_path'])]:
            with self.subTest(key=key, value=value):
                routes = fixture()
                routes[key] = value
                with self.assertRaises(ValueError):
                    validate_routes(routes)

    def test_rejects_unexpected_structure(self):
        routes = fixture()
        routes['extra'] = 'ignored?'
        with self.assertRaises(ValueError):
            validate_routes(routes)

    def test_matcher_requires_canonical_uuid(self):
        line = next(line for line in render(fixture()).splitlines()
                    if line.strip().startswith('@subscription'))
        pattern = line.strip().split(' ', 2)[2]
        self.assertIsNotNone(re.fullmatch(pattern, '/node_path/22222222-3333-4444-8888-555555555555/auto/'))
        self.assertIsNone(re.fullmatch(pattern, '/node_path/' + '-' * 36 + '/'))
        self.assertIsNone(re.fullmatch(pattern, '/admin_path/22222222-3333-4444-8888-555555555555/'))


if __name__ == '__main__':
    unittest.main()
