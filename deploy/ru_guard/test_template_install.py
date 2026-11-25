import copy
import tempfile
import unittest
from pathlib import Path

from patch import BRIDGE, OLD_DOMAIN, OLD_IP, QUIC, SING_BRIDGE, SING_SNIFF, SING_REMOTE, XRAY_STRATEGY
from template_install import install, prepare


def fixture():
    return [
        {'id': 1, 'slug': 'xray/server/base/route', 'is_builtin': True,
         'builtin_override': False, 'content': XRAY_STRATEGY + BRIDGE + QUIC + OLD_IP + OLD_DOMAIN},
        {'id': 2, 'slug': 'hiddify-core/server/base/route', 'is_builtin': True,
         'builtin_override': False, 'content': SING_REMOTE + SING_SNIFF + SING_BRIDGE},
    ]


class Panel:
    def __init__(self, rows):
        self.rows = copy.deepcopy(rows)
        self.originals = copy.deepcopy(rows)
        self.calls = []
        self.fail_id = None

    def get_templates(self):
        return copy.deepcopy(self.rows)

    def patch_template(self, template_id, data):
        self.calls.append((template_id, data))
        row = next(x for x in self.rows if x['id'] == template_id)
        if data.get('builtin_override') is False:
            row.update(next(x for x in self.originals if x['id'] == template_id))
        else:
            row.update(data)
        if template_id == self.fail_id:
            self.fail_id = None
            raise RuntimeError('response lost')


class InstallerTests(unittest.TestCase):
    def test_installs_and_is_idempotent(self):
        panel = Panel(fixture())
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(install(panel, Path(d) / 'backup.json'))
            self.assertEqual(len(panel.calls), 2)
            self.assertFalse(install(panel, Path(d) / 'other.json'))
            self.assertEqual(len(panel.calls), 2)

    def test_failure_even_after_remote_commit_rolls_back(self):
        panel = Panel(fixture())
        panel.fail_id = 2
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RuntimeError):
                install(panel, Path(d) / 'backup.json')
            self.assertEqual(panel.rows, panel.originals)
            self.assertTrue((Path(d) / 'backup.json').exists())

    def test_custom_override_rejected_before_writes(self):
        rows = fixture()
        rows[0]['builtin_override'] = True
        panel = Panel(rows)
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                install(panel, Path(d) / 'backup.json')
            self.assertEqual(panel.calls, [])

    def test_missing_duplicate_or_broken_template_rejected(self):
        rows = fixture()
        for broken in (rows[:1], rows + rows[:1], [{**rows[0], 'content': 'broken'}, rows[1]]):
            with self.subTest(broken=broken):
                with self.assertRaises(ValueError):
                    prepare(broken)


if __name__ == '__main__':
    unittest.main()
