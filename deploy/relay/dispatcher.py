"""Restricted SSH command: deploy only the tested public main revision."""

import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path('/root/services/gamelife-vpn/relay-ci')
REPOSITORY = 'Mplusk/mevcherezpn'
API = 'https://api.github.com/repos/' + REPOSITORY
FILES = ('renderer.py', 'release.py', 'dispatcher.py', 'compose.yaml',
         'gamelife-subrelay-refresh.service', 'gamelife-subrelay-refresh.timer')
LIFE_FILES = ('index.html', 'life.js', 'gateway.py')
RU_ASSET_FILES = ('installer.py', 'reduce.py', 'sync.py', 'compose.ru-assets.yaml',
                  'compose_plan.py', 'relay_hook.py', 'gamelife-ru-assets.service',
                  'gamelife-ru-assets.timer')


def parse_command(command):
    if command == 'relay-probe':
        return 'probe', None
    match = re.fullmatch(r'relay-deploy ([a-f0-9]{40})', command)
    if match:
        return 'deploy', match.group(1)
    raise ValueError('command denied')


def api_json(path):
    request = Request(API + path, headers={'User-Agent': 'gamelife-relay-ci',
                                           'Accept': 'application/vnd.github+json'})
    with urlopen(request, timeout=30) as response:
        body = response.read(2 * 1024 * 1024 + 1)
    if len(body) > 2 * 1024 * 1024:
        raise RuntimeError('API response too large')
    return json.loads(body)


def require_main(revision, get=api_json):
    data = get('/git/ref/heads/main')
    if (data.get('object') or {}).get('sha') != revision:
        raise RuntimeError('revision is not current main')


def require_verified(revision, get=api_json):
    params = urlencode({'head_sha': revision, 'event': 'push',
                        'status': 'completed', 'per_page': 20})
    data = get('/actions/workflows/ci.yml/runs?' + params)
    runs = data.get('workflow_runs') or []
    if not any(isinstance(run, dict) and run.get('head_sha') == revision
               and run.get('head_branch') == 'main' and run.get('event') == 'push'
               and run.get('conclusion') == 'success'
               and (run.get('head_repository') or {}).get('full_name') == REPOSITORY
               for run in runs):
        raise RuntimeError('revision has not passed CI')


def run(arguments, *, cwd=None, env=None, output=False, timeout=180):
    result = subprocess.run(arguments, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=timeout)
    if result.returncode:
        raise RuntimeError('operation failed')
    if output:
        if len(result.stdout) > 2 * 1024 * 1024:
            raise RuntimeError('output exceeds limit')
        return result.stdout
    return None


def check_relay():
    state = run(['docker', 'inspect', '--format', '{{.State.Status}}',
                 'gamelife-subrelay-relay-1'], output=True).strip()
    if state != b'running':
        raise RuntimeError('relay is not running')
    run(['systemctl', 'is-active', '--quiet', 'gamelife-subrelay-refresh.timer'])
    with socket.create_connection(('127.0.0.1', 8443), timeout=10):
        pass
    print('RELAY_STATUS ready', flush=True)


def extract_ru_assets(revision, directory, env, get=None):
    """Stage the fixed public asset installer sources without executing them."""
    if not re.fullmatch(r'[a-f0-9]{40}', revision):
        raise ValueError('invalid revision')
    get = get or run
    directory = Path(directory)
    target = directory / 'ru_assets'
    target.mkdir(mode=0o700)
    for name in RU_ASSET_FILES:
        content = get(['git', '-C', str(directory), 'show', revision + ':deploy/ru_assets/' + name],
                      env=env, output=True)
        fd = os.open(target / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)


def deploy(revision):
    require_main(revision)
    require_verified(revision)
    with tempfile.TemporaryDirectory(prefix='.source-', dir=ROOT) as temp:
        directory = Path(temp)
        env = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': temp,
               'XDG_CONFIG_HOME': temp, 'GIT_CONFIG_NOSYSTEM': '1',
               'GIT_TERMINAL_PROMPT': '0', 'LC_ALL': 'C'}
        run(['git', 'init', '--quiet', '--template=', temp], env=env)
        run(['git', '-c', 'credential.helper=', '-C', temp, 'fetch', '--quiet',
             '--no-tags', '--depth=1', 'https://github.com/' + REPOSITORY + '.git',
             'refs/heads/main'], env=env)
        fetched = run(['git', '-C', temp, 'rev-parse', '--verify', 'FETCH_HEAD^{commit}'],
                      env=env, output=True).decode('ascii').strip()
        if fetched != revision:
            raise RuntimeError('fetched revision differs')
        for name in FILES:
            content = run(['git', '-C', temp, 'show', revision + ':deploy/relay/' + name],
                          env=env, output=True)
            path = directory / name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content)
        life_dir = directory / 'life'
        life_dir.mkdir(mode=0o700)
        for name in LIFE_FILES:
            content = run(['git', '-C', temp, 'show', revision + ':deploy/life/' + name],
                          env=env, output=True)
            fd = os.open(life_dir / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content)
        extract_ru_assets(revision, directory, env)
        require_main(revision)
        result = subprocess.run(['/usr/bin/python3', str(directory / 'release.py'), 'apply', revision],
                                cwd=temp, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=600)
        output = result.stdout.decode('utf-8', errors='replace').splitlines()
        if result.returncode:
            if output == ['RELAY_ERROR rollback_incomplete']:
                print(output[0], flush=True)
            else:
                print('RELAY_ERROR deployment_failed', flush=True)
            raise SystemExit(1)
        if len(output) != 1 or output[0] not in ('RELAY_STATUS updated', 'RELAY_STATUS unchanged'):
            raise RuntimeError('unexpected deployment response')
        print(output[0], flush=True)


def main():
    import fcntl
    os.umask(0o077)
    if os.geteuid() != 0:
        raise RuntimeError('root is required')
    action, revision = parse_command(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
    if action == 'probe':
        check_relay()
        return
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (ROOT / 'deploy.lock').open('w') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        deploy(revision)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('RELAY_ERROR denied_or_failed', flush=True)
        raise SystemExit(1)
