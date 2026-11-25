#!/bin/sh
set -eu
umask 077
[ "$(id -u)" = 0 ] || { echo 'Root required' >&2; exit 1; }
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
root=/opt/gamelife-geodata
if [ -e "$root" ] || [ -L "$root" ]; then
  [ -d "$root" ] && [ ! -L "$root" ] || exit 1
else
  install -d -m 700 "$root"
fi
for file in update.py reduce.py; do
  tmp=$(mktemp "$root/.install.XXXXXX")
  install -m 600 "$source_dir/$file" "$tmp"
  mv -f "$tmp" "$root/$file"
done
for file in gamelife-geodata.service gamelife-geodata.timer; do
  tmp=$(mktemp /etc/systemd/system/.gamelife-geodata.XXXXXX)
  install -m 644 "$source_dir/$file" "$tmp"
  mv -f "$tmp" "/etc/systemd/system/$file"
done
systemctl daemon-reload
systemctl enable --now gamelife-geodata.timer
