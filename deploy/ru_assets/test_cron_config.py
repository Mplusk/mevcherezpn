import json
import subprocess
import unittest
from unittest.mock import patch

import cron_config as c


class CronConfigTests(unittest.TestCase):
    def setUp(self):
        self.source = '{\n  "dns": {},\n  "routing": {}\n}'
        self.row = {'id': 6, 'side': 'server', 'core': 'xray', 'enable': True,
                    'is_builtin': True, 'builtin_override': False, 'version': '1.0.0',
                    'content': self.source}

    def test_hot_reload_schedule_is_top_level_and_idempotent(self):
        updated = c.transform(self.source)
        self.assertEqual(json.loads(updated)['geodata'], {'cron': c.CRON})
        self.assertEqual(c.transform(updated), updated)
        self.assertEqual(json.loads(updated)['routing'], {})

    def test_unknown_existing_overrides_and_duplicate_sections_fail(self):
        for text in ('{}', self.source + self.source, self.source.replace('"dns"', '"geodata"')):
            with self.subTest(text=text), self.assertRaises(ValueError):
                c.transform(text)
        with self.assertRaises(ValueError):
            c.select([dict(self.row, builtin_override=True)])
        with self.assertRaises(ValueError):
            c.select([self.row, self.row])

    def test_native_validation_receives_full_config_without_printing(self):
        calls = []
        def runner(args, **kwargs):
            calls.append((args, kwargs))
            if args[2] == 'gamelife-hiddify-panel-1' and 'cat' in args:
                return subprocess.CompletedProcess(args, 0, stdout=b'{"inbounds":[],"outbounds":[]}')
            return subprocess.CompletedProcess(args, 0)
        c.check_generated(runner)
        data = json.loads(calls[-1][1]['input'])
        self.assertEqual(data['geodata'], {'cron': c.CRON})
        self.assertEqual(calls[-1][1]['stdout'], subprocess.DEVNULL)
        self.assertEqual(calls[-1][1]['stderr'], subprocess.DEVNULL)

    def test_preview_checks_panel_render_and_native_validation(self):
        class Panel:
            def request(_, method, path, body=None):
                if method == 'GET':
                    return [self.row]
                self.assertEqual(path, 'proxy-base-configs/preview/')
                return {'ok': True, 'parsed': {'geodata': {'cron': c.CRON}}}
        with patch.object(c, 'check_generated') as check:
            row, updated = c.preview(Panel())
        self.assertEqual(row, self.row)
        self.assertEqual(json.loads(updated)['geodata'], {'cron': c.CRON})
        check.assert_called_once()


if __name__ == '__main__':
    unittest.main()
