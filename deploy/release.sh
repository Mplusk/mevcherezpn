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
gateway_network="$(awk -F= '$1 == "CADDY_NETWORK" {print $2}' "$envfile" | tail -n 1)"
if [[ -n "$gateway_network" && ! "$gateway_network" =~ ^[A-Za-z0-9_.-]+$ ]]; then
    echo 'Invalid Caddy network name'
    exit 1
fi
standalone_gateway="$(awk -F= '$1 == "VPN_STANDALONE_GATEWAY" {print $2}' "$envfile" | tail -n 1)"
if [[ -n "$standalone_gateway" && "$standalone_gateway" != 1 ]]; then
    echo 'Invalid standalone gateway setting'
    exit 1
fi
if [[ "$standalone_gateway" == 1 ]]; then
    if [[ -z "$gateway_network" || ! -f "$root/gateway/Caddyfile" ]]; then
        echo 'Standalone gateway network or configuration is missing'
        exit 1
    fi
    docker network inspect "$gateway_network" >/dev/null
    export VPN_GATEWAY_CONFIG="$root/gateway/Caddyfile"
    "${compose[@]}" -f compose.standalone.yaml up -d
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
if [[ -n "$gateway_network" ]]; then
    panel_id="$("${compose[@]}" "${panel_files[@]}" ps -q panel)"
    if [[ ! "$panel_id" =~ ^[a-f0-9]{12,64}$ ]]; then
        echo 'Panel container is unavailable'
        exit 1
    fi
    attached="$(docker inspect --format "{{if index .NetworkSettings.Networks \"$gateway_network\"}}yes{{else}}no{{end}}" "$panel_id")"
    if [[ "$attached" == no ]]; then
        docker network connect --alias gamelife-vpn-origin "$gateway_network" "$panel_id"
    elif [[ "$attached" == yes ]]; then
        if ! docker inspect --format "{{range (index .NetworkSettings.Networks \"$gateway_network\").Aliases}}{{println .}}{{end}}" "$panel_id" | grep -Fxq gamelife-vpn-origin; then
            echo 'Panel is connected to the Caddy network without the required alias'
            exit 1
        fi
    else
        echo 'Could not check the panel network'
        exit 1
    fi
fi
if [[ "$standalone_gateway" == 1 ]]; then
    python3 deploy/life/gateway.py "$root"
fi
panel_port="$(configured_port PANEL_HTTPS_PORT 28443)"
panel_wait=7200
if [[ ! -L "$root/current" ]]; then
    panel_wait=14400
fi
deadline=$((SECONDS + panel_wait))
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
