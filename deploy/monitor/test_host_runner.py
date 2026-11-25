import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import host_runner


class HostRunnerTests(unittest.TestCase):
    def setUp(self):
        self.credentials = {
            'MONITOR_PROFILE_JSON': '{"test":"private"}',
            'MONITOR_SUBSCRIPTION_URL': 'https://example.invalid/private',
            'MONITOR_SMTP_USERNAME': 'sender@example.invalid',
            'MONITOR_SMTP_PASSWORD': 'test-only-password',
            'MONITOR_ALERT_TO': 'receiver@example.invalid',
            'MONITOR_SOURCE': 'control',
        }

    def run_case(self, results):
        tmp = tempfile.TemporaryDirectory()
        with patch.object(host_runner, 'load_credentials', return_value=self.credentials), \
             patch.object(host_runner.tempfile, 'TemporaryDirectory', return_value=tmp), \
             patch.object(host_runner.subprocess, 'run', side_effect=results) as run:
            outcome = host_runner.main()
        return outcome, run.call_args_list

    def test_separate_environments_and_success(self):
        status, calls = self.run_case([subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0)])
        self.assertEqual(status, 0)
        probe = calls[0].kwargs['env']
        alert = calls[1].kwargs['env']
        self.assertIn('MONITOR_PROFILE_JSON', probe)
        self.assertNotIn('MONITOR_SMTP_PASSWORD', probe)
        self.assertIn('MONITOR_SMTP_PASSWORD', alert)
        self.assertNotIn('MONITOR_PROFILE_JSON', alert)
        self.assertNotIn('MONITOR_SUBSCRIPTION_URL', alert)
        self.assertEqual(alert['MONITOR_SOURCE'], 'control')
        self.assertEqual(calls[0].kwargs['stdout'], subprocess.DEVNULL)
        self.assertEqual(calls[1].kwargs['stderr'], subprocess.DEVNULL)

    def test_probe_failure_or_timeout_still_runs_alert(self):
        for first in (subprocess.CompletedProcess([], 1), subprocess.TimeoutExpired('probe', 410)):
            with self.subTest(first=type(first).__name__):
                status, calls = self.run_case([first, subprocess.CompletedProcess([], 0)])
                self.assertEqual(status, 1)
                self.assertEqual(len(calls), 2)

    def test_email_failure_is_failure(self):
        status, calls = self.run_case([subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 1)])
        self.assertEqual(status, 1)
        self.assertEqual(len(calls), 2)

    def test_bad_credentials_never_start_process(self):
        with patch.object(host_runner, 'load_credentials', side_effect=ValueError('private')), \
             patch.object(host_runner.subprocess, 'run') as run:
            self.assertEqual(host_runner.main(), 1)
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
