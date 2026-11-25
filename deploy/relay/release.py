"""Deploy the subscription relay without exposing private route state."""

import http.client
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
import uuid

from renderer import render, validate_routes


ROOT = Path('/root/services/gamelife-vpn')
SOURCE = Path(__file__).resolve().parent
UNITS = Path('/etc/systemd/system')
AGENT = 'HiddifyNext/5.9.15 (android) like ClashMeta v2ray sing-box'


class RollbackError(RuntimeError):
    pass


def private_json(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
        raise RuntimeError('private state has unsafe permissions')
    return json.loads(path.read_text(encoding='utf-8'))


class LocalHTTPS(http.client.HTTPSConnection):
    def connect(self):
        sock = socket.create_connection(('127.0.0.1', self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def request(host, port, path, headers=None):
    connection = LocalHTTPS(host, port=port, timeout=45, context=ssl.create_default_context())
    try:
        connection.request('GET', path, headers=headers or {})
        response = connection.getresponse()
        body = response.read(2 * 1024 * 1024 + 1)
        if len(body) > 2 * 1024 * 1024:
            raise RuntimeError('response exceeds limit')
        return response.status, body
    finally:
        connection.close()


def probe(routes, root=ROOT):
    host, _, parent_path, aliases = validate_routes(routes)
    state = private_json(root / 'node-access.json')
    owner = urlsplit(state['owner_url'])
    parts = owner.path.strip('/').split('/')
    if owner.scheme != 'https' or owner.hostname != host or len(parts) != 2:
        raise RuntimeError('node state does not match relay')
    admin_path, key = parts
    if not re.fullmatch(r'[A-Za-z0-9_-]{5,80}', admin_path) or admin_path in aliases:
        raise RuntimeError('invalid node state')
    uuid.UUID(key)
    status, _ = request(host, 8443, '/')
    if status != 404:
        raise RuntimeError('unexpected relay root')
    invalid = '/' + parent_path + '/' + str(uuid.uuid4()) + '/'
    status, body = request(host, 8443, invalid, {'User-Agent': AGENT})
    if status not in (302, 401, 403, 404):
        raise RuntimeError('unknown user was not rejected')
    try:
        decoded = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        decoded = None
    if isinstance(decoded, dict) and decoded.get('outbounds'):
        raise RuntimeError('unknown user received a profile')
    status, body = request(host, 443, '/' + admin_path + '/api/v2/admin/user/',
                           {'Hiddify-API-Key': key})
    if status == 404:
        return
    if status != 200:
        raise RuntimeError('node user status unavailable')
    users = json.loads(body)
    if not isinstance(users, list):
        raise RuntimeError('invalid node user response')
    active = [item for item in users if item.get('is_active') and item.get('uuid')]
    if active:
        user_id = str(uuid.UUID(str(active[0]['uuid'])))
        status, body = request(host, 8443, '/' + parent_path + '/' + user_id + '/',
                               {'User-Agent': AGENT})
        if status != 200:
            raise RuntimeError('existing subscription unavailable')
        profile = json.loads(body)
        if not isinstance(profile, dict) or not profile.get('outbounds'):
            raise RuntimeError('existing subscription is incomplete')


class Runner:
    def run(self, arguments):
        result = subprocess.run(arguments, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=180)
        if result.returncode:
            raise RuntimeError('relay operation failed')


def atomic_write(path, data, mode):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.relay-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def snapshot(paths):
    result = {}
    for path in paths:
        try:
            info = path.lstat()
        except FileNotFoundError:
            result[path] = None
            continue
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError('unexpected configuration file type')
        result[path] = (path.read_bytes(), stat.S_IMODE(info.st_mode))
    return result


class Deployment:
    def __init__(self, root=ROOT, units=UNITS, source=SOURCE, runner=None, health=None):
        self.root, self.units, self.source = root, units, source
        self.directory = root / 'subscription-relay-backup'
        self.ci_dir = root / 'relay-ci'
        self.runner = runner or Runner()
        self.health = health or (lambda routes: probe(routes, root))

    def compose(self, file):
        return ['docker', 'compose', '-p', 'gamelife-subrelay', '-f', str(file)]

    def apply(self, revision):
        if not re.fullmatch(r'[a-f0-9]{40}', revision):
            raise ValueError('invalid revision')
        routes = private_json(self.directory / 'routes.json')
        caddy = render(routes).encode('utf-8')
        compose_data = (self.source / 'compose.yaml').read_bytes()
        service_data = (self.source / 'gamelife-subrelay-refresh.service').read_bytes()
        timer_data = (self.source / 'gamelife-subrelay-refresh.timer').read_bytes()
        dispatcher_data = (self.source / 'dispatcher.py').read_bytes()
        caddy_path = self.directory / 'Caddyfile'
        compose_path = self.directory / 'compose.yaml'
        service_path = self.units / 'gamelife-subrelay-refresh.service'
        timer_path = self.units / 'gamelife-subrelay-refresh.timer'
        dispatcher_path = self.ci_dir / 'dispatcher.py'
        targets = [caddy_path, compose_path, service_path, timer_path, dispatcher_path]
        before = snapshot(targets)
        if any(before[path] is None for path in (caddy_path, compose_path, service_path, timer_path)):
            raise RuntimeError('relay has not been bootstrapped')
        wanted = {caddy_path: (caddy, 0o600), compose_path: (compose_data, 0o600),
                  service_path: (service_data, 0o644), timer_path: (timer_data, 0o644),
                  dispatcher_path: (dispatcher_data, 0o700)}
        changed = {path for path in targets if before[path] != wanted[path]}
        gateway_changed = bool(changed & {caddy_path, compose_path})
        units_changed = bool(changed & {service_path, timer_path})
        with tempfile.TemporaryDirectory(prefix='.relay-check-', dir=self.directory) as temp:
            stage = Path(temp)
            atomic_write(stage / 'Caddyfile', caddy, 0o600)
            atomic_write(stage / 'compose.yaml', compose_data, 0o600)
            candidate = self.compose(stage / 'compose.yaml')
            self.runner.run(candidate + ['config', '--quiet'])
            self.runner.run(candidate + ['run', '--rm', '--no-deps', 'relay', 'caddy',
                                         'validate', '--config', '/etc/caddy/Caddyfile',
                                         '--adapter', 'caddyfile'])
        if changed:
            backup = self.directory / ('rollback-' + revision[:12] + '-' + str(time.time_ns()))
            backup.mkdir(mode=0o700)
            for index, path in enumerate(targets):
                old = before[path]
                if old is not None:
                    atomic_write(backup / str(index), old[0], 0o600)
        try:
            for path in targets:
                if path in changed and path != dispatcher_path:
                    atomic_write(path, *wanted[path])
            if units_changed:
                self.runner.run(['systemctl', 'daemon-reload'])
                self.runner.run(['systemctl', 'enable', '--now', 'gamelife-subrelay-refresh.timer'])
            if gateway_changed:
                self.runner.run(self.compose(compose_path) +
                                ['up', '-d', '--force-recreate', '--wait', '--wait-timeout', '90', 'relay'])
            self.runner.run(['systemctl', 'is-active', '--quiet', 'gamelife-subrelay-refresh.timer'])
            self.health(routes)
            if dispatcher_path in changed:
                atomic_write(dispatcher_path, *wanted[dispatcher_path])
            atomic_write(self.ci_dir / 'current-revision', (revision + '\n').encode(), 0o600)
        except Exception as error:
            rollback_failed = False
            for path in targets:
                if path not in changed:
                    continue
                try:
                    old = before[path]
                    if old is None:
                        path.unlink()
                    else:
                        atomic_write(path, *old)
                except FileNotFoundError:
                    if before[path] is not None:
                        rollback_failed = True
                except Exception:
                    rollback_failed = True
            if units_changed:
                try:
                    self.runner.run(['systemctl', 'daemon-reload'])
                except Exception:
                    rollback_failed = True
            if gateway_changed:
                try:
                    self.runner.run(self.compose(compose_path) +
                                    ['up', '-d', '--force-recreate', '--wait', '--wait-timeout', '90', 'relay'])
                except Exception:
                    rollback_failed = True
            if rollback_failed:
                raise RollbackError('deployment failed; rollback incomplete') from error
            raise RuntimeError('deployment failed; rollback completed') from error
        return bool(changed)


def main():
    import sys
    os.umask(0o077)
    if len(sys.argv) != 3 or sys.argv[1] != 'apply' or os.geteuid() != 0:
        raise RuntimeError('invalid deployment invocation')
    changed = Deployment().apply(sys.argv[2])
    print('RELAY_STATUS ' + ('updated' if changed else 'unchanged'))


if __name__ == '__main__':
    try:
        main()
    except RollbackError:
        print('RELAY_ERROR rollback_incomplete', flush=True)
        raise SystemExit(1)
    except Exception:
        print('RELAY_ERROR deployment_failed', flush=True)
        raise SystemExit(1)
