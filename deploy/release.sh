#!/usr/bin/env bash
set -euo pipefail
umask 077

# Invoked on the VPS from a release directory with the commit SHA as its name.
root="$HOME/services/gamelife-vpn"
release="$(pwd -P)"
[[ "$release" == "$root/releases/"* && "$(basename "$release")" =~ ^[a-f0-9]{40}$ ]]
exec 9>"$root/deploy.lock"
flock -w 300 9
envfile="$root/.env"
if [[ ! -e "$envfile" ]]; then
    command -v ss >/dev/null
    for project in gamelife-hiddify gamelife-vpn; do
        if docker ps -aq --filter "label=com.docker.compose.project=$project" | grep -q .; then
            echo 'A Compose project with the intended name already exists'
            exit 1
        fi
    done
    if ss -H -lnt | awk '$4 ~ /:(28443|18090)$/ {found=1} END {exit !found}'; then
        echo 'A required local port is already in use'
        exit 1
    fi
    tmp="$(mktemp "$root/.initial-env.XXXXXX")"
    {
        printf 'PANEL_DB_PASSWORD=%s\n' "$(openssl rand -hex 32)"
        printf 'PANEL_REDIS_PASSWORD=%s\n' "$(openssl rand -hex 32)"
        printf 'POSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 32)"
        printf 'VPN_ADMIN_TOKEN=%s\n' "$(openssl rand -hex 32)"
        printf 'HIDDIFY_API_KEY=\nHIDDIFY_ADMIN_BASE=\nHIDDIFY_CLIENT_BASE=\n'
    } >"$tmp"
    mv "$tmp" "$envfile"
fi
compose=(docker compose --parallel 1 --env-file "$envfile")
panel_files=(-f compose.panel.yaml)
if grep -Eq '^CADDY_NETWORK=[A-Za-z0-9_.-]+$' "$envfile"; then
    panel_files+=(-f compose.gateway.yaml)
elif grep -q '^CADDY_NETWORK=.' "$envfile"; then
    echo 'Invalid Caddy network name'
    exit 1
fi
configured_port() {
    local name="$1" fallback="$2" value
    value="$(awk -F= -v key="$name" '$1 == key {print $2}' "$envfile" | tail -n 1)"
    value="${!name:-${value:-$fallback}}"
    if [[ ! "$value" =~ ^[0-9]{1,5}$ ]] || ((10#$value < 1 || 10#$value > 65535)); then
        echo "Invalid port in $name"
        return 1
    fi
    printf '%s' "$value"
}

"${compose[@]}" "${panel_files[@]}" up -d
panel_port="$(configured_port PANEL_HTTPS_PORT 28443)"
deadline=$((SECONDS + 3600))
until timeout 60 bash -c 'openssl s_client -connect "127.0.0.1:$1" -servername gamelife.shop </dev/null 2>/dev/null | openssl x509 -noout >/dev/null 2>&1' _ "$panel_port"; do
    if ((SECONDS >= deadline)); then
        echo 'Panel TLS did not become ready within the expected time'
        exit 1
    fi
    sleep 15
done
if grep -Eq '^HIDDIFY_API_KEY=.+' "$envfile"; then
    "${compose[@]}" -f compose.yaml up -d --build
    api_port="$(configured_port VPN_API_PORT 18090)"
    deadline=$((SECONDS + 1800))
    until timeout 60 curl --fail --silent --output /dev/null "http://127.0.0.1:$api_port/healthz"; do
        if ((SECONDS >= deadline)); then
            echo 'Backend did not become ready within the expected time'
            exit 1
        fi
        sleep 15
    done
    echo 'Backend and PostgreSQL healthy'
else
    echo 'Panel bootstrap complete. Configure the Hiddify domain, API key and paths in the server .env to start the backend.'
fi
ln -sfnT "$release" "$root/current"
