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
import zlib

from reduce import MAX_BYTES, publish

URLS = {
    'geoip.dat': 'https://raw.githubusercontent.com/runetfreedom/russia-v2ray-rules-dat/release/geoip.dat',
    'geosite.dat': 'https://raw.githubusercontent.com/runetfreedom/russia-v2ray-rules-dat/release/geosite.dat',
    'geosite-ru-blocked.srs': 'https://raw.githubusercontent.com/runetfreedom/russia-v2ray-rules-dat/release/sing-box/rule-set-geosite/geosite-ru-blocked.srs',
    'geoip-ru-blocked.srs': 'https://raw.githubusercontent.com/runetfreedom/russia-v2ray-rules-dat/release/sing-box/rule-set-geoip/geoip-ru-blocked.srs',
}
DAT_NAMES = ('geoip.dat', 'geosite.dat')
SRS_NAMES = ('geosite-ru-blocked.srs', 'geoip-ru-blocked.srs')
MAX_SRS_BYTES = 4 * 1024 * 1024
MAX_SRS_UNCOMPRESSED = 16 * 1024 * 1024
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
                if size > (MAX_SRS_BYTES if url.endswith('.srs') else MAX_BYTES):
                    raise ValueError('upstream file too large')
                out.write(block)
        if not size:
            raise ValueError('empty upstream file')

def validate_srs(data):
    """Reject invalid, truncated or unbounded compressed sing-box rule sets."""
    if not 5 <= len(data) <= MAX_SRS_BYTES or data[:4] != b'SRS\x01':
        raise ValueError('invalid rule set header or size')
    decoder = zlib.decompressobj()
    result = decoder.decompress(data[4:], MAX_SRS_UNCOMPRESSED + 1)
    if not result or len(result) > MAX_SRS_UNCOMPRESSED or not decoder.eof or decoder.unused_data:
        raise ValueError('invalid rule set payload')

# The tar comes only from our two in-memory byte strings and has no directory or
# symlink entries. Validate every destination before updating the current link.
CONTAINER_SCRIPT = r'''set -eu
root=/data/public-geodata
rev="$1"; iphash="$2"; sitehash="$3"; srs1hash="$4"; srs2hash="$5"
case "$rev$iphash$sitehash$srs1hash$srs2hash" in *[!0-9a-f]*|'') exit 2;; esac
for hash in "$rev" "$iphash" "$sitehash" "$srs1hash" "$srs2hash"; do [ "${#hash}" = 64 ]; done
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
for pair in "geosite-ru-blocked.srs:$srs1hash" "geoip-ru-blocked.srs:$srs2hash"; do
 file=${pair%:*}; hash=${pair#*:}; [ -f "$stage/$file" ] && [ ! -L "$stage/$file" ]
 [ "$(sha256sum "$stage/$file" | cut -d " " -f 1)" = "$hash" ]
done
chmod 644 "$stage/geoip.dat" "$stage/geosite.dat" "$stage/geosite-ru-blocked.srs" "$stage/geoip-ru-blocked.srs"
target="$root/revisions/$rev"
if [ -e "$target" ] || [ -L "$target" ]; then
 [ -d "$target" ] && [ ! -L "$target" ]
 [ ! -L "$target/geoip.dat" ] && [ ! -L "$target/geosite.dat" ]
 [ "$(sha256sum "$target/geoip.dat" | cut -d " " -f 1)" = "$iphash" ]
 [ "$(sha256sum "$target/geosite.dat" | cut -d " " -f 1)" = "$sitehash" ]
 for pair in "geosite-ru-blocked.srs:$srs1hash" "geoip-ru-blocked.srs:$srs2hash"; do
  file=${pair%:*}; hash=${pair#*:}; [ -f "$target/$file" ] && [ ! -L "$target/$file" ]
  [ "$(sha256sum "$target/$file" | cut -d " " -f 1)" = "$hash" ]
 done
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
        for name in DAT_NAMES + SRS_NAMES:
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
    for name in SRS_NAMES:
        validate_srs(files[name])
    result = runner(['docker', 'exec', '-i', CONTAINER, 'sh', '-c', CONTAINER_SCRIPT, '--',
                     revision, *(hashes[name] for name in DAT_NAMES + SRS_NAMES)],
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
        dat_revision = publish(source, target)
        files = {name: (target / 'revisions' / dat_revision / name).read_bytes() for name in DAT_NAMES}
        files.update({name: (source / name).read_bytes() for name in SRS_NAMES})
        hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
        revision = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        push(files, revision)
        print('GEODATA_UPDATED ' + revision)

if __name__ == '__main__':
    try:
        run()
    except Exception as exc:
        print('GEODATA_ERROR ' + type(exc).__name__, file=__import__('sys').stderr)
        raise SystemExit(1)
