"""Verify generated server routing in memory without printing configuration.

The staged mode checks guards and the existing RU block before the parent
outbound is created.  The active mode additionally requires both local rule
sets and the Xray bridge to the matching direct outbound.  This is a routing
configuration check, not a substitute for an external VPN connection test.
"""

import json
import subprocess
import sys


SITE_FILE = '/opt/hiddify-manager/services/xray/bin/gamelife/current/geosite-ru-blocked.srs'
IP_FILE = '/opt/hiddify-manager/services/xray/bin/gamelife/current/geoip-ru-blocked.srs'
XRAY_SITE = 'ext:gamelife/current/geosite.dat:ru-blocked'
XRAY_IP = 'ext:gamelife/current/geoip.dat:ru-blocked'


class InvalidRouting(ValueError):
    pass


def one(items, predicate):
    found = [(index, item) for index, item in enumerate(items) if predicate(item)]
    if len(found) != 1:
        raise InvalidRouting('expected one routing item')
    return found[0]


def contains(item, key, value):
    return isinstance(item, dict) and isinstance(item.get(key), list) and value in item[key]


def verify_xray(data, active):
    route = data.get('routing') if isinstance(data, dict) else None
    if not isinstance(route, dict) or route.get('domainStrategy') != 'IPOnDemand':
        raise InvalidRouting('unsafe Xray strategy')
    rules = route.get('rules')
    if not isinstance(rules, list) or not rules or not all(isinstance(r, dict) for r in rules):
        raise InvalidRouting('missing Xray rules')
    private = next((i for i, r in enumerate(rules) if contains(r, 'ip', 'geoip:private')
                    and r.get('outboundTag') == 'blackhole'), None)
    torrent = next((i for i, r in enumerate(rules) if contains(r, 'protocol', 'bittorrent')
                    and r.get('outboundTag') == 'blackhole'), None)
    if private is None or torrent is None:
        raise InvalidRouting('missing Xray safeguards')
    loopback, _ = one(rules, lambda r: r.get('type') == 'field'
                      and r.get('ip') == ['127.0.0.1']
                      and r.get('port') == '443,80,53'
                      and r.get('outboundTag') == 'freedom')
    if loopback >= private:
        raise InvalidRouting('Xray loopback service exception changed')
    if any(any(isinstance(tag, str) and tag.startswith('in-')
               for tag in rule.get('inboundTag', []))
           for rule in rules[:private] if isinstance(rule.get('inboundTag', []), list)):
        raise InvalidRouting('Xray bridge precedes private guard')
    ru_ip, _ = one(rules, lambda r: contains(r, 'ip', 'geoip:ru') and r.get('outboundTag') == 'blackhole')
    ru_site, _ = one(rules, lambda r: contains(r, 'domain', 'geosite:category-ru')
                     and contains(r, 'domain', 'domain:ru') and r.get('outboundTag') == 'blackhole')
    if not (private < ru_ip and private < ru_site and torrent < ru_ip and torrent < ru_site):
        raise InvalidRouting('Xray safeguards follow RU block')
    if any('user' in rule for rule in rules):
        raise InvalidRouting('preferred user outbound bypasses routing')
    if rules[-1].get('outboundTag') != 'freedom' or rules[-1].get('port') != '0-65535':
        raise InvalidRouting('unexpected Xray default')
    has_exception = any(contains(r, 'domain', XRAY_SITE) or contains(r, 'ip', XRAY_IP) for r in rules)
    if not active:
        if has_exception:
            raise InvalidRouting('unexpected Xray exception before activation')
        return None
    site_index, site = one(rules, lambda r: contains(r, 'domain', XRAY_SITE))
    ip_index, ip = one(rules, lambda r: contains(r, 'ip', XRAY_IP))
    tag = site.get('outboundTag')
    if (not isinstance(tag, str) or not tag or tag in ('blackhole', 'freedom')
            or ip.get('outboundTag') != tag
            or not (max(private, torrent) < min(site_index, ip_index)
                    and max(site_index, ip_index) < min(ru_ip, ru_site))):
        raise InvalidRouting('unsafe Xray exception order')
    outbounds = data.get('outbounds')
    if not isinstance(outbounds, list):
        raise InvalidRouting('missing Xray outbounds')
    _, bridge = one(outbounds, lambda o: isinstance(o, dict) and o.get('tag') == tag)
    if bridge.get('protocol') != 'socks':
        raise InvalidRouting('missing Xray bridge')
    return tag


