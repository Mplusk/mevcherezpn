#!/usr/bin/env bash
set -euo pipefail
[[ $(id -u) -eq 0 ]] || { echo 'Must run as root' >&2; exit 1; }
src=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
for cmd in curl unzip sha256sum python3 systemctl; do command -v "$cmd" >/dev/null || { echo "Missing dependency: $cmd" >&2; exit 1; }; done
creds=/etc/gamelife-vpn-monitor/credentials.json
[[ -f "$creds" && ! -L "$creds" && $(stat -c '%u:%a' "$creds") == 0:600 ]] || { echo 'Root-owned mode-600 credentials required' >&2; exit 1; }
work=$(mktemp -d /tmp/gamelife-monitor.XXXXXXXX)
trap 'rm -f "$work/xray.zip" "$work/xray"; rmdir "$work"' EXIT
curl -fsSL --retry 3 --max-time 120 https://github.com/XTLS/Xray-core/releases/download/v26.11.9/Xray-linux-64.zip -o "$work/xray.zip"
printf '%s  %s\n' b651f7e1d9966ee2c1b1ee1bf8b52c72771824d3065dce96bbc8bcc19ed95f82 "$work/xray.zip" | sha256sum -c -
unzip -q "$work/xray.zip" xray -d "$work"
install -d -m 700 /opt/gamelife-vpn-monitor /var/lib/gamelife-vpn-monitor
install -m 700 "$work/xray" /opt/gamelife-vpn-monitor/xray
for f in vpn_monitor.py alerts.py host_runner.py; do install -m 600 "$src/$f" "/opt/gamelife-vpn-monitor/$f"; done
for f in gamelife-vpn-monitor.service gamelife-vpn-monitor.timer; do install -m 644 "$src/$f" "/etc/systemd/system/$f"; done
systemctl daemon-reload
systemctl enable --now gamelife-vpn-monitor.timer
systemctl is-active --quiet gamelife-vpn-monitor.timer
echo 'Standby monitor timer enabled'
