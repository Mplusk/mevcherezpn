import unittest
from patch import (BRIDGE, QUIC, EARLY_XRAY, EARLY_SING, SING_BRIDGE, SING_SNIFF, SING_REMOTE, SING_LOCAL, SING_DNS, SING_QUIC,
                   OLD_IP, NEW_IP, OLD_DOMAIN, NEW_DOMAIN,
                   XRAY_STRATEGY, transform_xray, transform_singbox)


class GuardTemplateTests(unittest.TestCase):
    def setUp(self):
        self.loopback = '{"type":"field","outboundTag":"freedom","ip":["127.0.0.1"],"port":"443,80,53"}'
        self.xray = ('begin\n' + XRAY_STRATEGY + '\n' + BRIDGE + self.loopback + '\n' + QUIC
                     + 'user rules\n' + OLD_IP + '\n' + OLD_DOMAIN + '\nend')
        self.singbox = SING_REMOTE + SING_SNIFF + SING_BRIDGE + SING_DNS + SING_QUIC + 'user rules\n'

    def test_xray_guards_precede_bridge_and_user_rules(self):
        result = transform_xray(self.xray)
        self.assertEqual(result.count(self.loopback), 1)
        self.assertLess(result.index(self.loopback), result.index(EARLY_XRAY))
        self.assertLess(result.index(EARLY_XRAY), result.index(BRIDGE))
        self.assertLess(result.index(BRIDGE), result.index('user rules'))
        self.assertIn(NEW_IP, result)
        self.assertIn(NEW_DOMAIN, result)
        self.assertIn('g == "geoip:ru-blocked"', NEW_IP)
        self.assertIn('g == "geosite:ru-blocked"', NEW_DOMAIN)
        self.assertEqual(result, transform_xray(result))

    def test_singbox_guards_precede_bridge(self):
        result = transform_singbox(self.singbox)
        self.assertLess(result.index(SING_SNIFF), result.index(EARLY_SING))
        self.assertLess(result.index(SING_SNIFF), result.index(SING_DNS))
        self.assertLess(result.index(SING_DNS), result.index(SING_QUIC))
        self.assertLess(result.index(SING_QUIC), result.index(EARLY_SING))
        self.assertLess(result.index(EARLY_SING), result.index(SING_BRIDGE))
        self.assertLess(result.index(SING_BRIDGE), result.index('user rules'))
        self.assertLess(result.index('"action": "resolve"'), result.index('"ip_is_private": true'))
        self.assertIn(SING_LOCAL, result)
        self.assertEqual(result, transform_singbox(result))

    def test_unexpected_and_partial_sources_are_rejected(self):
        for broken in (self.xray.replace(QUIC, ''), self.xray + BRIDGE,
                       self.xray.replace(OLD_IP, 'unexpected'), self.xray + EARLY_XRAY,
                       self.xray.replace('IPOnDemand', 'IPIfNonMatch')):
            with self.subTest(broken=broken):
                with self.assertRaises(ValueError):
                    transform_xray(broken)
        for broken in ('no marker', self.singbox + SING_BRIDGE, self.singbox + EARLY_SING,
                       self.singbox.replace(SING_DNS, ''), self.singbox.replace(SING_QUIC, ''),
                       self.singbox.replace(SING_BRIDGE, '') + SING_BRIDGE):
            with self.subTest(broken=broken):
                with self.assertRaises(ValueError):
                    transform_singbox(broken)


if __name__ == '__main__':
    unittest.main()
