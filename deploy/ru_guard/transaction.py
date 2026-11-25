"""PROTOTYPE ONLY: guarded transaction orchestration; no production adapter.

This module is not a deployment tool. A future adapter must securely implement
and test every callback against all four hosts, including interrupted calls and
persistent mount journaling. Do not use on production based on these unit tests.
"""
from pathlib import Path
import json
import os
import tempfile

import outbound_install
import template_install


class TransactionError(RuntimeError):
    pass


class RollbackIncomplete(TransactionError):
    """Keep mounts in place: old state and data plane were not proven."""


def require_nodes(result, nodes, fields):
    if not isinstance(result, dict) or set(result) != set(nodes):
        raise TransactionError('node result is incomplete')
    for node in nodes:
        data = result[node]
        if not isinstance(data, dict) or any(data.get(field) is not True for field in fields):
            raise TransactionError('node verification failed')


def restore_templates(panel, path):
    if not path.exists():
        return
    rows = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(rows, list) or len(rows) != 2:
        raise TransactionError('invalid template backup')
    expected = set(template_install.TRANSFORMS)
    if {row.get('slug') for row in rows} != expected:
        raise TransactionError('unexpected template backup')
    current = panel.get_templates()
    for row in rows:
        found = [item for item in current if item.get('slug') == row['slug']]
        if len(found) != 1 or found[0].get('id') != row.get('id'):
            raise TransactionError('template identity changed')
    for row in reversed(rows):
        body = ({'builtin_override': True, 'content': row['content']}
                if row['builtin_override'] else {'builtin_override': False})
        panel.patch_template(row['id'], body)
    current = panel.get_templates()
    for row in rows:
        found = [item for item in current if item.get('slug') == row['slug']]
        if (len(found) != 1 or bool(found[0].get('builtin_override')) != row['builtin_override']
                or found[0].get('content') != row['content']):
            raise TransactionError('template restoration unverified')


def run(panel, driver, nodes, backup_root):
    """Run only with a future audited adapter; see README for callback contract.

    Driver must journal mount ownership *before* each write so rollback_mounts
    can restore a partial prepare_mounts call. Each callback returns verified
    facts, not merely a successful command exit code.
    """
    nodes = tuple(nodes)
    if len(nodes) != 4 or len(set(nodes)) != 4 or any(not isinstance(n, str) or not n for n in nodes):
        raise TransactionError('exactly four distinct named nodes required')
    root = Path(backup_root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = root.lstat()
    if not root.is_dir() or root.is_symlink() or (os.name != 'nt' and (info.st_uid != 0 or info.st_mode & 0o077)):
        raise TransactionError('unsafe backup directory')
    directory = Path(tempfile.mkdtemp(prefix='route-', dir=root))
    templates_backups = {node: directory / f'templates-{index}.json'
                         for index, node in enumerate(nodes)}
    outbound_backup = directory / 'outbound.json'
    rollback_backup = directory / 'outbound-rollback.json'

    # Validate every dependency before the first change, including a healthy
    # original data plane; existing user overrides cause prepare() to fail.
    panels = driver.node_panels()
    if (not isinstance(panels, dict) or set(panels) != set(nodes)
            or getattr(driver, 'parent_node', None) not in nodes
            or panels[driver.parent_node] is not panel):
        raise TransactionError('all distinct node panel adapters are required')
    prepared = {node: template_install.prepare(panels[node].get_templates()) for node in nodes}
    old_rows, _, _ = outbound_install.snapshot(panel)
    old_exception = outbound_install.marked(old_rows)
    if old_exception and old_exception.get('enabled'):
        if any(any(row['content'] != row['updated'] or not row['builtin_override']
                   for row in prepared[node]) for node in nodes):
            raise TransactionError('active exception with unprepared node templates')
    require_nodes(driver.preflight(), nodes, ('host_verified', 'version_pinned', 'baseline_healthy'))
    mount_attempted = False
    panel_attempted = False
    try:
        mount_attempted = True
        driver.prepare_mounts()
        require_nodes(driver.verify_mounts(), nodes,
                      ('host_verified', 'version_pinned', 'mount_persistent',
                       'assets_valid', 'xray_valid', 'singbox_valid'))
        panel_attempted = True
        # Each child maintains its own template database; ordinary outbound
        # synchronization does not copy parent template overrides.
        for node in nodes:
            template_install.install(panels[node], templates_backups[node])
        require_nodes(driver.apply(), nodes, ('applied',))
        require_nodes(driver.verify_baseline(), nodes, ('baseline_restored', 'vpn_healthy'))
        for node in nodes:
            rows = template_install.prepare(panels[node].get_templates())
            if any(row['content'] != row['updated'] or not row['builtin_override'] for row in rows):
                raise TransactionError('node template readback failed')
        outbound_install.install(panel, outbound_backup)
        require_nodes(driver.apply(), nodes, ('applied',))
        require_nodes(driver.verify_sync(), nodes, ('guard_active', 'exception_active'))
        require_nodes(driver.verify_new(), nodes,
                      ('vpn_healthy', 'blocked_via_vpn', 'ru_guarded',
                       'private_guarded', 'torrent_guarded'))
        if driver.verify_subscription() is not True:
            raise TransactionError('subscription verification failed')
        return directory
    except Exception as error:
        try:
            if panel_attempted:
                # Preserve an exception that was already active before this run.
                if not old_exception or not old_exception.get('enabled'):
                    outbound_install.remove(panel, rollback_backup)
                for node in reversed(nodes):
                    restore_templates(panels[node], templates_backups[node])
                require_nodes(driver.apply(), nodes, ('applied',))
                require_nodes(driver.verify_baseline(), nodes, ('baseline_restored', 'vpn_healthy'))
            elif mount_attempted:
                require_nodes(driver.verify_baseline(), nodes, ('baseline_restored', 'vpn_healthy'))
            if mount_attempted:
                driver.rollback_mounts()
                require_nodes(driver.verify_baseline(), nodes, ('baseline_restored', 'vpn_healthy'))
        except Exception as rollback:
            raise RollbackIncomplete('rollback unverified; inspect mount state and backups') from rollback
        raise TransactionError('transaction failed and prior state was restored') from error
