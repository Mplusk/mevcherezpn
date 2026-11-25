import io
import json
import os
import unittest
from unittest.mock import Mock, patch

import vpn_monitor as monitor


def profiles():
    return {name: {"protocol": "vless", "settings": {"vnext": [{"address": host, "users": [{"id": "test-only"}]}]},
                   "streamSettings": {"network": "ws", "security": "tls"}}
            for name, (host, _) in monitor.NODES.items()}


class MonitorTests(unittest.TestCase):
    def test_profiles_reject_unknown_host_and_protocol(self):
        data = profiles()
        self.assertEqual(monitor.validate_profiles(json.dumps(data)), data)
        data["de"]["settings"]["vnext"][0]["address"] = "attacker.example"
        with self.assertRaises(ValueError):
            monitor.validate_profiles(json.dumps(data))
        data = profiles()
        data["nl"]["protocol"] = "freedom"
        with self.assertRaises(ValueError):
            monitor.validate_profiles(json.dumps(data))

    def test_url_allowlist_and_redirect_block(self):
        self.assertTrue(monitor.validate_subscription_url("https://nl.gamelife.shop/example/xray/"))
        for url in ("http://nl.gamelife.shop/xray/", "https://evil.example/xray/",
                    "https://user@nl.gamelife.shop/xray/", "https://nl.gamelife.shop:444/xray/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                monitor.validate_subscription_url(url)
        self.assertIsNone(monitor.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example"))

    @patch("vpn_monitor.urllib.request.build_opener")
    def test_subscription_content_is_validated(self, build_opener):
        response = Mock()
        response.status = 200
        response.read.return_value = json.dumps({"outbounds": list(profiles().values())}).encode()
        build_opener.return_value.open.return_value.__enter__ = Mock(return_value=response)
        build_opener.return_value.open.return_value.__exit__ = Mock(return_value=False)
        url = "https://gamelife.shop/test/xray/"
        self.assertTrue(monitor.subscription_ok(url))
        response.read.return_value = b'{"outbounds": []}'
        self.assertFalse(monitor.subscription_ok(url))

    @patch("vpn_monitor.time.sleep")
    def test_retry_eventual_success_and_failure(self, _):
        probe = Mock(side_effect=[False, False, True])
        self.assertTrue(monitor.retry(probe))
        self.assertEqual(probe.call_count, 3)
        self.assertFalse(monitor.retry(lambda: False))

    @patch("vpn_monitor.time.sleep")
    @patch("vpn_monitor.subprocess.run")
    @patch("vpn_monitor.subprocess.Popen")
    def test_probe_uses_stdin_and_checks_exit_ip(self, popen, run, _):
        process = Mock()
        process.stdin = io.BytesIO()
        process.poll.return_value = None
        popen.return_value = process
        run.return_value = Mock(returncode=0, stdout=b"2.27.75.91\n")
        with patch.dict(os.environ, {"MONITOR_PROFILE_JSON": "private", "MONITOR_SUBSCRIPTION_URL": "private", "MONITOR_SMTP_PASSWORD": "private"}):
            self.assertTrue(monitor.probe_one("xray", profiles()["de"], "2.27.75.91"))
        self.assertNotIn("MONITOR_PROFILE_JSON", popen.call_args.kwargs["env"])
        self.assertNotIn("MONITOR_SUBSCRIPTION_URL", run.call_args.kwargs["env"])
        self.assertNotIn("MONITOR_SMTP_PASSWORD", popen.call_args.kwargs["env"])
        self.assertIn("stdin:", popen.call_args.args[0])
        run.return_value.stdout = b"203.0.113.9"
        process.stdin = io.BytesIO()
        self.assertFalse(monitor.probe_one("xray", profiles()["de"], "2.27.75.91"))

    @patch("vpn_monitor.time.sleep")
    @patch("vpn_monitor.subprocess.run")
    @patch("vpn_monitor.subprocess.Popen")
    def test_ip_endpoint_failure_falls_back(self, popen, run, _):
        process = Mock()
        process.stdin = io.BytesIO()
        process.poll.return_value = None
        popen.return_value = process
        run.side_effect = [Mock(returncode=28, stdout=b""), Mock(returncode=0, stdout=b"2.27.75.91\n")]
        self.assertTrue(monitor.probe_one("xray", profiles()["de"], "2.27.75.91"))
        self.assertEqual(run.call_count, 2)

    @patch("vpn_monitor.retry", side_effect=[True, False, True, False])
    def test_all_nodes_tested_even_when_subscription_fails(self, retry):
        with patch.dict(os.environ, {"MONITOR_PROFILE_JSON": json.dumps(profiles()),
                                     "MONITOR_SUBSCRIPTION_URL": "https://gamelife.shop/test/xray/"}):
            self.assertEqual(monitor.main(), 1)
        self.assertEqual(retry.call_count, 4)

    def test_missing_secret_fails_without_revealing_content(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(monitor.main(), 1)

    @patch("vpn_monitor.retry", return_value=True)
    def test_invalid_subscription_does_not_prevent_node_checks(self, retry):
        with patch.dict(os.environ, {"MONITOR_PROFILE_JSON": json.dumps(profiles()),
                                     "MONITOR_SUBSCRIPTION_URL": "https://invalid.example/xray/"}):
            self.assertEqual(monitor.main(), 1)
        self.assertEqual(retry.call_count, 3)


if __name__ == "__main__":
    unittest.main()