def verify_singbox(data, active, expected_tag):
    route = data.get('route') if isinstance(data, dict) else None
    if not isinstance(route, dict) or route.get('final') != 'freedom':
        raise InvalidRouting('unexpected Hiddify default')
    rules = route.get('rules')
    sets = route.get('rule_set')
    if not isinstance(rules, list) or not isinstance(sets, list):
        raise InvalidRouting('missing Hiddify rules')
    if not all(isinstance(item, dict) for item in rules + sets):
        raise InvalidRouting('invalid Hiddify rules')
    sniff, _ = one(rules, lambda r: r.get('action') == 'sniff')
    resolve, _ = one(rules, lambda r: r.get('action') == 'resolve')
    private = next((i for i, r in enumerate(rules) if r.get('ip_is_private') is True
                    and r.get('action') == 'reject'), None)
    if private is None:
        raise InvalidRouting('missing Hiddify private-address guard')
    torrent, _ = one(rules, lambda r: contains(r, 'protocol', 'bittorrent') and r.get('action') == 'reject')
    dns_port, _ = one(rules, lambda r: r.get('action') == 'hijack-dns' and contains(r, 'port', 53))
    dns_protocol, _ = one(rules, lambda r: r.get('action') == 'hijack-dns'
                          and r.get('protocol') == 'dns')
    quic, _ = one(rules, lambda r: r.get('action') == 'reject'
                  and r.get('protocol') == 'quic' and contains(r, 'port', 443))
    if not (sniff < torrent < quic < resolve < private and max(dns_port, dns_protocol) < quic):
        raise InvalidRouting('unsafe Hiddify safeguards order')
    if any(any(isinstance(tag, str) and tag.startswith('in-')
               for tag in rule.get('inbound', []))
           for rule in rules[:private] if isinstance(rule.get('inbound', []), list)):
        raise InvalidRouting('Hiddify bridge precedes private guard')
    if any('auth_user' in rule for rule in rules):
        raise InvalidRouting('preferred user outbound bypasses routing')
    ru_tags = set()
    for item in sets:
        url = item.get('url')
        if isinstance(url, str) and (url.endswith('/geosite-category-ru.srs') or url.endswith('/geoip-ru.srs')):
            ru_tags.add(item.get('tag'))
    if len(ru_tags) != 2 or any(not isinstance(tag, str) or not tag for tag in ru_tags):
        raise InvalidRouting('missing Hiddify RU block sets')
    ru_rules = [i for i, r in enumerate(rules) if r.get('action') == 'reject'
                and isinstance(r.get('rule_set'), list) and ru_tags.issubset(set(r['rule_set']))]
    if len(ru_rules) != 1 or private >= ru_rules[0]:
        raise InvalidRouting('invalid Hiddify RU block order')
    local_sets = [item for item in sets if item.get('path') in (SITE_FILE, IP_FILE)]
    if not active:
        if local_sets:
            raise InvalidRouting('unexpected local sets before activation')
        return
    if (len(local_sets) != 2 or {item.get('path') for item in local_sets} != {SITE_FILE, IP_FILE}
            or any(item.get('type') != 'local' or item.get('format') != 'binary' for item in local_sets)):
        raise InvalidRouting('missing local Hiddify rule sets')
    local_tags = {item.get('tag') for item in local_sets}
    if len(local_tags) != 2 or any(not isinstance(tag, str) or not tag for tag in local_tags):
        raise InvalidRouting('invalid local Hiddify rule set tags')
    allow_rules = [i for i, r in enumerate(rules) if r.get('outbound') == expected_tag
                   and isinstance(r.get('rule_set'), list) and local_tags.issubset(set(r['rule_set']))]
    if len(allow_rules) != 1 or not (private < allow_rules[0] < ru_rules[0]):
        raise InvalidRouting('unsafe Hiddify exception order')
    outbounds = data.get('outbounds')
    if not isinstance(outbounds, list):
        raise InvalidRouting('missing Hiddify outbounds')
    _, direct = one(outbounds, lambda o: isinstance(o, dict) and o.get('tag') == expected_tag)
    if direct.get('type') != 'direct':
        raise InvalidRouting('Hiddify exception does not use direct outbound')


def verify(xray, singbox, active):
    if type(active) is not bool:
        raise InvalidRouting('invalid mode')
    tag = verify_xray(xray, active)
    verify_singbox(singbox, active, tag)


def read_config(path):
    result = subprocess.run(['docker', 'exec', 'gamelife-hiddify-panel-1', 'cat', path],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20, check=True)
    if len(result.stdout) > 16 * 1024 * 1024:
        raise InvalidRouting('generated configuration exceeds limit')
    return json.loads(result.stdout)


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ('staged', 'active'):
        raise ValueError('choose staged or active')
    for service in ('hiddify-xray.service', 'hiddify-core.service'):
        subprocess.run(['docker', 'exec', 'gamelife-hiddify-panel-1', 'systemctl', 'is-active', '--quiet', service],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, check=True)
    verify(read_config('/opt/hiddify-manager/generated/xray.json'),
           read_config('/opt/hiddify-manager/generated/hiddify-core.json'), sys.argv[1] == 'active')
    print('GENERATED_ROUTING_OK')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('GENERATED_ROUTING_FAILED', file=sys.stderr)
        raise SystemExit(1)
