"""PROTOTYPE: root-local whitelisted panel operations over an SSH stdin channel.

Not a production controller or a self-installing agent. The caller must pin SSH
host keys, verify this code, journal intents, provision assets on all nodes, and
independently verify child sync, running cores and real VPN paths. No secrets or
API response bodies are printed. Do not execute on a production VPS yet.
"""
import json
import os
from pathlib import Path
import re
import stat
import sys

import apply_monitor
import outbound_install
import template_install
import transaction

ROOT = Path('/root/services/gamelife-vpn/guard-transactions')
STATE_ROOT = Path('/root/services/gamelife-vpn')
NODES = {'main', 'nl', 'de', 'pl'}
ACTIONS = {'inspect', 'status', 'stage_templates', 'stage_outbound', 'apply', 'restore'}


def parse_command(value):
    if not isinstance(value, dict) or set(value) != {'action', 'node', 'tx'}:
        raise ValueError('unsupported command schema')
    action, node, tx = value['action'], value['node'], value['tx']
    if (not isinstance(action, str) or action not in ACTIONS
            or not isinstance(node, str) or node not in NODES
            or not isinstance(tx, str) or not re.fullmatch(r'[a-f0-9]{32}', tx)):
        raise ValueError('invalid action, node or transaction')
    if action == 'stage_outbound' and node != 'main':
        raise ValueError('outbounds managed by parent')
    return action, node, tx


def secure_directory(path, create=False):
    path = Path(path)
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError('unsafe transaction directory')
    return path


def tx_directory(tx, *, root=ROOT):
    secure_directory(root, create=True)
    return secure_directory(root / tx, create=True)


def safe_existing_file(path):
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return False
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError('unsafe backup file')
    return True


def inspect(panel):
    rows = template_install.prepare(panel.get_templates())
    templates_ready = all(row['content'] == row['updated'] and row['builtin_override'] for row in rows)
    data = panel.request('GET', 'outbounds/')
    items = data.get('outbounds') if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError('invalid outbound list')
    matching = [row for row in items if row.get('name') == outbound_install.NAME]
    if len(matching) > 1 or (matching and not outbound_install.matches(matching[0])):
        raise ValueError('unexpected exception rule')
    exception = matching[0] if matching else None
    blocks = [row for row in items if row.get('mode') == 'block' and row.get('enabled')]
    default = [row for row in items if row.get('is_default')]
    if len(blocks) != 1 or len(default) != 1:
        raise ValueError('invalid block or default')
    block = blocks[0]
    block_ready = ({'geosite:category-ru', 'geoip:ru'} <= set(block.get('geosites') or [])
                   and 'ru' in set(block.get('sites') or []))
    order_ok = bool(exception and exception.get('enabled')
                    and items.index(exception) + 1 == items.index(block))
    return {'templates_ready': templates_ready, 'block_ready': block_ready,
            'direct_default': default[0].get('mode') == 'direct',
            'exception_present': bool(exception),
            'exception_enabled': bool(exception and exception.get('enabled')),
            'exception_order_ok': order_ok}


def status(panel):
    state = panel.request('GET', 'apply/')
    if not isinstance(state, dict) or not isinstance(state.get('running'), list):
        raise ValueError('invalid apply state')
    # The caller only needs a busy flag; private log content is never returned.
    return {'busy': bool(state['running'])}


def require_no_enabled_asset_references(panel):
    data = panel.request('GET', 'outbounds/')
    rows = data.get('outbounds') if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError('unable to verify active outbounds')
    needles = ('geosite:ru-blocked', 'geoip:ru-blocked', 'geosite-ru-blocked.srs',
               'geoip-ru-blocked.srs', 'gamelife/current/')
    for row in rows:
        if row.get('enabled'):
            # API responses may contain private user data: inspect in memory and
            # never emit the serialized value or the offending row.
            content = json.dumps({key: row.get(key) for key in ('geosites', 'rule_sets', 'config')}).lower()
            if any(needle in content for needle in needles):
                raise ValueError('active outbound still depends on asset templates')


def execute(command, panel, directory):
    action, node, _ = parse_command(command)
    directory = Path(directory)
    if action == 'inspect':
        return inspect(panel)
    if action == 'status':
        return status(panel)
    if action == 'stage_templates':
        changed = template_install.install(panel, directory / 'templates.json')
        return {'changed': changed}
    if action == 'stage_outbound':
        changed = outbound_install.install(panel, directory / 'outbounds.json')
        return {'changed': changed}
    if action == 'apply':
        apply_monitor.start_and_wait(panel)
        return {'finished': True}
    # Only undo operations for which this transaction created a backup. An
    # existing exception with no backup must not be touched on rollback.
    outbound_backup = directory / 'outbounds.json'
    template_backup = directory / 'templates.json'
    if node == 'main' and safe_existing_file(outbound_backup):
        outbound_install.remove(panel, directory / 'outbounds-restore.json')
    if safe_existing_file(template_backup):
        require_no_enabled_asset_references(panel)
        transaction.restore_templates(panel, template_backup)
    return {'restored': True}


def main():
    if os.geteuid() != 0:
        raise ValueError('root required')
    raw = sys.stdin.buffer.read(4097)
    if len(raw) > 4096:
        raise ValueError('command too large')
    command = json.loads(raw)
    action, node, tx = parse_command(command)
    state = STATE_ROOT / ('admin-access.json' if node == 'main' else 'node-access.json')
    panel = template_install.LocalPanel(state)
    # Observations must not create or modify transaction directories.
    directory = ROOT / tx if action in ('inspect', 'status') else tx_directory(tx)
    result = execute(command, panel, directory)
    print(json.dumps({'ok': True, 'result': result}, separators=(',', ':')))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Only a whitelist of exception class names is safe to expose.  Error
        # messages, tracebacks and API bodies may contain private information.
        kind = type(exc).__name__
        if kind not in ('ValueError', 'RuntimeError', 'ApplyError', 'TimeoutError',
                        'ConnectionError', 'FileNotFoundError', 'PermissionError'):
            kind = 'OperationError'
        print(json.dumps({'ok': False, 'error': kind}, separators=(',', ':')))
        raise SystemExit(1)
