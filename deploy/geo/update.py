"""Download public runetfreedom databases and safely publish reduced revisions."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import time
import urllib.request

from reduce import MAX_BYTES, publish

URLS = {
    'geoip.dat': 'https://raw.githubusercontent.com/runetfreedom/russia-v2ray-rules-dat/release/geoip.dat',
    'geosite.dat': 'https://raw.githubusercontent.com/runetfreedom/russia-v2ray-rules-dat/release/geosite.dat',
}
CONTAINER = 'gamelife-gateway-proxy-1'
MAX_DOWNLOAD_SECONDS = 180

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('upstream redirect rejected')

def download(url, target):
    if url not in URLS.values():
        raise ValueError('untrusted source')
    started = time.monotonic()
    request = urllib.request.Request(url, headers={'User-Agent': 'gamelife-geodata/1'})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=25) as response:
        if response.status != 200:
            raise ValueError('upstream status')
        size = 0
        with target.open('xb') as out:
            while True:
                if time.monotonic() - started > MAX_DOWNLOAD_SECONDS:
                    raise TimeoutError('download exceeded deadline')
                block = response.read(65536)
                if not block:
                    break
                size += len(block)
                if size > MAX_BYTES:
                    raise ValueError('upstream file too large')
                out.write(block)
        if not size:
            raise ValueError('empty upstream file')

# The tar comes only from our two in-memory byte strings and has no directory or
# symlink entries. Validate every destination before updating the current link.
CONTAINER_SCRIPT = r'''set -eu
root=/data/public-geodata
rev="$1"; iphash="$2"; sitehash="$3"
case "$rev$iphash$sitehash" in *[!0-9a-f]*|'') exit 2;; esac
[ "${#rev}" = 64 ] && [ "${#iphash}" = 64 ] && [ "${#sitehash}" = 64 ]
if [ -e "$root" ] || [ -L "$root" ]; then [ -d "$root" ] && [ ! -L "$root" ]; else mkdir -m 755 "$root"; fi
if [ -e "$root/revisions" ] || [ -L "$root/revisions" ]; then [ -d "$root/revisions" ] && [ ! -L "$root/revisions" ]; else mkdir -m 755 "$root/revisions"; fi
stage=$(mktemp -d "$root/revisions/.stage.XXXXXX")
link="$root/.current.$$"
trap 'rm -f "$link"; rm -rf "$stage"' EXIT HUP INT TERM
tar -xf - -C "$stage"
[ -f "$stage/geoip.dat" ] && [ ! -L "$stage/geoip.dat" ]
[ -f "$stage/geosite.dat" ] && [ ! -L "$stage/geosite.dat" ]
[ "$(sha256sum "$stage/geoip.dat" | cut -d " " -f 1)" = "$iphash" ]
[ "$(sha256sum "$stage/geosite.dat" | cut -d " " -f 1)" = "$sitehash" ]
chmod 644 "$stage/geoip.dat" "$stage/geosite.dat"
target="$root/revisions/$rev"
if [ -e "$target" ] || [ -L "$target" ]; then
 [ -d "$target" ] && [ ! -L "$target" ]
 [ ! -L "$target/geoip.dat" ] && [ ! -L "$target/geosite.dat" ]
 [ "$(sha256sum "$target/geoip.dat" | cut -d " " -f 1)" = "$iphash" ]
 [ "$(sha256sum "$target/geosite.dat" | cut -d " " -f 1)" = "$sitehash" ]
else
 chmod 755 "$stage"
 mv "$stage" "$target"
fi
if [ -e "$root/current" ] || [ -L "$root/current" ]; then [ -L "$root/current" ]; fi
ln -s "revisions/$rev" "$link"
mv -Tf "$link" "$root/current"
# Delete only old, content-addressed revision directories; keep seven including
# current. Running requests are normally completed long before this horizon.
n=0
for entry in $(ls -1t "$root/revisions"); do
 case "$entry" in *[!0-9a-f]*|'') continue;; esac
 [ "${#entry}" = 64 ] || continue
 path="$root/revisions/$entry"
 [ -d "$path" ] && [ ! -L "$path" ] || continue
 n=$((n+1))
 if [ "$n" -gt 7 ] && [ "$entry" != "$rev" ]; then rm -rf "$path"; fi
done
'''

def make_tar(files):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w') as archive:
        for name in ('geoip.dat', 'geosite.dat'):
            content = files[name]
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(content))
    return data.getvalue()

def push(files, revision, runner=subprocess.run):
    if set(files) != set(URLS) or not re.fullmatch('[0-9a-f]{64}', revision):
        raise ValueError('invalid revision')
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    expected_revision = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    if expected_revision != revision:
        raise ValueError('revision mismatch')
    result = runner(['docker', 'exec', '-i', CONTAINER, 'sh', '-c', CONTAINER_SCRIPT, '--',
                     revision, hashes['geoip.dat'], hashes['geosite.dat']],
                    input=make_tar(files), stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=120, check=False)
    if result.returncode:
        raise RuntimeError('container publication failed')

def run():
    with tempfile.TemporaryDirectory(prefix='gamelife-geodata-') as tmp:
        source = Path(tmp) / 'source'; source.mkdir()
        target = Path(tmp) / 'reduced'
        for name, url in URLS.items():
            download(url, source / name)
        revision = publish(source, target)
        files = {name: (target / 'revisions' / revision / name).read_bytes() for name in URLS}
        push(files, revision)
        print('GEODATA_UPDATED ' + revision)

if __name__ == '__main__':
    try:
        run()
    except Exception as exc:
        print('GEODATA_ERROR ' + type(exc).__name__, file=__import__('sys').stderr)
        raise SystemExit(1)
