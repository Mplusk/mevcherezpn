"""Attach CI to an already running relay using private state supplied over SSH."""

import json
import os
from pathlib import Path
import re
import stat
import sys
from urllib.parse import urlsplit
import uuid

from renderer import _UUID, render, validate_routes
from release import ROOT, SOURCE, atomic_write, private_json, request


def make_routes(manifest, host, node_path, admin_path):
    if not isinstance(manifest, dict) or set(manifest) != {'parent_host', 'parent_path', 'nl_path'}:
        raise ValueError('invalid private manifest')
    aliases = list(dict.fromkeys([manifest['parent_path'], manifest['nl_path'], node_path]))
    routes = {'version': 1, 'host': host, 'parent_host': manifest['parent_host'],
              'parent_path': manifest['parent_path'], 'aliases': aliases}
    validate_routes(routes)
    if not re.fullmatch(r'[A-Za-z0-9_-]{5,80}', admin_path) or admin_path in aliases:
        raise ValueError('admin and subscription routes overlap')
    return routes


def bootstrap(manifest, root=ROOT, source=SOURCE):
    directory = root / 'subscription-relay-backup'
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise RuntimeError('unsafe relay directory')
    state = private_json(root / 'node-access.json')
    owner = urlsplit(state['owner_url'])
    parts = owner.path.strip('/').split('/')
    if owner.scheme != 'https' or not owner.hostname or len(parts) != 2:
        raise RuntimeError('invalid node state')
    admin_path, key = parts
    uuid.UUID(key)
    status, body = request(owner.hostname, 443, '/' + admin_path + '/api/v2/admin/settings/',
                           {'Hiddify-API-Key': key})
    if status != 200:
        raise RuntimeError('node settings unavailable')
    settings = json.loads(body)
    paths = [field['value'] for category in settings.get('categories', [])
             for field in category.get('fields', []) if field.get('key') == 'proxy_path_client']
    if len(paths) != 1:
        raise RuntimeError('node client route unavailable')
    routes = make_routes(manifest, owner.hostname, paths[0], admin_path)
    current = (directory / 'Caddyfile').read_text(encoding='utf-8')
    expected = render(routes)
    previous = render(routes, privacy_logs=False)
    legacy = previous.replace(_UUID, '[0-9a-fA-F-]{36}')
    if current not in (expected, previous, legacy):
        raise RuntimeError('existing relay needs manual review')
    ci = root / 'relay-ci'
    ci.mkdir(mode=0o700, exist_ok=True)
    ci_info = ci.lstat()
    if not stat.S_ISDIR(ci_info.st_mode) or ci_info.st_uid != os.geteuid() or ci_info.st_mode & 0o077:
        raise RuntimeError('unsafe CI directory')
    state_path = directory / 'routes.json'
    dispatcher_path = ci / 'dispatcher.py'
    dispatcher = (source / 'dispatcher.py').read_bytes()
    if state_path.exists() and private_json(state_path) != routes:
        raise RuntimeError('existing route state differs')
    if dispatcher_path.exists() and dispatcher_path.read_bytes() != dispatcher:
        raise RuntimeError('existing CI dispatcher differs')
    if not state_path.exists():
        atomic_write(state_path, (json.dumps(routes) + '\n').encode(), 0o600)
    if not dispatcher_path.exists():
        atomic_write(dispatcher_path, dispatcher, 0o700)
    return routes


if __name__ == '__main__':
    try:
        os.umask(0o077)
        if os.geteuid() != 0:
            raise RuntimeError('root required')
        bootstrap(json.load(sys.stdin))
        print('RELAY_STATUS bootstrapped')
    except Exception:
        print('RELAY_ERROR bootstrap_failed')
        raise SystemExit(1)
