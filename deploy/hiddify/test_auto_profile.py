import json
import unittest

from auto_profile import AUTO_ALIAS_TAG, AUTO_TAG, automatic_profile, supported_client
from install_auto_profile import NEW, OLD, transform_source


AGENT = "HiddifyNext/5.9.15 (android) like ClashMeta v2ray sing-box"


def fixture():
    return {"outbounds": [
        {"type": "selector", "tag": "Select", "default": "Auto",
         "outbounds": ["Auto", "NL", "DE", "PL"]},
        {"type": "urltest", "tag": "Auto", "outbounds": ["NL", "DE", "PL"]},
        *[{"type": "vless", "tag": name, "server": name.lower() + ".example",
           "server_port": 443, "uuid": "00000000-0000-4000-8000-000000000001",
           "tls": {"enabled": True, "server_name": name.lower() + ".example"}}
          for name in ("NL", "DE", "PL")],
        {"type": "direct", "tag": "direct"},
        {"type": "block", "tag": "block"},
        {"type": "dns", "tag": "dns-out"},
    ]}


class AutomaticProfileTests(unittest.TestCase):
    def test_supported_clients_only(self):
        for agent in ("HiddifyNext/4.1.1 (windows)", AGENT, "hiddifynext/5.9.15"):
            self.assertTrue(supported_client(agent), agent)
        for agent in ("HiddifyNext/4.0.9", "HiddifyNext/6.0.0", "HiddifyNext",
                      "sing-box/1.13", "Dart/3", "Mozilla/5.0"):
            self.assertFalse(supported_client(agent), agent)

    def test_profile_groups_always_lead_to_the_same_balancer_and_preserve_credentials(self):
        original = fixture()
        result = json.loads(automatic_profile(json.dumps(original), AGENT))
        auto, alias, *other = result["outbounds"]
        self.assertEqual((auto["type"], auto["tag"], auto["strategy"]),
                         ("balancer", AUTO_TAG, "lowest-delay"))
        self.assertEqual((alias["type"], alias["tag"], alias["outbounds"]),
                         ("balancer", AUTO_ALIAS_TAG, [AUTO_TAG]))
        proxies = [out for out in other if out["type"] == "vless"]
        self.assertEqual(auto["outbounds"], [out["tag"] for out in proxies])
        self.assertEqual(len(proxies), 3)
        self.assertTrue(all("§hide§" in out["tag"] for out in proxies))
        self.assertTrue(all("direct" != tag for tag in auto["outbounds"]))
        for before, after in zip(original["outbounds"][2:5], proxies):
            self.assertEqual({k: v for k, v in after.items() if k != "tag"},
                             {k: v for k, v in before.items() if k != "tag"})
        self.assertEqual(original, fixture())

    def test_explicit_no_encryption_is_compatible_with_older_clients(self):
        data = fixture()
        for proxy in data["outbounds"][2:5]:
            proxy["encryption"] = "none"
        result = json.loads(automatic_profile(json.dumps(data), AGENT))
        proxies = [out for out in result["outbounds"] if out["type"] == "vless"]
        self.assertEqual(len(proxies), 3)
        self.assertTrue(all("encryption" not in out for out in proxies))

    def test_single_working_node_still_has_automatic_group(self):
        data = fixture()
        data["outbounds"][1]["outbounds"] = ["NL"]
        data["outbounds"] = [x for x in data["outbounds"] if x.get("tag") not in ("DE", "PL")]
        auto = json.loads(automatic_profile(json.dumps(data), AGENT))["outbounds"][0]
        self.assertEqual(len(auto["outbounds"]), 1)

    def test_other_app_and_inactive_account_are_unchanged(self):
        source = json.dumps(fixture())
        self.assertEqual(automatic_profile(source, "sing-box/1.13"), source)
        inactive = json.dumps({"outbounds": [{"type": "block", "tag": "expired"}]})
        self.assertEqual(automatic_profile(inactive, AGENT), inactive)
        self.assertEqual(automatic_profile("not json", AGENT), "not json")

    def test_unsafe_or_unexpected_profiles_are_unchanged(self):
        cases = []
        bad = fixture(); bad["outbounds"][1]["outbounds"].append("direct"); cases.append(bad)
        bad = fixture(); bad["outbounds"][1]["outbounds"].append("missing"); cases.append(bad)
        bad = fixture(); bad["outbounds"][1]["outbounds"].append("NL"); cases.append(bad)
        bad = fixture(); bad["outbounds"][2]["detour"] = "direct"; cases.append(bad)
        bad = fixture(); bad["outbounds"][2]["encryption"] = "some-new-mode"; cases.append(bad)
        bad = fixture(); bad["outbounds"].append({"type": "direct", "tag": "untrusted"}); cases.append(bad)
        bad = fixture(); bad["outbounds"][3]["tag"] = "NL"; cases.append(bad)
        bad = fixture(); bad["route"] = {"final": "Select"}; cases.append(bad)
        bad = fixture(); bad["endpoints"] = [{"type": "wireguard"}]; cases.append(bad)
        for bad in cases:
            with self.subTest(case=bad):
                source = json.dumps(bad)
                self.assertEqual(automatic_profile(source, AGENT), source)

    def test_output_is_deterministic_and_idempotent(self):
        data = json.dumps(fixture(), ensure_ascii=False)
        result = automatic_profile(data, AGENT)
        self.assertEqual(automatic_profile(data, AGENT), result)
        self.assertEqual(automatic_profile(result, AGENT), result)


class InstallerTests(unittest.TestCase):
    def test_exact_hook_and_idempotence(self):
        source = "def handler(self, core, c):\n" + OLD
        updated = transform_source(source)
        self.assertIn(NEW, updated)
        self.assertEqual(transform_source(updated), updated)

    def test_unknown_or_partial_source_is_rejected(self):
        with self.assertRaises(ValueError):
            transform_source("def handler():\n    pass\n")
        with self.assertRaises(ValueError):
            transform_source("def handler(self, core, c):\n" + NEW + OLD)


if __name__ == "__main__":
    unittest.main()
