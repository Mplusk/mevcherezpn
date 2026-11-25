import copy
import tempfile
import unittest
from pathlib import Path

from outbound_install import install, remove, matches
from test_template_install import fixture as template_fixture
from patch import transform_xray, transform_singbox


def fixture():
    return [
        {'id': 1, 'name': 'Block', 'mode': 'block', 'enabled': True,
         'is_builtin': True, 'is_default': False, 'sites': ['ru'],
         'geosites': ['geosite:category-ru', 'geoip:ru'],
         'rule_sets': ['https://x/geosite-category-ru.srs', 'https://x/geoip-ru.srs']},
        {'id': 2, 'name': 'WARP', 'mode': 'warp', 'enabled': False, 'is_default': False},
        {'id': 3, 'name': 'Direct', 'mode': 'direct', 'enabled': True,
         'is_builtin': True, 'is_default': True},
    ]


class Panel:
    def __init__(self):
        self.rows = fixture()
        self.original = copy.deepcopy(self.rows)
        self.fail_after = None
        self.mutations = []
        self.guards_installed = True

    def get_templates(self):
        rows = template_fixture()
        if self.guards_installed:
            for row, transform in zip(rows, (transform_xray, transform_singbox)):
                row['content'] = transform(row['content'])
                row['builtin_override'] = True
        return rows

    def request(self, method, path, data=None):
        if method == 'GET':
            return {'managed_by_parent': False, 'outbounds': copy.deepcopy(self.rows)}
        self.mutations.append((method, path))
        if method == 'POST':
            row = {**copy.deepcopy(data), 'id': 4, 'is_builtin': False, 'is_default': False}
            self.rows.insert(len(self.rows) - 1, row)
        elif method == 'PUT':
            by_id = {row['id']: row for row in self.rows}
            self.rows = [by_id[item] for item in data['ids']]
        elif method == 'PATCH':
            target = int(path.split('/')[1])
            next(row for row in self.rows if row['id'] == target).update(data)
        elif method == 'DELETE':
            target = int(path.split('/')[1])
            self.rows = [r for r in self.rows if r['id'] != target]
        else:
            raise ValueError(method)
        if self.fail_after == method:
            self.fail_after = None
            raise RuntimeError('response lost after commit')
        return {'ok': True}


class OutboundInstallTests(unittest.TestCase):
    def test_install_order_default_idempotence_and_remove(self):
        panel = Panel()
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(install(panel, Path(d) / 'before.json'))
            self.assertEqual([r['id'] for r in panel.rows], [4, 1, 2, 3])
            self.assertTrue(matches(panel.rows[0]))
            self.assertEqual(panel.rows[-1]['id'], 3)
            self.assertFalse(install(panel, Path(d) / 'unused.json'))
            self.assertTrue(remove(panel, Path(d) / 'remove.json'))
            self.assertFalse(panel.rows[0]['enabled'])
            self.assertEqual(panel.rows[1:], panel.original)
            self.assertFalse(remove(panel, Path(d) / 'unused-remove.json'))
            self.assertTrue(install(panel, Path(d) / 'reactivate.json'))
            self.assertTrue(panel.rows[0]['enabled'])
            self.assertEqual(panel.rows[0]['id'], 4)

    def test_lost_post_and_order_response_are_rolled_back(self):
        for step in ('POST', 'PUT'):
            with self.subTest(step=step), tempfile.TemporaryDirectory() as d:
                panel = Panel()
                panel.fail_after = step
                with self.assertRaises(RuntimeError):
                    install(panel, Path(d) / 'before.json')
                self.assertFalse(next(row for row in panel.rows if row['id'] == 4)['enabled'])
                self.assertEqual([row for row in panel.rows if row['id'] != 4], panel.original)
                self.assertTrue((Path(d) / 'before.json').exists())

    def test_lost_deactivation_response_restores_existing_id(self):
        panel = Panel()
        with tempfile.TemporaryDirectory() as d:
            install(panel, Path(d) / 'before.json')
            panel.fail_after = 'PATCH'
            with self.assertRaises(RuntimeError):
                remove(panel, Path(d) / 'remove.json')
            self.assertEqual(panel.rows[0]['id'], 4)
            self.assertTrue(panel.rows[0]['enabled'])

    def test_unexpected_existing_exception_is_not_touched(self):
        panel = Panel()
        panel.rows.insert(0, {'id': 4, 'name': 'GameLife blocked sites', 'mode': 'tor'})
        with tempfile.TemporaryDirectory() as d, self.assertRaises(ValueError):
            install(panel, Path(d) / 'before.json')
        self.assertEqual(panel.mutations, [])

    def test_missing_protection_or_default_prevents_all_writes(self):
        for change in ('block', 'default'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as d:
                panel = Panel()
                if change == 'block':
                    panel.rows[0]['geosites'] = []
                else:
                    panel.rows[-1]['is_default'] = False
                with self.assertRaises(ValueError):
                    install(panel, Path(d) / 'before.json')
                self.assertEqual(panel.mutations, [])

    def test_uninstalled_template_guards_prevent_all_writes(self):
        panel = Panel()
        panel.guards_installed = False
        with tempfile.TemporaryDirectory() as d, self.assertRaises(ValueError):
            install(panel, Path(d) / 'before.json')
        self.assertEqual(panel.mutations, [])


if __name__ == '__main__':
    unittest.main()
