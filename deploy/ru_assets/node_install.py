"""Stage a single node's routing asset mount with recovery and an external VPN probe.

Run as root on a node. JSON stdin holds the monitoring profile only in memory;
it is never used in an argument, environment variable, log or persistent file.
Template and parent-outbound activation are deliberately outside this command.
"""
from __future__ import annotations

import fcntl
import copy
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
import uuid

import installer
import sync

ROOT = Path('/root/services/gamelife-vpn')
ASSETS = ROOT / 'ru-assets'
STATE = ROOT / 'node-access.json'
ENVFILE = ROOT / '.env'
TXROOT = ASSETS / 'mount-transactions'
OVERLAY = ASSETS / 'compose.ru-assets.yaml'
MARKER = ASSETS / 'compose-enabled'
OVERLAY_SOURCE = Path(__file__).with_name('compose.ru-assets.yaml')
NODES = {'de': ('de.gamelife.shop', '2.27.75.91'),
         'pl': ('pl.gamelife.shop', '191.217.211.240'),
         'nl': ('nl1.gamelife.shop', '153.52.155.69')}
UUID_RE = re.compile(r'[a-f0-9]{32}\Z')
RELEASE_RE = re.compile(r'/root/services/gamelife-vpn/releases/[a-f0-9]{40}/compose\.panel\.yaml\Z')


def root_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError('unsafe private file')


def root_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError('unsafe transaction directory')


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_private(path, data):
    fd, name = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        fsync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def validate_request(request):
    if not isinstance(request, dict) or set(request) not in (
            {'action', 'node', 'outbound'}, {'action', 'node', 'outbound', 'transaction'}):
        raise ValueError('invalid request')
    action, node, outbound = request['action'], request['node'], request['outbound']
    if action not in ('stage', 'rollback') or node not in NODES or not isinstance(outbound, dict):
        raise ValueError('invalid request')
    if (action == 'rollback') != ('transaction' in request):
        raise ValueError('transaction required for rollback')
    if 'transaction' in request and not UUID_RE.fullmatch(request['transaction']):
        raise ValueError('invalid transaction')
    targets = outbound.get('settings', {}).get('vnext', [])
    stream = outbound.get('streamSettings', {})
    if (outbound.get('protocol') != 'vless' or len(targets) != 1
            or targets[0].get('address') != NODES[node][0]
            or stream.get('network') not in ('ws', 'websocket') or stream.get('security') != 'tls'):
        raise ValueError('unexpected monitoring endpoint')
    return action, node, outbound


def template_dependency():
    root_file(STATE)
    state = json.loads(STATE.read_text(encoding='utf-8'))
    owner = urlsplit(state['owner_url'])
    parts = owner.path.strip('/').split('/')
    host = state['bootstrap_hostname']
    if owner.scheme != 'https' or len(parts) != 2 or not host or any(not p for p in parts):
        raise ValueError('invalid panel state')
    class LocalConnection(http.client.HTTPSConnection):
        def connect(self):
            sock = socket.create_connection(('127.0.0.1', 28443), self.timeout)
            self.sock = self._context.wrap_socket(sock, server_hostname=host)
    conn = LocalConnection(host, timeout=20, context=ssl._create_unverified_context())
    try:
        conn.request('GET', '/' + parts[0] + '/api/v2/admin/proxy-templates/',
                     headers={'Host': host, 'Hiddify-API-Key': parts[1]})
        response = conn.getresponse()
        data = response.read(4 * 1024 * 1024 + 1)
        if response.status != 200 or len(data) > 4 * 1024 * 1024:
            raise RuntimeError('panel template check failed')
        rows = json.loads(data)
        if not isinstance(rows, list):
            raise ValueError('invalid template response')
        expected = {'xray/server/base/route', 'hiddify-core/server/base/route'}
        relevant = [row for row in rows if row.get('slug') in expected]
        if len(relevant) != 2 or {row.get('slug') for row in relevant} != expected:
            raise ValueError('missing route templates')
        if any(not isinstance(row.get('content'), str) for row in relevant):
            raise ValueError('invalid route template')
        return any('gamelife/current/' in row['content'] for row in relevant)
    finally:
        conn.close()


def run_silent(args, timeout=600):
    env = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/root',
           'LANG': 'C.UTF-8', 'VPN_RU_ASSET_ROOT': str(ASSETS)}
    result = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout, env=env)
    if result.returncode:
        raise RuntimeError('operation failed')


