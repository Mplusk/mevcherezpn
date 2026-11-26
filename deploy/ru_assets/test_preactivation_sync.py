import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import preactivation_sync as sync


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.key = Path(self.tmp.name) / 'key'
        self.key.touch()

    def run_controller(self, node, manifests, execute=True):
        args = ['sync', '--key', str(self.key)] + (['--execute'] if execute else [])
        with mock.patch.object(sys, 'argv', args), \
             mock.patch.object(sync, 'run_node', side_effect=node), \
             mock.patch.object(sync, 'manifest', side_effect=manifests), \
             contextlib.redirect_stdout(io.StringIO()):
            sync.main()

    def test_all_preflight_checks_precede_any_sync_then_all_verify(self):
        calls = []
        self.run_controller(lambda host, key, expected, action: calls.append((host, action)),
                            [{'revision': 'fixed'}] * 7)
        self.assertEqual([action for _, action in calls],
                         ['check'] * 4 + ['sync'] * 4 + ['verify'] * 4)

    def test_changed_manifest_stops_before_first_sync(self):
        calls = []
        with self.assertRaises(RuntimeError):
            self.run_controller(lambda host, key, expected, action: calls.append((host, action)),
                                [{'revision': 'old'}, {'revision': 'new'}])
        self.assertEqual([action for _, action in calls], ['check'] * 4)

    def test_preflight_failure_stops_before_sync(self):
        calls = []
        def node(host, key, expected, action):
            calls.append(action)
            if host == sync.HOSTS[2]:
                raise RuntimeError('node unavailable')
        with self.assertRaises(RuntimeError):
            self.run_controller(node, [{'revision': 'fixed'}])
        self.assertEqual(calls, ['check'] * 3)

    def test_default_is_read_only(self):
        calls = []
        self.run_controller(lambda host, key, expected, action: calls.append(action),
                            [{'revision': 'fixed'}], execute=False)
        self.assertEqual(calls, ['check'] * 4)


if __name__ == '__main__':
    unittest.main()
