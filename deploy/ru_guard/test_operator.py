import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch


def load_operator():
    # Linux-only node module is replaced without contaminating other tests.
    fake = types.ModuleType('node_install')
    fake.fsync_dir = lambda _: None
    spec = importlib.util.spec_from_file_location('ru_guard_operator', Path(__file__).with_name('node_operator.py'))
    result = importlib.util.module_from_spec(spec)
    previous = sys.modules.get('node_install')
    sys.modules['node_install'] = fake
    try:
        spec.loader.exec_module(result)
    finally:
        if previous is None:
            del sys.modules['node_install']
        else:
            sys.modules['node_install'] = previous
    return result


class OperatorTests(unittest.TestCase):
    def setUp(self):
        self.op = load_operator()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.panel = Mock()

    def tearDown(self):
        self.temp.cleanup()

    def test_mount_failure_stops_before_mutation(self):
        op = self.op
        with patch.object(op, 'panel_for', return_value=self.panel), \
             patch.object(op, 'locks_for', return_value=[]), \
             patch.object(op, 'require_idle'), \
             patch.object(op, 'require_mount', side_effect=ValueError), \
             patch.object(op.template_install, 'install') as install:
            with self.assertRaises(ValueError):
                op.execute('stage', 'de', 'a' * 32)
            install.assert_not_called()

    def test_missing_bind_is_rejected_before_asset_validation(self):
        op = self.op
        with patch.object(op.installer, 'inspect_container', return_value={
                 'running': True, 'health': 'healthy', 'mounts': []}), \
             patch.object(op.node_install, 'verify_assets', create=True) as assets:
            with self.assertRaises(ValueError):
                op.require_mount()
            assets.assert_not_called()

    def test_stage_records_intent_before_writes_and_verifies(self):
        op = self.op
        events = []
        with patch.object(op, 'panel_for', return_value=self.panel), \
             patch.object(op, 'locks_for', return_value=[]), \
             patch.object(op, 'require_idle'), patch.object(op, 'require_mount'), \
             patch.object(op.remote_agent, 'ROOT', self.root), \
             patch.object(op.remote_agent, 'secure_directory'), \
             patch.object(op.remote_agent, 'require_no_enabled_asset_references'), \
             patch.object(op, 'journal_write', side_effect=lambda _d, s: events.append(s)), \
             patch.object(op.template_install, 'install', side_effect=lambda *_: events.append('patch') or True), \
             patch.object(op.apply_monitor, 'start_and_wait', side_effect=lambda *_: events.append('apply')), \
             patch.object(op, 'verify_staged', side_effect=lambda: events.append('verify')):
            op.execute('stage', 'de', 'a' * 32)
        self.assertEqual(events, ['stage-intent', 'patch', 'apply', 'verify', 'stage-complete'])

    def test_existing_transaction_is_never_replayed(self):
        op = self.op
        (self.root / ('a' * 32)).mkdir()
        with patch.object(op, 'panel_for', return_value=self.panel), \
             patch.object(op, 'locks_for', return_value=[]), \
             patch.object(op, 'require_idle'), patch.object(op, 'require_mount'), \
             patch.object(op.remote_agent, 'ROOT', self.root), \
             patch.object(op.remote_agent, 'secure_directory'), \
             patch.object(op.remote_agent, 'require_no_enabled_asset_references'), \
             patch.object(op.template_install, 'install') as install:
            with self.assertRaises(FileExistsError):
                op.execute('stage', 'de', 'a' * 32)
            install.assert_not_called()

    def test_rollback_refuses_enabled_outbound(self):
        op = self.op
        directory = self.root / ('a' * 32)
        directory.mkdir()
        with patch.object(op, 'panel_for', return_value=self.panel), \
             patch.object(op, 'locks_for', return_value=[]), \
             patch.object(op, 'require_idle'), patch.object(op, 'require_mount'), \
             patch.object(op.remote_agent, 'ROOT', self.root), \
             patch.object(op.remote_agent, 'secure_directory'), \
             patch.object(op, 'journal_read', return_value='stage-complete'), \
             patch.object(op.remote_agent, 'require_no_enabled_asset_references', side_effect=ValueError), \
             patch.object(op.transaction, 'restore_templates') as restore:
            with self.assertRaises(ValueError):
                op.execute('rollback', 'de', 'a' * 32)
            restore.assert_not_called()

    def test_apply_failure_leaves_intent_for_reconciliation(self):
        op = self.op
        events = []
        with patch.object(op, 'panel_for', return_value=self.panel), \
             patch.object(op, 'locks_for', return_value=[]), \
             patch.object(op, 'require_idle'), patch.object(op, 'require_mount'), \
             patch.object(op.remote_agent, 'ROOT', self.root), \
             patch.object(op.remote_agent, 'secure_directory'), \
             patch.object(op.remote_agent, 'require_no_enabled_asset_references'), \
             patch.object(op, 'journal_write', side_effect=lambda _d, s: events.append(s)), \
             patch.object(op.template_install, 'install', return_value=True), \
             patch.object(op.apply_monitor, 'start_and_wait', side_effect=RuntimeError), \
             patch.object(op.transaction, 'restore_templates') as restore:
            with self.assertRaises(RuntimeError):
                op.execute('stage', 'de', 'a' * 32)
            restore.assert_not_called()
        self.assertEqual(events, ['stage-intent'])

    def test_ambiguous_apply_blocks_rollback_until_idle(self):
        op = self.op
        with patch.object(op, 'panel_for', return_value=self.panel), \
             patch.object(op, 'locks_for', return_value=[]), \
             patch.object(op, 'require_idle', side_effect=RuntimeError), \
             patch.object(op, 'require_mount') as mount, \
             patch.object(op.transaction, 'restore_templates') as restore:
            with self.assertRaises(RuntimeError):
                op.execute('rollback', 'de', 'a' * 32)
            mount.assert_not_called()
            restore.assert_not_called()

    def test_rollback_wont_apply_without_active_mount(self):
        op = self.op
        with patch.object(op, 'panel_for', return_value=self.panel), \
             patch.object(op, 'locks_for', return_value=[]), \
             patch.object(op, 'require_idle'), \
             patch.object(op, 'require_mount', side_effect=ValueError), \
             patch.object(op.apply_monitor, 'start_and_wait') as apply, \
             patch.object(op.transaction, 'restore_templates') as restore:
            with self.assertRaises(ValueError):
                op.execute('rollback', 'de', 'a' * 32)
            apply.assert_not_called()
            restore.assert_not_called()


if __name__ == '__main__':
    unittest.main()
