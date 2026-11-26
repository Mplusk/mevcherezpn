"""Attach a Happ-specific client routing profile to subscription responses."""

import base64
import json
from pathlib import Path
import re


PROFILE = {
    "Name": "GameLife Russia direct",
    "GlobalProxy": "true",
    "RouteOrder": "block-proxy-direct",
    "RemoteDNSType": "DoH",
    "RemoteDNSDomain": "https://cloudflare-dns.com/dns-query",
    "RemoteDNSIP": "1.1.1.1",
    "DomesticDNSType": "DoH",
    "DomesticDNSDomain": "https://dns.google/dns-query",
    "DomesticDNSIP": "8.8.8.8",
    "Geoipurl": "https://nl.gamelife.shop/assets/geodata/geoip.dat",
    "Geositeurl": "https://nl.gamelife.shop/assets/geodata/geosite.dat",
    "DnsHosts": {"cloudflare-dns.com": "1.1.1.1", "dns.google": "8.8.8.8"},
    "DirectSites": ["domain:2ip.io", "geosite:category-ru", "geosite:ru-available-only-inside", "geosite:private"],
    "DirectIp": ["geoip:ru", "geoip:private"],
    "ProxySites": ["geosite:ru-blocked"],
    "ProxyIp": ["geoip:ru-blocked"],
    "BlockSites": [],
    "BlockIp": [],
    "DomainStrategy": "IPIfNonMatch",
    "FakeDNS": "false",
}
_HAPP = re.compile(r"^Happ(?:/|\s|$)", re.IGNORECASE)
GEO_ROOT = Path("/opt/hiddify-manager/services/xray/bin/gamelife/current")


def last_geodata_update():
    """Expose the current downloaded DAT revision's timestamp to Happ.

    Existing revision files are immutable, so repeated subscription requests
    do not force a download when their contents have not changed. Failure to
    read the optional timestamp must not break subscription delivery.
    """
    try:
        timestamp = max((GEO_ROOT / name).stat().st_mtime for name in ("geoip.dat", "geosite.dat"))
        return str(int(timestamp)) if timestamp > 0 else None
    except (OSError, OverflowError, ValueError):
        return None


def add_routing_header(response, user_agent):
    """Only Happ receives the routing command; never include user secrets."""
    if _HAPP.match(user_agent or ""):
        profile_data = PROFILE.copy()
        updated = last_geodata_update()
        if updated is not None:
            profile_data["LastUpdated"] = updated
        profile = json.dumps(profile_data, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        response.headers["routing"] = "happ://routing/onadd/" + base64.b64encode(profile).decode("ascii")
    return response
