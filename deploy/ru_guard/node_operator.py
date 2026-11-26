"""Root-local template staging helper; cross-node coordination is mandatory.

Run from a trusted checked-out release containing deploy/ru_guard and
deploy/ru_assets. This script never sends or prints credentials. It does not
activate the parent outbound or prove connectivity from an external network.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'ru_assets'))
import installer
import node_install
import remote_agent
import template_install
import transaction
import apply_monitor
import verify_generated

NODES = {'main', 'nl', 'de', 'pl'}
FILES = ('geoip.dat', 'geosite.dat', 'geoip-ru-blocked.srs', 'geosite-ru-blocked.srs')
ROOT = Path('/root/services/gamelife-vpn')
ASSETS = ROOT / 'ru-assets'
TARGET = '/opt/hiddify-manager/services/xray/bin/gamelife'


def panel_for(node):
    name = 'admin-access.json' if node == 'main' else 'node-access.json'
    return template_install.LocalPanel(ROOT / name)


def locks_for(node):
    if node != 'main':
        return node_install.locks()
    # The primary release pipeline takes ROOT/deploy.lock; relay-ci is only
    # present on child gateways and must not be created on the primary.
    import fcntl
    import stat
    fd = os.open(ROOT / 'deploy.lock', os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise ValueError('unsafe deployment lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return [fd]
    except Exception:
        os.close(fd)
        raise


def require_mount():
    snapshot = installer.inspect_container()
    matches = [m for m in snapshot.get('mounts', []) if m.get('Destination') == TARGET]
    if (snapshot.get('running') is not True or snapshot.get('health') != 'healthy'
            or len(matches) != 1 or matches[0].get('Type') != 'bind'
            or matches[0].get('RW') is not False
            or Path(matches[0].get('Source') or '/').resolve() != ASSETS.resolve()):
        raise ValueError('expected healthy RO mount unavailable')
    node_install.verify_assets()
    # Compare what the container reads with the already validated host revision.
    import hashlib
    for name in FILES:
        expected = hashlib.sha256((ASSETS / 'current' / name).read_bytes()).hexdigest()
        result = subprocess.run(['docker', 'exec', installer.CONTAINER, 'sha256sum',
                                 TARGET + '/current/' + name], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=30, check=True)
        fields = result.stdout.split()
        if len(fields) != 2 or fields[0].decode('ascii') != expected:
            raise ValueError('container asset digest differs')


def require_idle(panel):
    if remote_agent.status(panel)['busy']:
        raise RuntimeError('panel apply is busy')


def verify_services():
    for service in ('hiddify-xray.service', 'hiddify-core.service'):
        subprocess.run(['docker', 'exec', installer.CONTAINER, 'systemctl', 'is-active', '--quiet', service],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, check=True)
def verify_staged():
    verify_services()
    xray = verify_generated.read_config('/opt/hiddify-manager/generated/xray.json')
    core = verify_generated.read_config('/opt/hiddify-manager/generated/hiddify-core.json')
    verify_generated.verify(xray, core, False)


def journal_write(directory, status):
    if status not in ('stage-intent', 'stage-complete', 'rollback-intent', 'rolled-back'):
        raise ValueError('bad journal status')
    node_install.atomic_private(directory / 'operator.json',
                                json.dumps({'version': 1, 'status': status}).encode())


def journal_read(directory):
    path = directory / 'operator.json'
    node_install.root_file(path)
    data = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(data, dict) or set(data) != {'version', 'status'}
            or data['version'] != 1 or data['status'] not in
            ('stage-intent', 'stage-complete', 'rollback-intent', 'rolled-back')):
        raise ValueError('bad operator journal')
    return data['status']


def execute(action, node, tx):
    if action not in ('preflight', 'stage', 'verify-staged', 'rollback') or node not in NODES:
        raise ValueError('unsupported operation')
    if not re.fullmatch('[a-f0-9]{32}', tx):
        raise ValueError('invalid transaction')
    panel = panel_for(node)
    locks = locks_for(node)
    try:
        require_idle(panel)
        require_mount()
        directory = remote_agent.ROOT / tx
        if action == 'preflight':
            remote_agent.require_no_enabled_asset_references(panel)
            rows = template_install.prepare(panel.get_templates())
            if any(row['content'] == row['updated'] or row['builtin_override'] for row in rows):
                raise ValueError('template state requires reconciliation')
            return
        if action == 'verify-staged':
            remote_agent.require_no_enabled_asset_references(panel)
            rows = template_install.prepare(panel.get_templates())
            if any(row['content'] != row['updated'] or not row['builtin_override'] for row in rows):
                raise ValueError('staged templates unavailable')
            verify_staged()
            return
        if action == 'stage':
            remote_agent.require_no_enabled_asset_references(panel)
            remote_agent.secure_directory(remote_agent.ROOT, create=True)
            # Exclusive mkdir ensures a retry after lost acknowledgement never
            # replays a write or overwrites the previous backup.
            directory.mkdir(mode=0o700)
            remote_agent.secure_directory(directory)
            node_install.fsync_dir(directory.parent)
            journal_write(directory, 'stage-intent')
            if not template_install.install(panel, directory / 'templates.json'):
                raise ValueError('templates were already changed')
            apply_monitor.start_and_wait(panel)
            verify_staged()
            journal_write(directory, 'stage-complete')
            return
        remote_agent.secure_directory(directory)
        if journal_read(directory) not in ('stage-intent', 'stage-complete', 'rollback-intent'):
            raise ValueError('invalid rollback state')
        remote_agent.require_no_enabled_asset_references(panel)
        if not remote_agent.safe_existing_file(directory / 'templates.json'):
            raise ValueError('no template backup; reconcile manually')
        journal_write(directory, 'rollback-intent')
        transaction.restore_templates(panel, directory / 'templates.json')
        apply_monitor.start_and_wait(panel)
        journal_write(directory, 'rolled-back')
    finally:
        for fd in locks:
            os.close(fd)


def main():
    if os.geteuid() != 0 or len(sys.argv) != 4:
        raise ValueError('root and action/node/transaction required')
    execute(*sys.argv[1:])
    print('OPERATOR_OK')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Never print API bodies, stack traces, keys or subscription contents.
        print('OPERATOR_FAILED_RECONCILE_BEFORE_RETRY', file=sys.stderr)
        raise SystemExit(1)
