import json
import subprocess
import unittest

import idle_gate as g


class GateTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.counts = []

    def run_command(self, args, **kwargs):
        self.calls.append(args)
        if 'ss' in args:
            data = self.counts.pop(0) if self.counts else b''
            return subprocess.CompletedProcess(args, 0, stdout=data)
        return subprocess.CompletedProcess(args, 0, stdout=b'')

    def test_blocks_only_new_syn_and_has_watchdog_before_gate(self):
        token = 'GL-GEO-0123456789abcdef'
        with g.IdleGate([12345], [], runner=self.run_command, token=token, sleep=lambda _: None):
            pass
        commands = [a for a in self.calls if 'iptables' in a or ('-d' in a and 'sh' in a)]
        self.assertEqual(len(commands), 3)
        self.assertIn('-d', commands[0])
        self.assertIn('--syn', commands[1])
        self.assertNotIn('--ctstate', commands[1])
        self.assertIn('-I', commands[1])
        self.assertIn('-D', commands[2])

    def test_busy_node_is_never_gated(self):
        self.counts = [b'active session\n']
        with self.assertRaises(g.Busy):
            with g.IdleGate([12345], [], runner=self.run_command, sleep=lambda _: None):
                pass
        self.assertFalse(any('iptables' in args for args in self.calls))

    def test_connection_arriving_during_gate_removes_it_without_apply(self):
        self.counts = [b'', b'', b'', b'new session\n']
        with self.assertRaises(g.Busy):
            with g.IdleGate([12345], [], runner=self.run_command, sleep=lambda _: None):
                self.fail('must not apply while a user is active')
        self.assertTrue(any('iptables' in a and '-D' in a for a in self.calls))

    def test_inbound_check_fails_closed_for_unknown_protocol(self):
        xray = {'inbounds': [{'listen': '127.0.0.1', 'port': 12345, 'protocol': 'trojan',
                              'streamSettings': {'network': 'ws'}}]}
        core = {'inbounds': []}
        replies = iter((json.dumps(xray).encode(), json.dumps(core).encode()))
        def runner(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout=next(replies))
        with self.assertRaises(ValueError):
            g.inspect_inbounds(runner)


if __name__ == '__main__':
    unittest.main()
