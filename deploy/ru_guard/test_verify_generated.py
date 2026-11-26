import copy
import unittest

import verify_generated as v


def fixture(active):
    xrules = [
        {'type': 'field', 'ip': ['127.0.0.1'], 'port': '443,80,53', 'outboundTag': 'freedom'},
        {'ip': ['geoip:private'], 'outboundTag': 'blackhole'},
        {'protocol': ['bittorrent'], 'outboundTag': 'blackhole'},
    ]
    srules = [
        {'action': 'sniff'},
        {'action': 'reject', 'protocol': ['bittorrent']},
        {'action': 'hijack-dns', 'port': [53]},
        {'action': 'hijack-dns', 'protocol': 'dns'},
        {'action': 'reject', 'protocol': 'quic', 'port': [443]},
        {'action': 'resolve'},
        {'action': 'reject', 'ip_is_private': True},
    ]
    sets = [
        {'tag': 'ru-site', 'url': 'https://example.org/geosite-category-ru.srs'},
        {'tag': 'ru-ip', 'url': 'https://example.org/geoip-ru.srs'},
    ]
    if active:
        xrules.extend([
            {'domain': [v.XRAY_SITE], 'outboundTag': 'exception'},
            {'ip': [v.XRAY_IP], 'outboundTag': 'exception'},
        ])
        sets.extend([
            {'tag': 'blocked-site', 'type': 'local', 'format': 'binary', 'path': v.SITE_FILE},
            {'tag': 'blocked-ip', 'type': 'local', 'format': 'binary', 'path': v.IP_FILE},
        ])
        srules.append({'outbound': 'exception', 'rule_set': ['blocked-site', 'blocked-ip']})
    xrules.extend([
        {'ip': ['geoip:ru'], 'outboundTag': 'blackhole'},
        {'domain': ['geosite:category-ru', 'domain:ru'], 'outboundTag': 'blackhole'},
        {'outboundTag': 'freedom', 'port': '0-65535'},
    ])
    srules.extend([
        {'action': 'reject', 'rule_set': ['ru-site', 'ru-ip']},
        {'action': 'reject', 'ip_is_private': True},
    ])
    xray = {'routing': {'domainStrategy': 'IPOnDemand', 'rules': xrules},
            'outbounds': [{'tag': 'exception', 'protocol': 'socks'}]}
    sing = {'route': {'final': 'freedom', 'rules': srules, 'rule_set': sets},
            'outbounds': [{'tag': 'exception', 'type': 'direct'}]}
    return xray, sing


class VerificationTests(unittest.TestCase):
    def test_both_modes_accept_valid_with_late_duplicate_private_guard(self):
        for active in (False, True):
            v.verify(*fixture(active), active)

    def test_dns_hijack_after_private_rejected(self):
        xray, sing = fixture(True)
        rules = sing['route']['rules']
        rules.insert(2, rules.pop(6))
        with self.assertRaises(v.InvalidRouting):
            v.verify(xray, sing, True)

    def test_exception_after_ru_block_rejected(self):
        xray, sing = fixture(True)
        rules = xray['routing']['rules']
        rules.insert(6, rules.pop(3))
        with self.assertRaises(v.InvalidRouting):
            v.verify(xray, sing, True)

    def test_wrong_local_ruleset_and_bridge_rejected(self):
        for mutation in ('ruleset', 'bridge'):
            xray, sing = fixture(True)
            if mutation == 'ruleset':
                sing['route']['rule_set'][-1]['path'] = '/tmp/wrong.srs'
            else:
                xray['outbounds'][0]['protocol'] = 'freedom'
            with self.assertRaises(v.InvalidRouting):
                v.verify(xray, sing, True)

    def test_strategy_torrent_and_preferred_user_fail_closed(self):
        for mutation in ('strategy', 'torrent', 'user'):
            xray, sing = fixture(True)
            if mutation == 'strategy':
                xray['routing']['domainStrategy'] = 'IPIfNonMatch'
            elif mutation == 'torrent':
                xray['routing']['rules'].pop(2)
            else:
                sing['route']['rules'].append({'auth_user': ['example']})
            with self.assertRaises(v.InvalidRouting):
                v.verify(xray, sing, True)

    def test_staged_rejects_premature_exception(self):
        xray, sing = fixture(True)
        with self.assertRaises(v.InvalidRouting):
            v.verify(xray, sing, False)

    def test_loopback_exception_must_remain_before_private_guard(self):
        for mutation in ('removed', 'after-private'):
            xray, sing = fixture(True)
            rules = xray['routing']['rules']
            rule = rules.pop(0)
            if mutation == 'after-private':
                rules.insert(1, rule)
            with self.assertRaises(v.InvalidRouting):
                v.verify(xray, sing, True)

    def test_bridge_before_private_rejected(self):
        for engine in ('xray', 'singbox'):
            xray, sing = fixture(True)
            if engine == 'xray':
                xray['routing']['rules'].insert(1, {'inboundTag': ['in-bridge'], 'outboundTag': 'freedom'})
            else:
                sing['route']['rules'].insert(1, {'inbound': ['in-bridge'], 'outbound': 'freedom'})
            with self.assertRaises(v.InvalidRouting):
                v.verify(xray, sing, True)


if __name__ == '__main__':
    unittest.main()
