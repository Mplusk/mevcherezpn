"""Expose individual VPN nodes and an automatic latency-based group in Hiddify.

Hiddify rebuilds profiles and ignores the panel's selector and URL-test group.
Its core keeps balancers, so Auto provides a latency-based option while the
original node tags remain selectable. The old alias preserves saved selections.
"""

from __future__ import annotations

import copy
import json
import re


AUTO_TAG = "Auto"
AUTO_ALIAS_TAG = "Auto (backup)"
_AGENT = re.compile(r"^HiddifyNext/(\d+)\.(\d+)(?:\.(\d+))?(?:\b|$)", re.IGNORECASE)
_PASSTHROUGH = {("direct", "direct"), ("block", "block"), ("dns", "dns-out")}


def supported_client(agent: str) -> bool:
    match = _AGENT.match(agent or "")
    if not match:
        return False
    version = tuple(int(part or 0) for part in match.groups())
    return (4, 1, 0) <= version < (6, 0, 0)


def automatic_profile(text: str, user_agent: str) -> str:
    """Return a safe automatic profile or the original for unsupported inputs.

    Expired users, other applications, unusual configs and unsupported app
    versions retain the panel's original response. Never introduce a direct
    outbound into the automatic group's members.
    """
    if not supported_client(user_agent):
        return text
    try:
        profile = json.loads(text)
    except (TypeError, ValueError):
        return text
    if not isinstance(profile, dict) or set(profile) - {"outbounds", "endpoints"}:
        return text
    if profile.get("endpoints") not in (None, []):
        return text
    outbounds = profile.get("outbounds")
    if not isinstance(outbounds, list) or not all(isinstance(out, dict) for out in outbounds):
        return text
    tags = [out.get("tag") for out in outbounds]
    if any(not isinstance(tag, str) or not tag for tag in tags) or len(tags) != len(set(tags)):
        return text
    by_tag = dict(zip(tags, outbounds))
    groups = [out for out in outbounds if out.get("type") == "urltest"]
    selectors = [out for out in outbounds if out.get("type") == "selector"]
    if len(groups) != 1 or len(selectors) != 1:
        return text
    members = groups[0].get("outbounds")
    if (not isinstance(members, list) or not members
            or any(not isinstance(tag, str) or not tag for tag in members)
            or len(members) != len(set(members))):
        return text
    proxies = []
    for tag in members:
        outbound = by_tag.get(tag)
        if not outbound or outbound.get("type") != "vless" or outbound.get("detour"):
            return text
        # Hiddify 4 rejects this newer optional field. Removing the explicit
        # no-encryption value preserves its meaning; never discard a real value.
        if outbound.get("encryption") not in (None, "none"):
            return text
        proxies.append(outbound)
    remaining = [out for out in outbounds if out is not groups[0]
                 and out is not selectors[0] and out.get("tag") not in members]
    if any((out.get("type"), out.get("tag")) not in _PASSTHROUGH for out in remaining):
        return text
    if ({AUTO_TAG, AUTO_ALIAS_TAG} & set(members)
            or any("§hide§" in tag for tag in members)):
        return text
    if {AUTO_TAG, AUTO_ALIAS_TAG} & {out.get("tag") for out in remaining}:
        return text
    visible_proxies = []
    for proxy in proxies:
        item = copy.deepcopy(proxy)
        item.pop("encryption", None)
        visible_proxies.append(item)
    auto = {
        "type": "balancer", "tag": AUTO_TAG, "strategy": "lowest-delay",
        "outbounds": members, "tolerance": 20, "delay_acceptable_ratio": 2,
        "interrupt_exist_connections": True,
    }
    alias = {
        "type": "balancer", "tag": AUTO_ALIAS_TAG, "strategy": "lowest-delay",
        "outbounds": [AUTO_TAG], "tolerance": 20, "delay_acceptable_ratio": 2,
        "interrupt_exist_connections": True,
    }
    result = copy.deepcopy(profile)
    result["outbounds"] = [auto, alias, *visible_proxies, *remaining]
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
