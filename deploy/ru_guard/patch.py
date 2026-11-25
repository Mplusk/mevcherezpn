"""Fail-closed, idempotent routing-template transformations for Hiddify 14.2.

The caller must pre-provision the two separate DAT files and symlinks before
installing transformed templates. This module does not modify a running server.
"""

BRIDGE = '''      {# Traffic hiddify-core sends into the bridge of an xray outbound leaves through that outbound #}
      {% for o in ctx.outbounds.xray_bridged %}
      {
        "type": "field",
        "inboundTag": ["in-{{ o.xray_tag }}"],
        "outboundTag": {{ o.xray_tag|tojson }}
      },
      {% endfor %}
'''
QUIC = '''        { //block quic
          "type": "field", "port": 443, "network": "udp", "outboundTag": "blackhole"
        },
'''
EARLY_XRAY = '''        {# GameLife: enforce private-address and BitTorrent protections before exceptions. #}
        {"type": "field", "outboundTag": "blackhole", "ip": ["geoip:private"]},
        {% if ctx.hconfig.torrent_block %}
        {"type": "field", "outboundTag": "blackhole", "protocol": ["bittorrent"]},
        {% endif %}
'''
SING_BRIDGE = '''    {# Traffic xray sends into a bridge SOCKS inbound (Tor, Psiphon, the admin's outbounds / endpoints) leaves through that outbound #}
'''
SING_SNIFF = '''    {"action": "sniff"},
    {% if ctx.hconfig.torrent_block %}
    {# Settings > General: Block BitTorrent (the sniffer above recognizes its handshake, whatever the port) #}
    {"protocol": ["bittorrent"], "action": "reject", "method": "default"},
    {% endif %}
'''
SING_REMOTE = '''      "type": "remote",
      "format": {{ rs.format|tojson }},
      "update_interval": "1d",
      "url": {{ rs.url|tojson }}
'''
SING_LOCAL = '''      {% if rs.url == "https://nl.gamelife.shop/assets/geodata/geosite-ru-blocked.srs" %}
      "type": "local", "format": "binary", "path": "/opt/hiddify-manager/services/xray/bin/gamelife/current/geosite-ru-blocked.srs"
      {% elif rs.url == "https://nl.gamelife.shop/assets/geodata/geoip-ru-blocked.srs" %}
      "type": "local", "format": "binary", "path": "/opt/hiddify-manager/services/xray/bin/gamelife/current/geoip-ru-blocked.srs"
      {% else %}
''' + SING_REMOTE + '''      {% endif %}
'''
EARLY_SING = '''    {# GameLife: resolve before rejecting private destinations and exception rules. #}
    {"action": "resolve"},
    {"action": "reject", "ip_is_private": true},
'''
OLD_IP = '"ip": {{ o.geoips|tojson }}'
NEW_IP = '''"ip": [{% for g in o.geoips %}{% if not loop.first %},{% endif %}{{ ("ext:gamelife/current/geoip.dat:ru-blocked" if g == "geoip:ru-blocked" else g)|tojson }}{% endfor %}]'''
OLD_DOMAIN = '{% for g in o.geosites %}{{ g|tojson }},{% endfor %}'
NEW_DOMAIN = '''{% for g in o.geosites %}{{ ("ext:gamelife/current/geosite.dat:ru-blocked" if g == "geosite:ru-blocked" else g)|tojson }},{% endfor %}'''
XRAY_STRATEGY = '"domainStrategy": "IPOnDemand",'


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise ValueError('unexpected template structure')
    return source.replace(old, new, 1)


def transform_xray(source):
    if source.count(XRAY_STRATEGY) != 1:
        raise ValueError('xray routing must resolve destination IP before rules')
    if EARLY_XRAY in source:
        if (source.count(EARLY_XRAY) == source.count(NEW_IP) == source.count(NEW_DOMAIN) == 1
                and source.count(BRIDGE) == 1 and source.index(QUIC) < source.index(EARLY_XRAY) < source.index(BRIDGE)):
            return source
        raise ValueError('partially patched xray template')
    if source.count(BRIDGE) != 1 or source.index(BRIDGE) > source.index(QUIC):
        raise ValueError('unexpected bridge order')
    source = replace_once(source, BRIDGE, '')
    source = replace_once(source, QUIC, QUIC + EARLY_XRAY + BRIDGE)
    source = replace_once(source, OLD_IP, NEW_IP)
    return replace_once(source, OLD_DOMAIN, NEW_DOMAIN)


def transform_singbox(source):
    if (source.count(SING_SNIFF) != 1 or source.count(SING_BRIDGE) != 1
            or source.index(SING_SNIFF) > source.index(SING_BRIDGE)):
        raise ValueError('hiddify-core sniff and bittorrent guard not found')
    if EARLY_SING in source:
        if (source.count(EARLY_SING) == 1 and source.count(SING_BRIDGE) == 1
                and source.count(SING_LOCAL) == 1 and source.index(EARLY_SING) < source.index(SING_BRIDGE)):
            return source
        raise ValueError('partially patched hiddify-core template')
    source = replace_once(source, SING_REMOTE, SING_LOCAL)
    return replace_once(source, SING_BRIDGE, EARLY_SING + SING_BRIDGE)