def probe(xray, outbound, expected):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    config = {'log': {'loglevel': 'none'},
              'inbounds': [{'listen': '127.0.0.1', 'port': port, 'protocol': 'socks',
                            'settings': {'auth': 'noauth', 'udp': False}}],
              'outbounds': [outbound]}
    env = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/nonexistent', 'LANG': 'C'}
    proc = None
    try:
        proc = subprocess.Popen([str(xray), 'run', '-c', 'stdin:'], stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        proc.stdin.write(json.dumps(config, separators=(',', ':')).encode())
        proc.stdin.close()
        time.sleep(1)
        if proc.poll() is not None:
            return False
        for url in ('https://api.ipify.org/', 'https://icanhazip.com/'):
            try:
                res = subprocess.run(['curl', '-fsS', '--noproxy', '', '--max-time', '15',
                                      '--max-filesize', '128',
                                      '--socks5-hostname', f'127.0.0.1:{port}', url],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20, env=env)
                if res.returncode == 0 and ipaddress.ip_address(res.stdout.decode().strip()) == ipaddress.ip_address(expected):
                    return True
            except (ValueError, UnicodeDecodeError, subprocess.TimeoutExpired):
                pass
        return False
    finally:
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def healthy(xray, outbound, expected, timeout=600):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            snap = installer.inspect_container()
            if snap['running'] and snap['health'] == 'healthy':
                run_silent(['docker', 'exec', installer.CONTAINER, 'systemctl', 'is-active', '--quiet', 'hiddify-xray.service'], 20)
                run_silent(['docker', 'exec', installer.CONTAINER, 'systemctl', 'is-active', '--quiet', 'hiddify-core.service'], 20)
                if probe(xray, outbound, expected):
                    return
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
            pass
        time.sleep(5)
    raise RuntimeError('VPN health check failed')


def compose_from_snapshot(snapshot):
    value = snapshot.get('config_files')
    if not isinstance(value, str):
        raise ValueError('unexpected release Compose path')
    paths = value.split(',')
    if len(paths) not in (1, 2) or not RELEASE_RE.fullmatch(paths[0]):
        raise ValueError('unexpected release Compose path')
    if len(paths) == 2 and paths[1] != str(OVERLAY):
        raise ValueError('unexpected Compose override')
    path = Path(paths[0])
    if path.is_symlink() or not path.is_file() or path.resolve() != path:
        raise ValueError('unsafe release Compose')
    return path


def compare_runtime(compose, snapshot, staged):
    adjusted = copy.deepcopy(snapshot)
    adjusted['config_files'] = str(compose)
    installer.validate_snapshot(adjusted, compose, ASSETS if staged else None)
    if not staged:
        installer.compare_runtime(compose, ENVFILE, adjusted)
        return
    def get(args):
        if args[:2] == ['docker', 'compose']:
            args = list(args)
            pos = args.index('config')
            args[pos:pos] = ['-f', str(OVERLAY)]
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/root',
                                     'LANG': 'C.UTF-8', 'VPN_RU_ASSET_ROOT': str(ASSETS)},
                                timeout=60, check=True)
        if len(result.stdout) > 4 * 1024 * 1024:
            raise ValueError('inspection output too large')
        return result.stdout
    installer.compare_runtime(compose, ENVFILE, adjusted, get)


def prepare_overlay():
    source = OVERLAY_SOURCE.read_bytes()
    if not source or len(source) > 16 * 1024:
        raise ValueError('invalid Compose overlay')
    if OVERLAY.exists() or OVERLAY.is_symlink():
        root_file(OVERLAY)
        if OVERLAY.read_bytes() != source:
            raise ValueError('existing Compose overlay differs')
    else:
        atomic_private(OVERLAY, source)


def marker_exists():
    if not MARKER.exists() and not MARKER.is_symlink():
        return False
    root_file(MARKER)
    info = MARKER.stat()
    if MARKER.read_bytes() != b'v1\n' or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError('invalid asset mount marker')
    return True


def enable_marker():
    if not marker_exists():
        atomic_private(MARKER, b'v1\n')


def verify_assets():
    installer.check_assets(ASSETS)
    root_info = ASSETS.lstat()
    target = (ASSETS / 'current').resolve(strict=True)
    for directory in (ASSETS, ASSETS / 'revisions', target):
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
                or info.st_mode & 0o005 != 0o005):
            raise ValueError('unsafe asset directory')
    if (root_info.st_uid != 0 or root_info.st_mode & 0o022
            or target.parent != (ASSETS / 'revisions').resolve(strict=True)
            or not re.fullmatch('[a-f0-9]{64}', target.name)):
        raise ValueError('unsafe asset revision')
    files = {}
    for name in sync.FILES:
        path = target / name
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('unsafe asset file')
        data = path.read_bytes()
        sync.validate(name, data)
        files[name] = data
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    revision = hashlib.sha256(b'gamelife-geo-manifest-v1\x00' +
                              json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    if revision != target.name:
        raise ValueError('asset revision checksum mismatch')
    sync.validate_xray(files)


def compose_up(compose, staged):
    args = ['docker', 'compose', '--env-file', str(ENVFILE), '-f', str(compose)]
    if staged:
        args += ['-f', str(OVERLAY)]
    run_silent(args + ['config', '--quiet'], 60)
    run_silent(args + ['up', '-d', '--no-deps', 'panel'], 600)
    installer.restore_gateway(installer.inspect_container(), run_silent)


def journal_read(path):
    root_file(path)
    data = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(data, dict) or set(data) != {'version', 'status', 'node', 'compose', 'sha256'}
            or data['version'] != 1 or data['status'] not in ('pending', 'committed', 'rolled_back')
            or data['node'] not in NODES or not RELEASE_RE.fullmatch(data['compose'])
            or not re.fullmatch('[a-f0-9]{64}', data['sha256'])):
        raise ValueError('invalid transaction')
    return data


