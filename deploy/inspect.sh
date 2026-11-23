#!/usr/bin/env bash
set -eu

report() { printf 'VPN_CHECK %s=%s\n' "$1" "$2"; }

if [[ "$(uname -s)" == Linux && "$(uname -m)" =~ ^(x86_64|aarch64)$ ]]; then
    report supported_os yes
else
    report supported_os no
fi

if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
    report docker_compose yes
else
    report docker_compose no
    exit 0
fi

if docker info >/dev/null 2>&1; then
    report docker_access yes
else
    report docker_access no
    exit 0
fi

if ! command -v ss >/dev/null; then
    report panel_port unknown
    report api_port unknown
    exit 0
fi

for port in 28443 18090; do
    name=panel_port
    [[ "$port" == 18090 ]] && name=api_port
    if ss -H -lnt | awk -v p=":$port" '$4 ~ (p "$") {found=1} END {exit !found}'; then
        report "$name" busy
    else
        report "$name" free
    fi
done

if docker ps -aq --filter label=com.docker.compose.project=gamelife-hiddify | grep -q .; then
    report panel_project_exists yes
else
    report panel_project_exists no
fi
if docker ps -aq --filter label=com.docker.compose.project=gamelife-vpn | grep -q .; then
    report api_project_exists yes
else
    report api_project_exists no
fi
if [[ -e "$HOME/services/gamelife-vpn" ]]; then
    report project_directory_exists yes
else
    report project_directory_exists no
fi
if docker ps --format '{{.Image}}' | grep -Eiq '(^|/)caddy(:|@|$)'; then
    report caddy_container yes
else
    report caddy_container no
fi
if ps -eo comm= | grep -qx caddy; then
    report caddy_process yes
else
    report caddy_process no
fi
if df -Pk "$HOME" | awk 'NR==2 {exit !($4 >= 8388608)}'; then
    report disk_8gb_available yes
else
    report disk_8gb_available no
fi
if awk '/MemAvailable:/ {exit !($2 >= 2097152)}' /proc/meminfo; then
    report memory_2gb_available yes
else
    report memory_2gb_available no
fi
