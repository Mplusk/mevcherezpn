import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import cron_config
import enable_hot as e
import idle_gate


@unittest.skipUnless(os.name == 'posix', 'deployment target is Linux')
class EnableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.journal = self.root / 'journal.json'
        self.row = {'id': 6, 'side': 'server', 'core': 'xray', 'enable': True,
                    'is_builtin': True, 'builtin_override': False, 'version': '1.0.0',
                    'content': '{\n  "dns": {},\n  "routing": {}\n}'}
        self.updated = cron_config.transform(self.row['content'])
        self.calls = []
        outer = self
        class Panel:
            def request(_, method, path, body=None):
                outer.calls.append((method, path))
                if method == 'GET' and path == 'apply/':
                    return {'running': []}
                if method == 'GET':
                    return [dict(outer.row)]
                if method == 'PATCH':
                    if body == {'builtin_override': False}:
                        outer.row['content'] = outer.original_content
                    outer.row.update(body)
                    return dict(outer.row)
                raise AssertionError('unexpected request')
        self.panel = Panel()
        self.original_content = self.row['content']

    def tearDown(self):
        self.temp.cleanup()

    def common(self):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch.object(e, 'JOURNAL', self.journal))
        stack.enter_context(patch.object(e, 'panel_for', return_value=self.panel))
        stack.enter_context(patch.object(e, 'locks', return_value=[]))
        stack.enter_context(patch.object(e, 'other_nodes_available'))
        stack.enter_context(patch.object(e.probe_nodes, 'probe'))
        stack.enter_context(patch.object(e.cron_config, 'preview', return_value=(dict(self.row), self.updated)))
        stack.enter_context(patch.object(e.idle_gate, 'inspect_inbounds', return_value=([12345], [2000])))
        stack.enter_context(patch.object(e.idle_gate, 'active_connections', return_value=0))
        class Gate:
            def __init__(_, *args, **kwargs): pass
            def __enter__(_): return _
            def __exit__(_, *args): return None
            def confirm(_): return None
        stack.enter_context(patch.object(e.idle_gate, 'IdleGate', Gate))
        stack.enter_context(patch.object(e.apply_monitor, 'start_and_wait'))
        stack.enter_context(patch.object(e, 'verify_routes'))
        stack.enter_context(patch.object(e, 'verify_active'))
        stack.enter_context(patch.object(e, 'enable_service'))
        stack.enter_context(patch.object(e, 'restore_service'))
        stack.enter_context(patch.object(e.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)))
        return stack

    def test_idle_node_enables_schedule_and_hot_service(self):
        with self.common():
            self.assertEqual(e.run('de'), 'enabled')
        state = json.loads(self.journal.read_text())
        self.assertEqual(state['state'], 'applied')
        self.assertEqual(self.row['content'], self.updated)
        self.assertEqual(self.journal.stat().st_mode & 0o777, 0o600)

    def test_busy_node_never_writes_the_base(self):
        with self.common():
            with patch.object(e.idle_gate, 'IdleGate', side_effect=idle_gate.Busy('busy')):
                with self.assertRaises(idle_gate.Busy):
                    e.run('de')
        self.assertFalse(self.journal.exists())
        self.assertFalse(any(method == 'PATCH' for method, _ in self.calls))

    def test_ambiguous_panel_write_is_journalled_and_not_replayed(self):
        original = self.panel.request
        def ambiguous(method, path, body=None):
            result = original(method, path, body)
            if method == 'PATCH':
                raise TimeoutError('response lost')
            return result
        with self.common():
            self.panel.request = ambiguous
            with self.assertRaises(TimeoutError):
                e.run('de')
            self.assertEqual(json.loads(self.journal.read_text())['state'], 'uncertain')
            before = list(self.calls)
            with self.assertRaisesRegex(RuntimeError, 'reconciliation'):
                e.run('de')
            self.assertEqual(before, self.calls)

    def test_failed_verified_apply_rolls_back_while_gate_is_held(self):
        with self.common():
            with patch.object(e, 'verify_active', side_effect=ValueError('invalid live config')):
                with self.assertRaises(ValueError):
                    e.run('de')
        self.assertEqual(self.row['content'], self.original_content)
        self.assertFalse(self.row['builtin_override'])
        self.assertEqual(json.loads(self.journal.read_text())['state'], 'rolled_back')


if __name__ == '__main__':
    unittest.main()
