"""Remove the server-side RU-only deny rule when clients cannot bypass it.

This is a one-time emergency operation on the parent panel. It saves a private
backup first. The panel propagates the change to its nodes and can restart the
VPN cores, interrupting connections. Client-side routing remains unchanged.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
import sys

from template_install import LocalPanel

ROOT = Path('/root/services/gamelife-vpn')
RULE_NAMES = ('geosite-category-ru.srs', 'geoip-ru.srs')


def checked_rows(response):
    if not isinstance(response, dict) or response.get('managed_by_parent'):
        raise ValueError('parent outbounds unavailable')
    rows = response.get('outbounds')
    if not isinstance(rows, list) or not rows or not all(isinstance(row, dict) for row in rows):
        raise ValueError('invalid outbound list')
    ids = [row.get('id') for row in rows]
    if (any(type(value) is not int or value <= 0 for value in ids)
            or len(ids) != len(set(ids))):
        raise ValueError('invalid outbound identities')
    block = [row for row in rows if row.get('mode') == 'block']
    defaults = [row for row in rows if row.get('is_default')]
    enabled = [row for row in rows if row.get('enabled')]
    if (len(block) != 1 or block[0].get('is_builtin') is not True
            or type(block[0].get('enabled')) is not bool
            or len(defaults) != 1 or not enabled or defaults[0] is not enabled[-1]
            or defaults[0].get('mode') != 'direct'):
        raise ValueError('unexpected routing order')
    target = block[0]
    rule_sets = target.get('rule_sets')
    if (set(target.get('sites') or []) != {'ru'}
            or set(target.get('geosites') or []) != {'geosite:category-ru', 'geoip:ru'}
            or not isinstance(rule_sets, list) or len(rule_sets) != len(RULE_NAMES)
            or any(not any(isinstance(item, str) and item.endswith('/' + name)
                           for item in rule_sets) for name in RULE_NAMES)
            or target.get('domestic')):
        raise ValueError('block includes unrelated rules')
    return rows, target


def backup(rows, directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
            or info.st_mode & 0o077):
        raise ValueError('unsafe backup directory')
    path = directory / ('before-' + secrets.token_hex(12) + '.json')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(rows, stream, ensure_ascii=True)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def disable_ru_only_block(panel, directory):
    rows, target = checked_rows(panel.request('GET', 'outbounds/'))
    if not target['enabled']:
        return False
    identities = [(row['id'], row.get('enabled'), row.get('is_default')) for row in rows]
    backup(rows, directory)
    panel.request('PATCH', f'outbounds/{target["id"]}/', {'enabled': False})
    new_rows, current = checked_rows(panel.request('GET', 'outbounds/'))
    expected = [(item[0], False if item[0] == target['id'] else item[1], item[2])
                for item in identities]
    if (current['id'] != target['id'] or current['enabled']
            or [(row['id'], row.get('enabled'), row.get('is_default')) for row in new_rows] != expected):
        raise RuntimeError('routing readback differs; reconcile before retry')
    return True


def main():
    if os.geteuid() != 0:
        raise ValueError('root required')
    fd = os.open(ROOT / 'deploy.lock', os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise ValueError('unsafe deployment lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        panel = LocalPanel(ROOT / 'admin-access.json')
        return disable_ru_only_block(panel, ROOT / 'ru-availability-backups')
    finally:
        os.close(fd)


if __name__ == '__main__':
    try:
        print('RU_AVAILABILITY_' + ('UPDATED' if main() else 'UNCHANGED'))
    except Exception as error:
        print('RU_AVAILABILITY_ERROR_' + type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
