import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import alerts


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.result = Path(self.tmp.name) / "result.json"
        self.state = Path(self.tmp.name) / "state" / "status.json"
        self.ok = dict.fromkeys(alerts.KEYS, "ok")
        self.result.write_text(json.dumps(self.ok), encoding="utf-8")
        self.env = {"MONITOR_RESULT_FILE": str(self.result), "MONITOR_STATE_FILE": str(self.state),
                    "MONITOR_SMTP_USERNAME": "sender@example.com", "MONITOR_SMTP_PASSWORD": "test-password",
                    "MONITOR_ALERT_TO": "receiver@example.com"}

    def run_alert(self, test=False):
        with patch.dict(os.environ, {**self.env, "MONITOR_TEST_EMAIL": str(test).lower()}, clear=True):
            return alerts.main()

    @patch("alerts.send_email")
    def test_transitions_and_recovery_only(self, send):
        self.assertEqual(self.run_alert(), 0)
        self.assertEqual(send.call_count, 0)
        self.assertEqual(self.run_alert(), 0)
        self.assertEqual(send.call_count, 0)
        broken = {**self.ok, "de": "failed"}
        self.result.write_text(json.dumps(broken), encoding="utf-8")
        self.assertEqual(self.run_alert(), 0)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.run_alert(), 0)
        self.assertEqual(send.call_count, 1)
        self.result.write_text(json.dumps(self.ok), encoding="utf-8")
        self.assertEqual(self.run_alert(), 0)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(alerts.read_state(self.state), self.ok)

    @patch("alerts.send_email", side_effect=OSError("secret must not print"))
    def test_failed_send_preserves_previous_for_retry(self, send):
        self.state.parent.mkdir()
        self.state.write_text(json.dumps(self.ok), encoding="utf-8")
        self.result.write_text(json.dumps({**self.ok, "pl": "failed"}), encoding="utf-8")
        self.assertEqual(self.run_alert(), 1)
        self.assertEqual(alerts.read_state(self.state), self.ok)

    @patch("alerts.send_email")
    def test_initial_failure_and_explicit_test_send(self, send):
        self.result.unlink()
        self.assertEqual(self.run_alert(), 0)
        self.assertTrue(all(value == "unknown" for value in alerts.read_state(self.state).values()))
        self.assertEqual(send.call_count, 1)
        self.result.write_text(json.dumps(self.ok), encoding="utf-8")
        self.assertEqual(self.run_alert(test=True), 0)
        self.assertTrue(send.call_args.args[-1])
        self.assertTrue(all(value == "unknown" for value in alerts.read_state(self.state).values()))

    @patch("alerts.send_email")
    def test_test_email_keeps_real_failure_pending(self, send):
        self.state.parent.mkdir()
        self.state.write_text(json.dumps(self.ok), encoding="utf-8")
        self.result.write_text(json.dumps({**self.ok, "de": "failed"}), encoding="utf-8")
        self.assertEqual(self.run_alert(test=True), 0)
        self.assertEqual(alerts.read_state(self.state), self.ok)
        self.assertEqual(self.run_alert(), 0)
        self.assertEqual(send.call_count, 2)

    @patch("alerts.send_email")
    def test_missing_smtp_fails_even_when_state_unchanged(self, send):
        self.env["MONITOR_SMTP_PASSWORD"] = ""
        self.assertEqual(self.run_alert(), 1)
        send.assert_not_called()

    @patch("alerts.smtplib.SMTP_SSL")
    def test_tls_and_test_subject(self, smtp):
        alerts.send_email("sender@example.com", "test-password", "receiver@example.com", self.ok, True, source="nl")
        self.assertEqual(smtp.call_args.args, ("smtp.spaceweb.ru", 465))
        client = smtp.return_value.__enter__.return_value
        client.login.assert_called_once_with("sender@example.com", "test-password")
        msg = client.send_message.call_args.args[0]
        self.assertIn("тестовое", msg["Subject"])
        self.assertNotIn("test-password", str(msg))
        self.assertIn("Нидерланды", msg.get_content())


if __name__ == "__main__":
    unittest.main()
