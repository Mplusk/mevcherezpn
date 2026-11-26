"""Enable hot geodata refresh only when a VPN node has no live sessions.

This is a one-time bootstrap. New TCP handshakes are briefly gated on one idle
node while its panel applies the Xray schedule; a watchdog removes every gate.
Afterwards normal geodata refreshes do not restart either core. Unknown state,
an unsupported listener, or a concurrent panel apply fails closed.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import socket
import ssl
import stat
import subprocess
import sys
import tempfile

# Keep versioned deployment directories free of new bytecode artifacts.
sys.dont_write_bytecode = True

# Source checkout keeps the panel helpers next door; the installed bundle
# puts the same reviewed helper files in this script's directory.
GUARD = Path(__file__).resolve().parent.parent / 'ru_guard'
if Path(__file__).resolve().parent.name == 'ru_assets' and GUARD.is_dir():
    sys.path.insert(0, str(GUARD))

import apply_monitor
import cron_config
import hot_sync
import idle_gate
import probe_nodes
import template_install
import verify_generated

ROOT = Path('/root/services/gamelife-vpn')
ASSETS = ROOT / 'ru-assets'
JOURNAL = ASSETS / 'hot-bootstrap.json'
UNIT = Path('/etc/systemd/system/gamelife-ru-assets.service')
NODES = {'de': 'de.gamelife.shop', 'pl': 'pl.gamelife.shop', 'nl': 'nl1.gamelife.shop'}
OLD_EXEC = 'ExecStart=/usr/bin/python3 /opt/gamelife-ru-assets/sync.py'
NEW_EXEC = 'ExecStart=/usr/bin/python3 /opt/gamelife-ru-assets/hot/current/hot_sync.py'


def atomic_private(path, data):
    fd, name = tempfile.mkstemp(prefix='.hot-bootstrap-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_journal():
    info = JOURNAL.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError('unsafe bootstrap journal')
    value = json.loads(JOURNAL.read_text(encoding='utf-8'))
    if (not isinstance(value, dict) or set(value) != {'version', 'state', 'id', 'content', 'override'}
            or value['version'] != 1 or value['state'] not in ('prepared', 'patched', 'applying', 'applied', 'rolled_back', 'uncertain')
            or type(value['id']) is not int or value['id'] <= 0 or not isinstance(value['content'], str)
            or type(value['override']) is not bool):
        raise ValueError('invalid bootstrap journal')
    return value


def save_journal(value):
    atomic_private(JOURNAL, json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode())


def panel_for(node):
    if node not in ('main', *NODES):
        raise ValueError('unknown node')
    return template_install.LocalPanel(ROOT / ('admin-access.json' if node == 'main' else 'node-access.json'))


def locks(node):
    paths = [ROOT / 'deploy.lock']
    if node != 'main':
        paths.append(ROOT / 'relay-ci' / 'deploy.lock')
    paths.append(ASSETS / '.sync.lock')
    fds = []
    try:
        for path in paths:
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
                os.close(fd)
                raise ValueError('unsafe lock')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fds.append(fd)
        return fds
    except Exception:
        for fd in fds:
            os.close(fd)
        raise


def other_nodes_available(node):
    """Fail closed if another gateway's verified HTTPS listener is unavailable."""
    names = [host for label, host in NODES.items() if label != node]
    if len(names) < 2:
        raise ValueError('not enough independent gateways')
    context = ssl.create_default_context()
    for host in names:
        with socket.create_connection((host, 443), timeout=8) as raw:
            with context.wrap_socket(raw, server_hostname=host):
                pass


def panel_idle(panel):
    state = panel.request('GET', 'apply/')
    if not isinstance(state, dict) or not isinstance(state.get('running'), list) or state['running']:
        raise RuntimeError('panel apply is busy or unknown')


def verify_routes():
    for service in ('hiddify-xray.service', 'hiddify-core.service'):
        idle_gate.docker(['systemctl', 'is-active', '--quiet', service])
    xray = verify_generated.read_config('/opt/hiddify-manager/generated/xray.json')
    core = verify_generated.read_config('/opt/hiddify-manager/generated/hiddify-core.json')
    verify_generated.verify(xray, core, True)
    return xray


