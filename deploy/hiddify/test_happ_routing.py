import base64
import json
import unittest
from types import SimpleNamespace

from happ_routing import PROFILE, add_routing_header
from install_auto_profile import HEADERS_NEW, HEADERS_OLD, NEW, OLD, transform_source


class HappRoutingTests(unittest.TestCase):
    def test_only_happ_receives_profile(self):
        for user_agent in ("Happ/3.13.0", "Happ/5.2.0/ios/123", "happ/4.4 Android"):
            response = SimpleNamespace(headers={"profile-title": "unchanged"})
            self.assertIs(add_routing_header(response, user_agent), response)
            prefix = "happ://routing/onadd/"
            self.assertTrue(response.headers["routing"].startswith(prefix))
            actual = json.loads(base64.b64decode(response.headers["routing"][len(prefix):]))
            self.assertEqual(actual, PROFILE)
            self.assertEqual(actual["RouteOrder"], "block-proxy-direct")
            self.assertEqual(actual["GlobalProxy"], "true")
            self.assertIn("geosite:category-ru", actual["DirectSites"])
            self.assertIn("domain:2ip.io", actual["DirectSites"])
            self.assertIn("geosite:ru-blocked", actual["ProxySites"])
            self.assertIn("geoip:ru", actual["DirectIp"])
            self.assertIn("geoip:ru-blocked", actual["ProxyIp"])
            self.assertEqual(response.headers["profile-title"], "unchanged")

    def test_other_clients_are_unchanged(self):
        for user_agent in ("HiddifyNext/5.9.15", "v2rayNG/1.10", "Mozilla/5.0", "Happened/1", "", None):
            response = SimpleNamespace(headers={})
            add_routing_header(response, user_agent)
            self.assertNotIn("routing", response.headers)

    def test_install_from_original_and_previous_hook_is_idempotent(self):
        original = "def a(self, core, c):\n" + OLD + "\ndef add_headers(c):\n" + HEADERS_OLD
        previous = "def a(self, core, c):\n" + NEW + "\ndef add_headers(c):\n" + HEADERS_OLD
        expected = transform_source(original)
        self.assertEqual(transform_source(previous), expected)
        self.assertEqual(transform_source(expected), expected)
        self.assertIn(HEADERS_NEW, expected)


if __name__ == "__main__":
    unittest.main()
