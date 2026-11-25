import unittest
from apply_monitor import ApplyError, start_and_wait


class Clock:
    def __init__(self):
        self.value = 0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class Panel:
    def __init__(self):
        self.calls = []
        self.busy = False
        self.ambiguous = False
        self.finished = True

    def request(self, method, path, data=None):
        self.calls.append((method, path))
        if method == 'POST':
            if self.ambiguous:
                raise TimeoutError('response lost')
            return {'action': 'apply', 'started': 100.0}
        if path.startswith('apply/log/'):
            return {'started': True, 'finished': self.finished, 'text': 'never print this'}
        return {'running': ['apply'] if self.busy else []}


class ApplyMonitorTests(unittest.TestCase):
    def run_monitor(self, panel):
        clock = Clock()
        return start_and_wait(panel, timeout=3, interval=1, clock=clock.now,
                              wall_clock=lambda: 100.0, sleep=clock.sleep)

    def test_observes_completion_without_exposing_log(self):
        panel = Panel()
        self.assertEqual(self.run_monitor(panel), 100.0)
        self.assertEqual(len([c for c in panel.calls if c[0] == 'POST']), 1)

    def test_busy_action_prevents_post(self):
        panel = Panel()
        panel.busy = True
        with self.assertRaises(ApplyError):
            self.run_monitor(panel)
        self.assertFalse(any(c[0] == 'POST' for c in panel.calls))

    def test_ambiguous_start_never_retries(self):
        panel = Panel()
        panel.ambiguous = True
        with self.assertRaises(ApplyError):
            self.run_monitor(panel)
        self.assertEqual(len([c for c in panel.calls if c[0] == 'POST']), 1)

    def test_unfinished_action_times_out(self):
        panel = Panel()
        panel.finished = False
        with self.assertRaises(ApplyError):
            self.run_monitor(panel)


if __name__ == '__main__':
    unittest.main()
