"""Fetch, validate and atomically publish the four public routing assets."""
from __future__ import annotations

import hashlib
import fcntl
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import zlib

from reduce import RULES, reduce_dat

ROOT = Path('/root/services/gamelife-vpn/ru-assets')
BASE = 'https://nl.gamelife.shop/assets/geodata/'
IMAGE = 'ghcr.io/hiddify/hiddify-manager:v14.2.0@sha256:399c248e20d139a7dab9cf05566751bcbbce30b3a03ae832525e95dfb78333c9'
FILES = ('geoip.dat', 'geosite.dat', 'geoip-ru-blocked.srs', 'geosite-ru-blocked.srs')
MAX_FILE = 4 * 1024 * 1024
MAX_DECOMPRESSED = 16 * 1024 * 1024
VALIDATION_SCRIPT = r'''set -eu
d=$(mktemp -d)
trap 'rm -rf "$d"' EXIT HUP INT TERM
tar -xf - -C "$d"
export XRAY_LOCATION_ASSET="$d"
/opt/hiddify-manager/services/xray/bin/xray run -test -c "$d/test.json" >/dev/null 2>&1
sed -i "s|TESTDIR|$d|g" "$d/srs-test.json"
set +e
timeout 5 /opt/hiddify-manager/services/hiddify-core/hiddify-core srun -c "$d/srs-test.json" >/dev/null 2>&1
rc=$?
set -e
[ "$rc" = 124 ]
'''


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('unexpected redirect')


def validate(name, data):
    if name not in FILES or not data or len(data) > MAX_FILE:
        raise ValueError('invalid asset')
    if name in RULES:
        reduce_dat(data, RULES[name])
    else:
        if data[:4] != b'SRS\x01':
            raise ValueError('invalid SRS header')
        decoder = zlib.decompressobj()
        plain = decoder.decompress(data[4:], MAX_DECOMPRESSED + 1)
        if not plain or len(plain) > MAX_DECOMPRESSED or not decoder.eof or decoder.unused_data:
            raise ValueError('invalid SRS payload')


def fetch(name):
    if name not in FILES + ('manifest.json',):
        raise ValueError('invalid asset name')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(BASE + name, headers={'User-Agent': 'gamelife-ru-assets/1'})
    deadline = time.monotonic() + 60
    parts, size = [], 0
    with opener.open(request, timeout=15) as response:
        if response.status != 200:
            raise ValueError('unexpected HTTP response')
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError('asset download timed out')
            part = response.read(65536)
            if not part:
                break
            size += len(part)
            if size > (8192 if name == 'manifest.json' else MAX_FILE):
                raise ValueError('asset too large')
            parts.append(part)
    data = b''.join(parts)
    if name != 'manifest.json':
        validate(name, data)
    return data


