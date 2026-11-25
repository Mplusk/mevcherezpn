"""Fail-closed check for an explicitly enabled parent-panel Compose overlay."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

FILES = ('geoip.dat', 'geosite.dat', 'geoip-ru-blocked.srs', 'geosite-ru-blocked.srs')
SALT = b'gamelife-geo-manifest-v1\x00'
MAX_FILE = 4 * 1024 * 1024
MARKER = 'compose-enabled'


def safe_dir(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError('unsafe asset directory')
    if info.st_mode & 0o005 != 0o005:
        raise ValueError('asset directory is not publicly traversable')


def is_enabled(root: Path) -> bool:
    try:
        root.lstat()
    except FileNotFoundError:
        return False
    safe_dir(root)
    marker = root / MARKER
    try:
        fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or stream.read(16) != b'v1\n'):
            raise ValueError('unsafe opt-in marker')
    revisions = root / 'revisions'
    safe_dir(revisions)
    current = root / 'current'
    if not current.is_symlink():
        raise ValueError('asset revision is missing')
    relative = os.readlink(current)
    if not re.fullmatch(r'revisions/[a-f0-9]{64}', relative):
        raise ValueError('unsafe asset revision')
    target = current.resolve(strict=True)
    if target.parent != revisions.resolve(strict=True):
        raise ValueError('asset revision escapes directory')
    safe_dir(target)
    hashes = {}
    for name in FILES:
        fd = os.open(target / name, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
                    or not info.st_mode & stat.S_IROTH or not 0 < info.st_size <= MAX_FILE):
                raise ValueError('invalid asset file')
            data = stream.read(MAX_FILE + 1)
            if len(data) != info.st_size:
                raise ValueError('asset size changed')
            hashes[name] = hashlib.sha256(data).hexdigest()
    revision = hashlib.sha256(SALT + json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    if revision != target.name:
        raise ValueError('asset revision hash mismatch')
    return True


def main():
    if len(sys.argv) != 2 or os.geteuid() != 0:
        raise ValueError('invalid invocation')
    return 0 if is_enabled(Path(sys.argv[1])) else 3


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, TypeError):
        print('RU_ASSETS_OPT_IN_INVALID', file=sys.stderr)
        raise SystemExit(1)
