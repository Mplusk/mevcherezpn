#!/usr/bin/env bash
set +e
ufw --force disable >/dev/null 2>&1
systemctl disable --now gamelife-docker-guard.service >/dev/null 2>&1
for tool in iptables ip6tables; do
    while "$tool" -w -C DOCKER-USER -j GAMELIFE-DOCKER-GUARD 2>/dev/null; do
        "$tool" -w -D DOCKER-USER -j GAMELIFE-DOCKER-GUARD
    done
    "$tool" -w -F GAMELIFE-DOCKER-GUARD 2>/dev/null
    "$tool" -w -X GAMELIFE-DOCKER-GUARD 2>/dev/null
done
backup=/root/services/gamelife-vpn/firewall-rollback
for file in default-ufw user.rules user6.rules; do
    if [[ -f "$backup/$file" ]]; then
        case "$file" in
            default-ufw) cp -p "$backup/$file" /etc/default/ufw ;;
            *) cp -p "$backup/$file" "/etc/ufw/$file" ;;
        esac
    fi
done