def parse_manifest(data):
    if not data or len(data) > 8192:
        raise ValueError('invalid manifest size')
    value = json.loads(data)
    if not isinstance(value, dict) or set(value) != {'version', 'revision', 'sha256'} or type(value['version']) is not int or value['version'] != 1:
        raise ValueError('invalid manifest')
    hashes = value['sha256']
    if not isinstance(hashes, dict) or set(hashes) != set(FILES):
        raise ValueError('invalid manifest asset list')
    if not all(isinstance(v, str) and re.fullmatch('[0-9a-f]{64}', v) for v in hashes.values()):
        raise ValueError('invalid asset digest')
    revision = hashlib.sha256(b'gamelife-geo-manifest-v1\x00' + json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    if value['revision'] != revision:
        raise ValueError('invalid manifest revision')
    return value


def fetch_consistent(fetcher):
    for _ in range(3):
        before = parse_manifest(fetcher('manifest.json'))
        data = {name: fetcher(name) for name in FILES}
        after = parse_manifest(fetcher('manifest.json'))
        hashes = {name: hashlib.sha256(data[name]).hexdigest() for name in FILES}
        if before == after and hashes == before['sha256']:
            return data, before['revision']
    raise ValueError('asset version changed during download')


def safe_directory(path, mode):
    try:
        path.mkdir(mode=mode)
        os.chmod(path, mode)
    except FileExistsError:
        pass
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ValueError('unsafe asset directory')
    if info.st_mode & 0o005 != 0o005:
        raise ValueError('asset directory is not readable and traversable')
    return info


def validate_xray(data, runner=subprocess.run):
    """Validate DAT and SRS in a no-network disposable pinned-image container."""
    config = {'log': {'loglevel': 'error'},
              'outbounds': [{'protocol': 'freedom', 'tag': 'direct'}],
              'routing': {'rules': [
                  {'type': 'field', 'domain': ['ext:geosite.dat:ru-blocked',
                                               'geosite:category-ru', 'geosite:private'], 'outboundTag': 'direct'},
                  {'type': 'field', 'ip': ['ext:geoip.dat:ru-blocked', 'geoip:ru',
                                           'geoip:private'], 'outboundTag': 'direct'}]}}
    srs_config = {'log': {'level': 'error'}, 'outbounds': [{'type': 'direct', 'tag': 'direct'}],
                  'route': {'rules': [{'rule_set': ['rs-site', 'rs-ip'], 'outbound': 'direct'}],
                            'rule_set': [{'type': 'local', 'tag': 'rs-site', 'format': 'binary',
                                          'path': 'TESTDIR/geosite-ru-blocked.srs'},
                                         {'type': 'local', 'tag': 'rs-ip', 'format': 'binary',
                                          'path': 'TESTDIR/geoip-ru-blocked.srs'}], 'final': 'direct'}}
    files = {name: data[name] for name in FILES}
    files['test.json'] = json.dumps(config, separators=(',', ':')).encode('utf-8')
    files['srs-test.json'] = json.dumps(srs_config, separators=(',', ':')).encode('utf-8')
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as tar:
        for name, contents in files.items():
            item = tarfile.TarInfo(name); item.mode = 0o600; item.size = len(contents)
            tar.addfile(item, io.BytesIO(contents))
    result = runner(['docker', 'run', '--rm', '-i', '--network', 'none', '--read-only',
                     '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                     '--pids-limit', '64', '--memory', '256m', '--tmpfs', '/tmp:rw,nosuid,nodev,noexec',
                     '--entrypoint', '/bin/sh', IMAGE, '-c', VALIDATION_SCRIPT],
                    input=stream.getvalue(), stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=60, check=False)
    if result.returncode:
        raise ValueError('Xray or Hiddify rejected candidate assets')


def cleanup(revisions, active, keep=7, protected=()):
    """Remove only known owned regular files in old content-addressed dirs."""
    if keep < 7:
        raise ValueError('must retain at least seven revisions')
    candidates = []
    for path in revisions.iterdir():
        if not re.fullmatch('[0-9a-f]{64}', path.name):
            continue
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o022:
            candidates.append((info.st_mtime_ns, path))
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    if any(not isinstance(name, str) or not re.fullmatch('[0-9a-f]{64}', name) for name in protected):
        raise ValueError('invalid protected revision')
    retained = {path.name for _, path in candidates[:keep]} | {active} | set(protected)
    for _, path in candidates:
        if path.name in retained:
            continue
        children = list(path.iterdir())
        if {child.name for child in children} != set(FILES):
            continue
        infos = [child.lstat() for child in children]
        if not all(stat.S_ISREG(i.st_mode) and i.st_uid == os.geteuid() and not i.st_mode & 0o022 for i in infos):
            continue
        for child in children:
            child.unlink()
        path.rmdir()


def _sync_locked(root, fetcher, validator, *, protected=(), on_commit=None, on_rollback=None):
    revisions = root / 'revisions'
    safe_directory(revisions, 0o755)
    current = root / 'current'
    previous = None
    if current.exists() or current.is_symlink():
        if not current.is_symlink() or not re.fullmatch(r'revisions/[0-9a-f]{64}', os.readlink(current)):
            raise ValueError('unsafe current link')
        if current.resolve(strict=True).parent != revisions.resolve(strict=True):
            raise ValueError('current link escapes revisions')
        previous = os.readlink(current)
    if any(not isinstance(name, str) or not re.fullmatch('[0-9a-f]{64}', name) for name in protected):
        raise ValueError('invalid protected revision')

    # All downloads and validations happen before the public selector changes.
    data, revision = fetch_consistent(fetcher)
    for name in FILES:
        validate(name, data[name])
    validator(data)
    hashes = {name: hashlib.sha256(data[name]).hexdigest() for name in FILES}
    target = revisions / revision
    if not target.exists():
        with tempfile.TemporaryDirectory(prefix='.stage-', dir=revisions) as stage_name:
            stage = Path(stage_name)  # mode 0700 until all files are complete
            for name in FILES:
                fd = os.open(stage / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, 'wb') as out:
                    out.write(data[name]); out.flush(); os.fsync(out.fileno())
                os.chmod(stage / name, 0o644)
            os.chmod(stage, 0o755)
            os.rename(stage, target)
    safe_directory(target, 0o755)
    for name in FILES:
        path = target / name
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ValueError('unsafe published asset')
        if hashlib.sha256(path.read_bytes()).hexdigest() != hashes[name]:
            raise ValueError('existing revision differs')
    link = root / ('.current-' + os.urandom(12).hex())
    try:
        os.symlink('revisions/' + revision, link)
        os.replace(link, current)
    finally:
        if link.is_symlink():
            link.unlink()
    try:
        if on_commit is not None:
            on_commit(revision)
    except Exception as error:
        if previous is None:
            current.unlink()
        else:
            rollback = root / ('.rollback-' + os.urandom(12).hex())
            try:
                os.symlink(previous, rollback)
                os.replace(rollback, current)
            finally:
                if rollback.is_symlink():
                    rollback.unlink()
        if on_rollback is not None:
            try:
                on_rollback()
            except Exception as rollback_error:
                raise RuntimeError('asset rollback notification failed') from rollback_error
        raise RuntimeError('asset activation failed; selector restored') from error
    cleanup(revisions, revision, protected=protected)
    return revision


def sync(root=ROOT, fetcher=fetch, validator=validate_xray, *, protected=(), on_commit=None, on_rollback=None):
    """Previous current remains selected after any download/validation failure."""
    root = Path(root)
    safe_directory(root, 0o755)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(root / '.sync.lock', flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError('unsafe sync lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _sync_locked(root, fetcher, validator, protected=protected, on_commit=on_commit, on_rollback=on_rollback)
    finally:
        os.close(fd)


if __name__ == '__main__':
    if os.geteuid() != 0:
        raise SystemExit('root required')
    try:
        sync()
        print('RU_ASSETS_SYNC_OK')
    except Exception as exc:
        print('RU_ASSETS_SYNC_FAILED ' + type(exc).__name__)
        raise SystemExit(1)