def verify_active():
    xray = verify_routes()
    if xray.get('geodata') != {'cron': cron_config.CRON}:
        raise ValueError('generated Xray schedule missing')
    hot_sync.inspect_runtime()


def enable_service():
    """Switch the existing asset timer to the validated hot-sync entry point."""
    info = UNIT.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError('unsafe asset service unit')
    text = UNIT.read_text(encoding='utf-8')
    if text.count(NEW_EXEC) == 1 and OLD_EXEC not in text:
        return
    if text.count(OLD_EXEC) != 1 or NEW_EXEC in text:
        raise ValueError('unexpected asset service unit')
    backup = ASSETS / 'hot-bootstrap-unit-before.service'
    if not backup.exists():
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(text.encode())
            stream.flush()
            os.fsync(stream.fileno())
    fd, name = tempfile.mkstemp(prefix='.gamelife-ru-assets-', dir=UNIT.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(text.replace(OLD_EXEC, NEW_EXEC, 1))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, UNIT)
        subprocess.run(['systemctl', 'daemon-reload'], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=45, check=True)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def restore_service():
    text = UNIT.read_text(encoding='utf-8')
    if text.count(OLD_EXEC) == 1 and NEW_EXEC not in text:
        return
    backup = ASSETS / 'hot-bootstrap-unit-before.service'
    info = backup.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError('unsafe service backup')
    before = backup.read_text(encoding='utf-8')
    if before.count(OLD_EXEC) != 1 or before.replace(OLD_EXEC, NEW_EXEC, 1) != text:
        raise ValueError('asset service differs from backup')
    fd, name = tempfile.mkstemp(prefix='.gamelife-ru-assets-', dir=UNIT.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(before)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, UNIT)
        subprocess.run(['systemctl', 'daemon-reload'], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=45, check=True)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def start_service():
    subprocess.run(['systemctl', 'start', 'gamelife-ru-assets.service'],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=420, check=True)


def rollback_idle(panel, node):
    """Restore the previous base only if no connection would be interrupted."""
    fds = locks(node)
    try:
        journal = read_journal()
        if journal['state'] != 'applied' or journal['override']:
            raise ValueError('unexpected rollback journal')
        panel_idle(panel)
        other_nodes_available(node)
        probe_nodes.probe([name for name in NODES if name != node])
        xports, coreports = idle_gate.inspect_inbounds(allow_empty_xray=(node == 'main'))
        with idle_gate.IdleGate(xports, coreports, watchdog_seconds=1200) as gate:
            gate.confirm()
            row, _ = cron_config.select(panel.request('GET', 'proxy-base-configs/?side=server&core=xray'))
            if (row['id'] != journal['id'] or not row.get('builtin_override')
                    or row['content'] != cron_config.transform(journal['content'])):
                raise ValueError('base changed since bootstrap')
            journal['state'] = 'uncertain'
            save_journal(journal)
            panel.request('PATCH', f'proxy-base-configs/{row["id"]}/', {'builtin_override': False})
            restored, _ = cron_config.select(panel.request('GET', 'proxy-base-configs/?side=server&core=xray'))
            if (restored['id'] != row['id'] or restored['content'] != journal['content']
                    or restored.get('builtin_override')):
                raise RuntimeError('base rollback readback failed')
            apply_monitor.start_and_wait(panel, timeout=420)
            verify_routes()
            restore_service()
            journal['state'] = 'rolled_back'
            save_journal(journal)
    finally:
        for fd in reversed(fds):
            os.close(fd)
    if node in NODES:
        probe_nodes.probe([node])


