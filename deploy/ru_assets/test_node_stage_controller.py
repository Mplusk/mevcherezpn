import json
import unittest
from unittest.mock import patch
import node_stage_controller as controller


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.transaction = 'a' * 32
        self.seen = []
        self.directory = '/root/services/gamelife-vpn/ru-assets/.node-installer.ABC12345'

    def ssh(self, host, command, *, data=None, timeout=180):
        if command.startswith('umask '):
            return self.directory.encode()
        if command.startswith('tar '):
            self.assertNotIn(b'synthetic-secret', data)
            return b''
        if command.startswith('python3 '):
            request = json.loads(data)
            self.seen.append((host, request['action']))
            if request['action'] == 'stage':
                return json.dumps({'status': 'staged', 'transaction': self.transaction}).encode()
            self.assertEqual(request['transaction'], self.transaction)
            return b'{"status":"rolled_back"}'
        if command.startswith('rm '):
            return b''
        self.fail('unexpected SSH command')

    def test_probes_from_other_server_and_checks_monitor(self):
        with patch.object(controller, 'profile', return_value={'synthetic-secret': 'value'}), \
             patch.object(controller, 'independent_probe') as probe, \
             patch.object(controller, 'monitor_ok') as monitor, \
             patch.object(controller, 'ssh', side_effect=self.ssh):
            self.assertEqual(controller.main('nl')['status'], 'staged')
        self.assertEqual(probe.call_count, 2)
        probe.assert_any_call('nl', controller.MAIN)
        self.assertEqual(monitor.call_count, 2)
        self.assertEqual(self.seen, [(controller.HOSTS['nl'], 'stage')])

    def test_failure_after_stage_rolls_back_and_rechecks(self):
        with patch.object(controller, 'profile', return_value={'synthetic-secret': 'value'}), \
             patch.object(controller, 'independent_probe', side_effect=[None, RuntimeError(), None]) as probe, \
             patch.object(controller, 'monitor_ok'), patch.object(controller, 'ssh', side_effect=self.ssh):
            with self.assertRaisesRegex(RuntimeError, 'rolled back'):
                controller.main('pl')
        self.assertEqual(probe.call_count, 3)
        self.assertEqual(self.seen, [(controller.HOSTS['pl'], 'stage'),
                                     (controller.HOSTS['pl'], 'rollback')])

    def test_no_stage_if_baseline_probe_fails(self):
        with patch.object(controller, 'profile', return_value={}), \
             patch.object(controller, 'independent_probe', side_effect=RuntimeError()), \
             patch.object(controller, 'ssh') as ssh:
            with self.assertRaises(RuntimeError):
                controller.main('de')
        ssh.assert_not_called()

    def test_timer_stopped_after_stage_causes_verified_rollback(self):
        with patch.object(controller, 'profile', return_value={}), \
             patch.object(controller, 'independent_probe') as probe, \
             patch.object(controller, 'monitor_ok', side_effect=[None, RuntimeError()]), \
             patch.object(controller, 'ssh', side_effect=self.ssh):
            with self.assertRaises(controller.TimerCheckRolledBack):
                controller.main('de')
        self.assertEqual(probe.call_count, 3)
        self.assertEqual(self.seen, [(controller.HOSTS['de'], 'stage'),
                                     (controller.HOSTS['de'], 'rollback')])


if __name__ == '__main__':
    unittest.main()
