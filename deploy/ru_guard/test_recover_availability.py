import copy
import sys
import types
import unittest
from unittest.mock import patch

try:
    import fcntl  # noqa: F401
except ModuleNotFoundError:  # Unit tests can also run on the Windows controller.
    sys.modules['fcntl'] = types.ModuleType('fcntl')

import recover_availability as recovery


def example():
    return [
        {'id': 1, 'mode': 'block', 'is_builtin': True, 'enabled': True,
         'is_default': False, 'domestic': False, 'sites': ['ru'],
         'geosites': ['geosite:category-ru', 'geoip:ru'],
         'rule_sets': ['https://example.test/geosite-category-ru.srs',
                       'https://example.test/geoip-ru.srs']},
        {'id': 2, 'mode': 'direct', 'enabled': True, 'is_default': True},
    ]


class Panel:
    def __init__(self, rows):
        self.rows = copy.deepcopy(rows)
        self.calls = []

    def request(self, method, path, data=None):
        self.calls.append((method, path))
        if method == 'GET':
            return {'managed_by_parent': False, 'outbounds': copy.deepcopy(self.rows)}
        if method == 'PATCH':
            target_id = int(path.split('/')[1])
            next(row for row in self.rows if row['id'] == target_id).update(data)
            return {}
        raise AssertionError('unexpected panel action')


class AvailabilityTests(unittest.TestCase):
    def test_backs_up_before_disabling_only_the_ru_deny(self):
        panel = Panel(example())
        captured = []
        with patch.object(recovery, 'backup', side_effect=lambda rows, _directory: captured.append(copy.deepcopy(rows))):
            self.assertTrue(recovery.disable_ru_only_block(panel, 'unused'))
        self.assertEqual(captured, [example()])
        self.assertEqual([method for method, _ in panel.calls], ['GET', 'PATCH', 'GET'])
        self.assertFalse(panel.rows[0]['enabled'])
        self.assertTrue(panel.rows[1]['enabled'])
        self.assertTrue(panel.rows[1]['is_default'])

    def test_repeated_run_does_not_write(self):
        rows = example()
        rows[0]['enabled'] = False
        panel = Panel(rows)
        with patch.object(recovery, 'backup') as save:
            self.assertFalse(recovery.disable_ru_only_block(panel, 'unused'))
        save.assert_not_called()
        self.assertEqual([method for method, _ in panel.calls], ['GET'])

    def test_does_not_disable_unrelated_blocks(self):
        for field, value in [('sites', 'example.com'), ('geosites', 'geoip:private'),
                             ('rule_sets', 'https://example.test/malware.srs')]:
            with self.subTest(field=field):
                rows = example()
                rows[0][field].append(value)
                panel = Panel(rows)
                with patch.object(recovery, 'backup') as save, self.assertRaises(ValueError):
                    recovery.disable_ru_only_block(panel, 'unused')
                save.assert_not_called()
                self.assertEqual([method for method, _ in panel.calls], ['GET'])

    def test_refuses_missing_or_concurrently_changed_default(self):
        rows = example()
        rows[-1]['is_default'] = False
        with self.assertRaises(ValueError):
            recovery.checked_rows({'managed_by_parent': False, 'outbounds': rows})

        class ChangedPanel(Panel):
            def request(self, method, path, data=None):
                response = super().request(method, path, data)
                if method == 'PATCH':
                    self.rows.append({'id': 3, 'mode': 'direct', 'enabled': True,
                                      'is_default': False})
                return response

        with patch.object(recovery, 'backup'), self.assertRaises((RuntimeError, ValueError)):
            recovery.disable_ru_only_block(ChangedPanel(example()), 'unused')


if __name__ == '__main__':
    unittest.main()
