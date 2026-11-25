#!/usr/bin/env bash
set -euo pipefail

# Exercise real iptables in a private network namespace, without changing the
# runner's network rules. Running twice also checks that the hook is idempotent.
sudo unshare --net -- bash -eu -c '
    ip link set lo up
    ip link add wan0 type dummy
    ip addr add 192.0.2.1/24 dev wan0
    ip link set wan0 up
    ip route add default dev wan0
    iptables -N DOCKER-USER
    ip6tables -N DOCKER-USER
    bash "$1"
    bash "$1"
    for tool in iptables ip6tables; do
        "$tool" -C DOCKER-USER -j GAMELIFE-DOCKER-GUARD
        [[ "$("$tool" -S DOCKER-USER | grep -Fxc -- "-A DOCKER-USER -j GAMELIFE-DOCKER-GUARD")" == 1 ]]
        "$tool" -C GAMELIFE-DOCKER-GUARD -i wan0 -p tcp -m conntrack --ctorigdstport 80 -j RETURN
        "$tool" -C GAMELIFE-DOCKER-GUARD -i wan0 -p tcp -m conntrack --ctorigdstport 443 -j RETURN
        "$tool" -C GAMELIFE-DOCKER-GUARD -i wan0 -j DROP
    done
' _ "$PWD/deploy/firewall/docker-guard.sh"
