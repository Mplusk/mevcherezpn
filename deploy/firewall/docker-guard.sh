#!/usr/bin/env bash
set -euo pipefail

# DOCKER-USER is evaluated before Docker's ACCEPT rules. Match only packets
# arriving from the Internet-facing interface, leaving container networks and
# established return traffic alone.
interface="$(ip -4 route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i=="dev") {print $(i+1); exit}}')"
[[ "$interface" =~ ^[a-zA-Z0-9_.:-]{1,15}$ ]]

for tool in iptables ip6tables; do
    "$tool" -w -S DOCKER-USER >/dev/null
    if ! "$tool" -w -S GAMELIFE-DOCKER-GUARD >/dev/null 2>&1; then
        "$tool" -w -N GAMELIFE-DOCKER-GUARD
    fi
    "$tool" -w -F GAMELIFE-DOCKER-GUARD
    "$tool" -w -A GAMELIFE-DOCKER-GUARD -i "$interface" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
    "$tool" -w -A GAMELIFE-DOCKER-GUARD -i "$interface" -p tcp -m conntrack --ctorigdstport 80 -j RETURN
    "$tool" -w -A GAMELIFE-DOCKER-GUARD -i "$interface" -p tcp -m conntrack --ctorigdstport 443 -j RETURN
    "$tool" -w -A GAMELIFE-DOCKER-GUARD -i "$interface" -j DROP
    "$tool" -w -A GAMELIFE-DOCKER-GUARD -j RETURN
    if ! "$tool" -w -C DOCKER-USER -j GAMELIFE-DOCKER-GUARD 2>/dev/null; then
        "$tool" -w -I DOCKER-USER 1 -j GAMELIFE-DOCKER-GUARD
    fi
done
