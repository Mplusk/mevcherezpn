import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import live_probe


class LiveProbeTests(unittest.TestCase):
    def test_target_allowlist_and_explicit_socks(self):
        completed = subprocess.CompletedProcess([], 0, b"200", b"")
        with patch.object(live_probe.subprocess, "run", return_value=completed) as run:
            self.assertEqual(live_probe.curl(live_probe.RU_SITE, port=12345), 200)
            command = run.call_args.args[0]
            self.assertIn("--socks5-hostname", command)
            self.assertIn("127.0.0.1:12345", command)
            self.assertNotIn("MONITOR_PROFILE_JSON", run.call_args.kwargs["env"])
            self.assertIn("=https", command)
            with self.assertRaises(ValueError):
                live_probe.curl("https://example.invalid/")
            self.assertEqual(run.call_count, 1)

    def test_bad_curl_results_are_inconclusive(self):
        with patch.object(live_probe.subprocess, "run", return_value=subprocess.CompletedProcess([], 28, b"000")):
            self.assertIsNone(live_probe.curl(live_probe.RU_SITE, port=12345))
        with patch.object(live_probe.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"not-an-ip")):
            self.assertIsNone(live_probe.curl(live_probe.IP_SITES[0], port=12345, ip=True))

    def test_http_error_is_not_reported_as_guard_denial(self):
        self.assertEqual(live_probe.site_status(None), "connection_failed")
        self.assertEqual(live_probe.site_status(403), "http_error")
        self.assertEqual(live_probe.site_status(302), "reachable")

    def test_run_reports_individual_nodes_without_profile_data(self):
        profiles = {name: {"private": "secret-example"} for name in live_probe.vpn_monitor.NODES}
        with patch.object(live_probe, "curl", return_value=200), patch.object(
                live_probe, "probe", return_value={"exit": "ok", "blocked_site": "reachable", "ru_site": "connection_failed"}):
            result = live_probe.run(profiles)
        self.assertEqual(set(result["nodes"]), {"de", "nl", "pl"})
        self.assertNotIn("secret-example", json.dumps(result))

    @unittest.skipUnless(os.name == "posix" and os.geteuid() == 0, "requires root-owned Linux fixture")
    def test_rejects_symlink_and_insecure_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "credentials.json"
            path.write_text("{}", encoding="utf-8")
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                live_probe.read_profiles(path)
            path.chmod(0o600)
            alias = Path(tmp) / "link"
            alias.symlink_to(path)
            with self.assertRaises(OSError):
                live_probe.read_profiles(alias)


if __name__ == "__main__":
    unittest.main()