def run(node):
    panel = panel_for(node)
    fds = locks(node)
    applied = False
    try:
        if JOURNAL.exists() or JOURNAL.is_symlink():
            journal = read_journal()
            if journal['state'] == 'applied':
                verify_active()
                enable_service()
                for fd in reversed(fds):
                    os.close(fd)
                fds.clear()
                probe_nodes.probe(list(NODES))
                start_service()
                return 'already-enabled'
            # A previous write/apply may have completed despite a lost reply.
            # Never replay it until an operator has reconciled the journal.
            raise RuntimeError('bootstrap requires reconciliation')
        panel_idle(panel)
        xports, coreports = idle_gate.inspect_inbounds(allow_empty_xray=(node == 'main'))
        if idle_gate.active_connections(xports + coreports):
            raise idle_gate.Busy('node has active connections')
        verify_routes()
        row, updated = cron_config.preview(panel)
        if row.get('builtin_override') or row['content'] == updated:
            raise ValueError('existing base override requires review')
        other_nodes_available(node)
        probe_nodes.probe(list(NODES))
        with idle_gate.IdleGate(xports, coreports, watchdog_seconds=1200) as gate:
            panel_idle(panel)
            current, expected = cron_config.select(panel.request('GET', 'proxy-base-configs/?side=server&core=xray'))
            if current['id'] != row['id'] or current['content'] != row['content'] or expected != updated:
                raise ValueError('base changed during preflight')
            journal = {'version': 1, 'state': 'prepared', 'id': row['id'],
                       'content': row['content'], 'override': bool(row.get('builtin_override'))}
            save_journal(journal)
            apply_completed = False
            try:
                panel.request('PATCH', f'proxy-base-configs/{row["id"]}/',
                              {'content': updated, 'builtin_override': True})
                after, _ = cron_config.select(panel.request('GET', 'proxy-base-configs/?side=server&core=xray'))
                if after['id'] != row['id'] or after['content'] != updated or not after.get('builtin_override'):
                    raise RuntimeError('base configuration readback failed')
                journal['state'] = 'patched'
                save_journal(journal)
                journal['state'] = 'applying'
                save_journal(journal)
                apply_monitor.start_and_wait(panel, timeout=420)
                apply_completed = True
                verify_active()
                journal['state'] = 'applied'
                save_journal(journal)
                enable_service()
                applied = True
            except Exception:
                # The panel may have accepted a write before the response was
                # lost. Only attempt rollback after a definitely completed apply;
                # otherwise retain the backup for explicit reconciliation.
                journal['state'] = 'uncertain'
                save_journal(journal)
                if apply_completed:
                    try:
                        panel_idle(panel)
                        gate.confirm()
                        panel.request('PATCH', f'proxy-base-configs/{row["id"]}/',
                                      {'builtin_override': False})
                        restored, _ = cron_config.select(panel.request('GET', 'proxy-base-configs/?side=server&core=xray'))
                        if (restored['id'] != row['id'] or restored['content'] != row['content']
                                or restored.get('builtin_override')):
                            raise RuntimeError('base rollback readback failed')
                        apply_monitor.start_and_wait(panel, timeout=420)
                        verify_routes()
                        restore_service()
                        journal['state'] = 'rolled_back'
                        save_journal(journal)
                    except Exception:
                        journal['state'] = 'uncertain'
                        save_journal(journal)
                raise
    finally:
        for fd in reversed(fds):
            os.close(fd)
    if applied:
        try:
            probe_nodes.probe(list(NODES))
        except probe_nodes.ProbeFailed as error:
            if error.node == node:
                try:
                    rollback_idle(panel, node)
                except Exception:
                    # Never force a rollback over an active connection or an
                    # unknown panel state; the existing external monitor alerts.
                    pass
            raise
        start_service()
    return 'enabled'


if __name__ == '__main__':
    try:
        if os.geteuid() != 0 or len(sys.argv) != 2:
            raise ValueError('root and node name required')
        result = run(sys.argv[1])
        subprocess.run(['systemctl', 'disable', '--now', 'gamelife-hot-bootstrap.timer'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45, check=True)
        print('HOT_BOOTSTRAP_' + result.upper().replace('-', '_'))
    except idle_gate.Busy:
        print('HOT_BOOTSTRAP_WAITING_FOR_IDLE')
    except Exception as error:
        print('HOT_BOOTSTRAP_FAILED_' + type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
