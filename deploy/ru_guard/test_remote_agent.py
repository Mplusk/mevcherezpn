import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

import remote_agent as agent
from test_outbound_install import Panel as OutboundPanel
from test_template_install import Panel as TemplatePanel, fixture as template_fixture

TX = 'a' * 32


class Panel:
    def __init__(self):
        self.outbounds = OutboundPanel()
        self.templates = TemplatePanel(template_fixture())

    def request(self, *args):
        return self.outbounds.request(*args)

    def get_templates(self):
        return self.templates.get_templates()

    def patch_template(self, *args):
        return self.templates.patch_template(*args)


def command(action, node='main'):
    return {'action': action, 'node': node, 'tx': TX}


class RemoteAgentTests(unittest.TestCase):
    def test_whitelist_rejects_unknown_fields_paths_and_child_outbound(self):
        bad = ({'action': 'shell', 'node': 'main', 'tx': TX},
               {'action': 'inspect', 'node': 'main', 'tx': '../etc'},
               {'action': 'inspect', 'node': 'main', 'tx': TX, 'cmd': 'bad'},
               command('stage_outbound', 'de'), command('inspect', 'unknown'))
        for item in bad:
            with self.subTest(item=item), self.assertRaises(ValueError):
                agent.parse_command(item)

    def test_inspect_returns_only_status_not_private_api_values(self):
        panel = Panel()
        panel.outbounds.rows[0]['password'] = 'sensitive-test-value'
        result = agent.inspect(panel)
        self.assertEqual(set(result), {'templates_ready', 'block_ready', 'direct_default',
                                      'exception_present', 'exception_enabled', 'exception_order_ok'})
        self.assertTrue(all(type(value) is bool for value in result.values()))
        self.assertNotIn('sensitive-test-value', str(result))

    def test_restore_refuses_active_asset_dependency(self):
        panel = Panel()
        panel.outbounds.rows.insert(0, {'id': 4, 'name': 'custom', 'mode': 'core_outbound',
                                        'enabled': True, 'geosites': ['geosite:ru-blocked']})
        with tempfile.TemporaryDirectory() as d, patch.object(agent, 'safe_existing_file', return_value=True), \
             patch.object(agent.transaction, 'restore_templates') as restore:
            with self.assertRaises(ValueError):
                agent.execute(command('restore', 'de'), panel, Path(d))
            restore.assert_not_called()

    def test_stage_templates_delegates_with_local_backup(self):
        panel = Panel()
        with tempfile.TemporaryDirectory() as d, patch.object(agent.template_install, 'install', return_value=True) as install:
            result = agent.execute(command('stage_templates', 'nl'), panel, Path(d))
            self.assertEqual(result, {'changed': True})
            self.assertEqual(install.call_args.args[1], Path(d) / 'templates.json')

    def test_status_does_not_return_log_content(self):
        class P:
            def request(self, *_):
                return {'running': ['apply'], 'text': 'sensitive-test-value'}
        self.assertEqual(agent.status(P()), {'busy': True})

    def test_apply_reports_completion_not_health(self):
        with tempfile.TemporaryDirectory() as d, patch.object(agent.apply_monitor, 'start_and_wait') as apply:
            result = agent.execute(command('apply'), Panel(), Path(d))
            self.assertEqual(result, {'finished': True})
            apply.assert_called_once()

    @unittest.skipUnless(os.name == 'posix' and os.geteuid() == 0, 'requires Linux root for ownership check')
    def test_backup_directory_and_file_permissions_are_checked(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            private = agent.tx_directory(TX, root=root / 'transactions')
            backup = private / 'templates.json'
            backup.write_text('{}')
            backup.chmod(0o600)
            self.assertTrue(agent.safe_existing_file(backup))
            backup.chmod(0o644)
            with self.assertRaises(ValueError):
                agent.safe_existing_file(backup)
            backup.unlink()
            backup.symlink_to(root / 'elsewhere')
            with self.assertRaises(ValueError):
                agent.safe_existing_file(backup)
            private.chmod(0o755)
            with self.assertRaises(ValueError):
                agent.secure_directory(private)


if __name__ == '__main__':
    unittest.main()
