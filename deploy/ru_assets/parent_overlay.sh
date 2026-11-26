#!/usr/bin/env bash
# Source from release.sh after root/release/gateway_network/panel_files are set.
# Only the exact root-owned opt-in can change the panel Compose files.
configure_parent_ru_assets() {
    local status=0 mount_list existing stable_overlay
    python3 "$release/deploy/ru_assets/opt_in.py" "$root/ru-assets" || status=$?
    case "$status" in
        0)
            if [[ -z "$gateway_network" || ! "$gateway_network" =~ ^[A-Za-z0-9_.-]+$ ]]; then
                echo 'Routing assets require a validated gateway network' >&2
                return 1
            fi
            if [[ ! -f "$release/deploy/ru_assets/compose.ru-assets.yaml" || -L "$release/deploy/ru_assets/compose.ru-assets.yaml" ]]; then
                echo 'Routing assets overlay is missing or unsafe' >&2
                return 1
            fi
            export VPN_RU_ASSET_ROOT="$root/ru-assets"
            export CADDY_NETWORK="$gateway_network"
            stable_overlay="$root/ru-assets/compose.ru-assets.yaml"
            if [[ -e "$stable_overlay" || -L "$stable_overlay" ]]; then
                if [[ ! -f "$stable_overlay" || -L "$stable_overlay" || "$(stat -c '%u:%a:%h' "$stable_overlay")" != '0:600:1' ]] ||
                   ! cmp -s "$stable_overlay" "$release/deploy/ru_assets/compose.ru-assets.yaml"; then
                    echo 'Stable routing overlay differs or is unsafe' >&2
                    return 1
                fi
                panel_files+=(-f "$stable_overlay")
            else
                panel_files+=(-f "$release/deploy/ru_assets/compose.ru-assets.yaml")
            fi
            ;;
        3)
            # Deleting the marker must never silently detach a mount that an
            # already active template may require on the next panel restart.
            if mount_list="$(docker inspect --format '{{range .Mounts}}{{println .Destination}}{{end}}' gamelife-hiddify-panel-1 2>/dev/null)"; then
                if grep -Fxq '/opt/hiddify-manager/services/xray/bin/gamelife' <<<"$mount_list"; then
                    echo 'Routing assets marker missing while panel mount is active' >&2
                    return 1
                fi
            else
                if ! existing="$(docker ps -aq --filter 'name=^/gamelife-hiddify-panel-1$' 2>/dev/null)"; then
                    echo 'Cannot verify existing panel mount' >&2
                    return 1
                fi
                if [[ -n "$existing" ]]; then
                    echo 'Cannot inspect existing panel mount' >&2
                    return 1
                fi
            fi
            ;;
        *)
            echo 'Routing assets opt-in is invalid' >&2
            return 1
            ;;
    esac
}
