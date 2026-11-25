"""Manage the blocked-site exception through Hiddify's parent-panel API.

Provision assets and verified template overrides on every node first. Panel API
writes can trigger automatic child synchronization and apply. The tool never
logs API credentials or user data, and does not perform independent health checks.
"""
import json
import os
from pathlib import Path
import sys

from template_install import LocalPanel, prepare

NAME = 'GameLife blocked sites'
GEO = ['geosite:ru-blocked', 'geoip:ru-blocked']
RULES = ['https://nl.gamelife.shop/assets/geodata/geosite-ru-blocked.srs',
         'https://nl.gamelife.shop/assets/geodata/geoip-ru-blocked.srs']
PAYLOAD = {'name': NAME, 'mode': 'core_outbound', 'enabled': True,
           'config': '{"type":"direct"}', 'sites': [], 'geosites': GEO,
           'rule_sets': RULES, 'domestic': False}


def snapshot(panel):
    result = panel.request('GET', 'outbounds/')
    rows = result.get('outbounds') if isinstance(result, dict) else None
    if not isinstance(rows, list) or not rows or result.get('managed_by_parent'):
        raise ValueError('unexpected parent outbounds')
    ids = [r.get('id') for r in rows]
    if any(not isinstance(i, int) or i <= 0 for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('invalid outbound ids')
    enabled = [r for r in rows if r.get('enabled')]
    defaults = [r for r in rows if r.get('is_default')]
    if (len(defaults) != 1 or not enabled or defaults[0]['id'] != enabled[-1]['id']
            or defaults[0].get('mode') != 'direct'):
        raise ValueError('unexpected default outbound')
    blocks = [r for r in rows if r.get('mode') == 'block']
    if len(blocks) != 1 or not blocks[0].get('enabled') or not blocks[0].get('is_builtin'):
        raise ValueError('unexpected block outbound')
    block = blocks[0]
    if (not {'geosite:category-ru', 'geoip:ru'} <= set(block.get('geosites') or [])
            or 'ru' not in set(block.get('sites') or [])):
        raise ValueError('Russian protection is not as expected')
    if not (any('geosite-category-ru.srs' in s for s in block.get('rule_sets') or [])
            and any('geoip-ru.srs' in s for s in block.get('rule_sets') or [])):
        raise ValueError('missing Russian rule sets')
    return rows, block, defaults[0]


def matches(row):
    try:
        config = json.loads(row.get('config') or '')
    except (ValueError, TypeError):
        return False
    return (row.get('name') == NAME and row.get('mode') == 'core_outbound'
            and isinstance(row.get('enabled'), bool) and not row.get('is_default')
            and not row.get('domestic') and config == {'type': 'direct'}
            and set(row.get('geosites') or []) == set(GEO)
            and set(row.get('rule_sets') or []) == set(RULES)
            and not row.get('sites'))


def marked(rows):
    found = [r for r in rows if r.get('name') == NAME]
    if len(found) > 1 or (found and not matches(found[0])):
        raise ValueError('existing exception differs; review required')
    return found[0] if found else None


def backup(rows, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(rows, stream, ensure_ascii=True)
        stream.flush()
        os.fsync(stream.fileno())


def order_with_exception(rows, exception_id, block_id):
    ids = [row['id'] for row in rows if row['id'] != exception_id]
    ids.insert(ids.index(block_id), exception_id)
    return ids


def validate_final(panel, original_ids, block_id, default_id):
    rows, block, default = snapshot(panel)
    exc = marked(rows)
    if not exc or not exc.get('enabled') or block['id'] != block_id or default['id'] != default_id:
        raise RuntimeError('exception verification failed')
    ids = [r['id'] for r in rows]
    if [i for i in ids if i != exc['id']] != original_ids:
        raise RuntimeError('outbound order changed concurrently')
    if ids.index(exc['id']) + 1 != ids.index(block_id):
        raise RuntimeError('exception is not immediately before block')
    return exc


def deactivate_and_restore_order(panel, original_ids):
    rows, _, _ = snapshot(panel)
    exc = marked(rows)
    others = [r['id'] for r in rows if not exc or r['id'] != exc['id']]
    if set(others) != set(original_ids) or len(others) != len(original_ids):
        raise RuntimeError('outbounds changed concurrently')
    if exc and exc.get('enabled'):
        panel.request('PATCH', f'outbounds/{exc["id"]}/', {'enabled': False})
    rows, _, _ = snapshot(panel)
    current = [r['id'] for r in rows]
    exc = marked(rows)
    without_exception = [item for item in current if not exc or item != exc['id']]
    if without_exception != original_ids:
        desired = ([exc['id']] if exc else []) + original_ids
        panel.request('PUT', 'outbounds/order/', {'ids': desired})
    rows, _, _ = snapshot(panel)
    exc = marked(rows)
    if ([r['id'] for r in rows if not exc or r['id'] != exc['id']] != original_ids
            or (exc and exc.get('enabled'))):
        raise RuntimeError('outbound rollback verification failed')


def install(panel, backup_path):
    templates = prepare(panel.get_templates())
    if any(row['content'] != row['updated'] or not row['builtin_override'] for row in templates):
        raise ValueError('required guarded templates are not installed')
    rows, block, default = snapshot(panel)
    existing = marked(rows)
    if existing:
        original = [r['id'] for r in rows if r['id'] != existing['id']]
        if existing.get('enabled'):
            validate_final(panel, original, block['id'], default['id'])
            return False
        before_ids = [r['id'] for r in rows]
        backup(rows, backup_path)
        try:
            panel.request('PATCH', f'outbounds/{existing["id"]}/', {'enabled': True})
            fresh, current_block, current_default = snapshot(panel)
            if current_block['id'] != block['id'] or current_default['id'] != default['id']:
                raise RuntimeError('unexpected outbound change')
            desired = order_with_exception(fresh, existing['id'], block['id'])
            if desired != [r['id'] for r in fresh]:
                panel.request('PUT', 'outbounds/order/', {'ids': desired})
            validate_final(panel, original, block['id'], default['id'])
        except Exception as error:
            try:
                panel.request('PATCH', f'outbounds/{existing["id"]}/', {'enabled': False})
                fresh, _, _ = snapshot(panel)
                if [r['id'] for r in fresh] != before_ids:
                    panel.request('PUT', 'outbounds/order/', {'ids': before_ids})
            except Exception as rollback:
                raise RuntimeError('outbound rollback incomplete') from rollback
            raise RuntimeError('outbound reactivation failed and was rolled back') from error
        return True
    original = [r['id'] for r in rows]
    backup(rows, backup_path)
    try:
        panel.request('POST', 'outbounds/', PAYLOAD)
        created_rows, current_block, current_default = snapshot(panel)
        exc = marked(created_rows)
        if (not exc or current_block['id'] != block['id'] or current_default['id'] != default['id']
                or [r['id'] for r in created_rows if r['id'] != exc['id']] != original):
            raise RuntimeError('unexpected outbound creation')
        desired = order_with_exception(created_rows, exc['id'], block['id'])
        if desired != [r['id'] for r in created_rows]:
            panel.request('PUT', 'outbounds/order/', {'ids': desired})
        validate_final(panel, original, block['id'], default['id'])
    except Exception as error:
        try:
            deactivate_and_restore_order(panel, original)
        except Exception as rollback:
            raise RuntimeError('outbound rollback incomplete') from rollback
        raise RuntimeError('outbound update failed and was rolled back') from error
    return True


def remove(panel, backup_path):
    rows, _, _ = snapshot(panel)
    exc = marked(rows)
    if not exc or not exc.get('enabled'):
        return False
    backup(rows, backup_path)
    try:
        panel.request('PATCH', f'outbounds/{exc["id"]}/', {'enabled': False})
        fresh, _, _ = snapshot(panel)
        current = marked(fresh)
        if (not current or current['id'] != exc['id'] or current.get('enabled')
                or [r['id'] for r in fresh] != [r['id'] for r in rows]):
            raise RuntimeError('outbound deactivation verification failed')
    except Exception as error:
        try:
            panel.request('PATCH', f'outbounds/{exc["id"]}/', {'enabled': True})
        except Exception as rollback:
            raise RuntimeError('outbound rollback incomplete') from rollback
        raise RuntimeError('outbound deactivation failed and was rolled back') from error
    return True


if __name__ == '__main__':
    if len(sys.argv) != 4 or sys.argv[1] not in ('install', 'remove') or os.geteuid() != 0:
        raise SystemExit('Usage: outbound_install.py install|remove STATE_FILE BACKUP_FILE (root only)')
    try:
        action = install if sys.argv[1] == 'install' else remove
        changed = action(LocalPanel(sys.argv[2]), sys.argv[3])
        print('OUTBOUND_STATUS=' + ('UPDATED' if changed else 'UNCHANGED'))
    except Exception as exc:
        print('OUTBOUND_ERROR=' + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