def locks():
    handles = []
    try:
        for path in (ROOT / 'deploy.lock', ROOT / 'relay-ci' / 'deploy.lock'):
            fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
                os.close(fd)
                raise ValueError('unsafe deployment lock')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            handles.append(fd)
        return handles
    except Exception:
        for fd in handles:
            os.close(fd)
        raise


def apply_rollback(transaction, node, verify):
    txdir = TXROOT / transaction
    if txdir.is_symlink() or not txdir.is_dir():
        raise ValueError('transaction missing')
    journal = journal_read(txdir / 'journal.json')
    if journal['node'] != node or journal['status'] not in ('pending', 'committed'):
        raise ValueError('transaction cannot be rolled back')
    backup = txdir / 'compose.before'
    root_file(backup)
    contents = backup.read_bytes()
    if hashlib.sha256(contents).hexdigest() != journal['sha256']:
        raise ValueError('transaction backup mismatch')
    if template_dependency() is not False:
        raise RuntimeError('templates depend on mount; retain it')
    compose = Path(journal['compose'])
    snapshot = installer.inspect_container()
    if compose_from_snapshot(snapshot) != compose or compose.read_bytes() != contents:
        raise ValueError('current release differs; retain mount')
    compose_up(compose, False)
    verify()
    after = installer.inspect_container()
    compare_runtime(compose, after, False)
    if marker_exists():
        MARKER.unlink()
        fsync_dir(MARKER.parent)
    journal['status'] = 'rolled_back'
    atomic_private(txdir / 'journal.json', json.dumps(journal, sort_keys=True).encode())


def run(request):
    action, node, outbound = validate_request(request)
    if os.geteuid() != 0:
        raise ValueError('root required')
    root_file(STATE)
    root_file(ENVFILE)
    state = json.loads(STATE.read_text(encoding='utf-8'))
    if state.get('node_domain') != NODES[node][0]:
        raise ValueError('node identity does not match the requested profile')
    root_dir(TXROOT)
    handles = locks()
    try:
        with tempfile.TemporaryDirectory(prefix='ru-node-probe-') as tmp:
            binary = Path(tmp) / 'xray'
            run_silent(['docker', 'cp', installer.CONTAINER + ':/opt/hiddify-manager/services/xray/bin/xray', str(binary)], 60)
            binary.chmod(0o700)
            verify = lambda: healthy(binary, outbound, NODES[node][1])
            if action == 'rollback':
                apply_rollback(request['transaction'], node, verify)
                return {'status': 'rolled_back', 'transaction': request['transaction']}
            pending = []
            for path in TXROOT.iterdir():
                if path.is_dir() and UUID_RE.fullmatch(path.name):
                    try:
                        if journal_read(path / 'journal.json')['status'] == 'pending':
                            pending.append(path.name)
                    except Exception:
                        raise RuntimeError('invalid prior transaction') from None
            if pending:
                raise RuntimeError('unfinished prior transaction; recovery required')
            before = installer.inspect_container()
            compose = compose_from_snapshot(before)
            verify_assets()
            staged = len(before['config_files'].split(',')) == 2
            compare_runtime(compose, before, staged)
            if staged:
                prepare_overlay()
                verify()
                enable_marker()
                return {'status': 'unchanged'}
            if marker_exists():
                raise RuntimeError('mount marker exists before mounting')
            if template_dependency() is not False:
                raise RuntimeError('templates already depend on mount')
            verify()
            transaction = uuid.uuid4().hex
            txdir = TXROOT / transaction
            txdir.mkdir(mode=0o700)
            fsync_dir(TXROOT)
            original = compose.read_bytes()
            atomic_private(txdir / 'compose.before', original)
            journal = {'version': 1, 'status': 'pending', 'node': node, 'compose': str(compose),
                       'sha256': hashlib.sha256(original).hexdigest()}
            atomic_private(txdir / 'journal.json', json.dumps(journal, sort_keys=True).encode())
            try:
                prepare_overlay()
                compose_up(compose, True)
                verify()
                compare_runtime(compose, installer.inspect_container(), True)
                enable_marker()
            except Exception:
                try:
                    # Keep the mount whenever templates might depend on it.
                    apply_rollback(transaction, node, verify)
                except Exception:
                    pass
                raise
            journal['status'] = 'committed'
            atomic_private(txdir / 'journal.json', json.dumps(journal, sort_keys=True).encode())
            return {'status': 'staged', 'transaction': transaction}
    finally:
        for fd in handles:
            os.close(fd)


if __name__ == '__main__':
    try:
        raw = sys.stdin.buffer.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            raise ValueError('request too large')
        print(json.dumps(run(json.loads(raw)), separators=(',', ':')))
    except Exception as error:
        # Avoid printing exception text: library exceptions may retain secrets.
        print(json.dumps({'status': 'failed', 'error': type(error).__name__}, separators=(',', ':')))
        raise SystemExit(1)
