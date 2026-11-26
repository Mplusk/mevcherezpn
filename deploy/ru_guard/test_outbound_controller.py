import unittest

import outbound_controller as controller

TX = 'a' * 32


class ControllerTests(unittest.TestCase):
    def test_activation_checks_every_node_before_write_and_after(self):
        calls = []
        def call(action, node, tx):
            calls.append((action, node, tx))
            return True
        controller.run('activate', TX, call, sleep=lambda _: None)
        self.assertEqual(calls[:4], [('verify-staged', node, TX) for node in controller.NODES])
        self.assertEqual(calls[4], ('activate', 'main', TX))
        self.assertEqual(calls[5:], [('verify-active', node, TX) for node in controller.NODES])

    def test_missing_staged_node_never_activates(self):
        calls = []
        def call(action, node, tx):
            calls.append((action, node))
            return node != 'pl'
        with self.assertRaises(controller.Incomplete):
            controller.run('activate', TX, call, sleep=lambda _: None)
        self.assertFalse(any(action == 'activate' for action, _ in calls))

    def test_ambiguous_activate_does_not_auto_rollback(self):
        calls = []
        def call(action, node, tx):
            calls.append((action, node))
            return action != 'activate'
        with self.assertRaises(controller.Incomplete):
            controller.run('activate', TX, call, sleep=lambda _: None)
        self.assertFalse(any(action == 'disable' for action, _ in calls))

    def test_rollback_disables_first_then_checks_every_node(self):
        calls = []
        def call(action, node, tx):
            calls.append((action, node))
            return True
        controller.run('rollback', TX, call, sleep=lambda _: None)
        self.assertEqual(calls[0], ('disable', 'main'))
        self.assertEqual(calls[1:], [('verify-staged', node) for node in controller.NODES])

    def test_sync_retry_only_rechecks_failed_nodes(self):
        calls = []
        def call(action, node, tx):
            calls.append((action, node))
            return node != 'pl' or calls.count((action, node)) >= 2
        controller.run('rollback', TX, call, sleep=lambda _: None)
        self.assertEqual(calls.count(('verify-staged', 'pl')), 2)
        self.assertEqual(calls.count(('verify-staged', 'de')), 1)


if __name__ == '__main__':
    unittest.main()
