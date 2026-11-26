"""Root-local activation/deactivation; called only by a cross-node controller.

This module never removes templates or mounts. After any ambiguous operation,
reconcile the panel/apply state before running rollback. No credentials or API
responses are printed.
"""
import json
import os
import re

import apply_monitor
import node_install
import node_operator
import outbound_install
import remote_agent
import template_install
import verify_generated


def write_state(directory, state, *, first=False):
    if state not in ('activate-intent', 'active', 'disable-intent', 'disabled'):
        raise ValueError('invalid outbound journal state')
    path = directory / 'activation.json'
    data = json.dumps({'version': 1, 'state': state}, separators=(',', ':')).encode()
    if first:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        node_install.fsync_dir(directory)
    else:
        node_install.root_file(path)
        node_install.atomic_private(path, data)


def read_state(directory):
    path = directory / 'activation.json'
    node_install.root_file(path)
    value = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(value, dict) or set(value) != {'version', 'state'}
            or value['version'] != 1 or value['state'] not in
            ('activate-intent', 'active', 'disable-intent', 'disabled')):
        raise ValueError('invalid outbound journal')
    return value['state']


def check_templates(panel):
    rows = template_install.prepare(panel.get_templates())
    if any(row['content'] != row['updated'] or not row['builtin_override'] for row in rows):
        raise ValueError('guarded templates unavailable')


def check_active(panel):
    check_templates(panel)
    info = remote_agent.inspect(panel)
    if not all(info.get(key) is True for key in ('templates_ready', 'block_ready', 'direct_default',
                                                  'exception_present', 'exception_enabled',
                                                  'exception_order_ok')):
        raise ValueError('active outbound readback failed')
    # The same check checks both services, then routes in memory.
    node_operator.verify_services()
    xray = verify_generated.read_config('/opt/hiddify-manager/generated/xray.json')
    core = verify_generated.read_config('/opt/hiddify-manager/generated/hiddify-core.json')
    verify_generated.verify(xray, core, True)


def restore_original_order(panel, backup_path):
    """Restore ordering for a previously disabled exception without deleting IDs."""
    node_install.root_file(backup_path)
    before = json.loads(backup_path.read_text(encoding='utf-8'))
    if not isinstance(before, list):
        raise ValueError('invalid outbound backup')
    original_ids = [row.get('id') for row in before]
    if (any(not isinstance(value, int) or value <= 0 for value in original_ids)
            or len(set(original_ids)) != len(original_ids)):
        raise ValueError('invalid outbound backup ids')
    prior_exception = outbound_install.marked(before)
    current, _, _ = outbound_install.snapshot(panel)
    exception = outbound_install.marked(current)
    if not exception or exception.get('enabled'):
        raise ValueError('exception is not safely disabled')
    ids = [row['id'] for row in current]
    if prior_exception:
        if prior_exception.get('enabled') or prior_exception['id'] != exception['id'] or set(ids) != set(original_ids):
            raise ValueError('outbound identities changed')
        if ids != original_ids:
            panel.request('PUT', 'outbounds/order/', {'ids': original_ids})
        current, _, _ = outbound_install.snapshot(panel)
        if [row['id'] for row in current] != original_ids:
            raise ValueError('outbound order was not restored')
    elif [value for value in ids if value != exception['id']] != original_ids:
        raise ValueError('unrelated outbound order changed')


def execute(action, node, tx):
    if (action not in ('verify-active', 'activate', 'disable') or node not in node_operator.NODES
            or not re.fullmatch('[a-f0-9]{32}', tx)
            or (action != 'verify-active' and node != 'main')):
        raise ValueError('unsupported action')
    panel = node_operator.panel_for(node)
    locks = node_operator.locks_for(node)
    try:
        node_operator.require_idle(panel)
        node_operator.require_mount()
        if action == 'verify-active':
            check_active(panel)
            return
        directory = remote_agent.ROOT / tx
        remote_agent.secure_directory(directory)
        if node_operator.journal_read(directory) != 'stage-complete':
            raise ValueError('template transaction is not staged')
        if action == 'activate':
            check_templates(panel)
            remote_agent.require_no_enabled_asset_references(panel)
            node_operator.verify_staged()
            rows, _, _ = outbound_install.snapshot(panel)
            existing = outbound_install.marked(rows)
            if existing and existing.get('enabled'):
                raise ValueError('existing active exception requires reconciliation')
            write_state(directory, 'activate-intent', first=True)
            if not outbound_install.install(panel, directory / 'outbounds-before.json'):
                raise ValueError('outbound was not changed')
            apply_monitor.start_and_wait(panel)
            check_active(panel)
            write_state(directory, 'active')
            return
        if read_state(directory) not in ('activate-intent', 'active'):
            raise ValueError('rollback requires reconciliation')
        if not remote_agent.safe_existing_file(directory / 'outbounds-before.json'):
            raise ValueError('missing original outbound backup')
        write_state(directory, 'disable-intent')
        outbound_install.remove(panel, directory / 'outbounds-before-disable.json')
        restore_original_order(panel, directory / 'outbounds-before.json')
        apply_monitor.start_and_wait(panel)
        remote_agent.require_no_enabled_asset_references(panel)
        node_operator.verify_staged()
        write_state(directory, 'disabled')
    finally:
        for fd in locks:
            os.close(fd)
