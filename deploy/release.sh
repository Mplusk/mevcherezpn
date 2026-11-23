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
compose=(docker compose --env-file "$envfile")
"${compose[@]}" -f compose.panel.yaml up -d --wait --wait-timeout 600
if grep -Eq '^HIDDIFY_API_KEY=.+' "$envfile"; then
    "${compose[@]}" -f compose.yaml up -d --build --wait --wait-timeout 120
    echo 'Backend and PostgreSQL healthy'
else
    echo 'Panel bootstrap complete. Configure the Hiddify domain, API key and paths in the server .env to start the backend.'
fi
ln -sfnT "$release" "$root/current"
