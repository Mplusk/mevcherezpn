"""Prepare Xray's supported hot-geodata schedule in its panel base template.

This module prepares and validates configuration only.  Installing or applying
it is a separate operation, because restarting a live Xray drops its sessions.
"""
from __future__ import annotations

import json
import subprocess

CRON = '*/10 * * * *'
OLD = '  "dns": {},\n'
NEW = OLD + '  "geodata": {"cron": "' + CRON + '"},\n'
CONTAINER = 'gamelife-hiddify-panel-1'


def transform(content):
    if not isinstance(content, str):
        raise ValueError('unexpected base configuration')
    if content.count(NEW) == 1 and content.count('"geodata"') == 1:
        return content
    if content.count(OLD) != 1 or '"geodata"' in content:
        raise ValueError('unexpected base configuration or existing schedule')
    return content.replace(OLD, NEW, 1)


def select(rows):
    if not isinstance(rows, list):
        raise ValueError('unexpected base configuration list')
    selected = [row for row in rows if row.get('side') == 'server' and row.get('core') == 'xray'
                and row.get('enable') is True]
    if len(selected) != 1:
        raise ValueError('expected exactly one active Xray server base')
    row = selected[0]
    if not row.get('is_builtin') or type(row.get('id')) is not int or row['id'] <= 0:
        raise ValueError('unexpected base identity')
    updated = transform(row.get('content'))
    if row.get('builtin_override') and updated != row.get('content'):
        raise ValueError('existing override requires review')
    return row, updated


def check_generated(runner=subprocess.run):
    """Run Xray's native config validation without logging private config."""
    result = runner(['docker', 'exec', CONTAINER, 'cat', '/opt/hiddify-manager/generated/xray.json'],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=True)
    if len(result.stdout) > 16 * 1024 * 1024:
        raise ValueError('generated configuration too large')
    config = json.loads(result.stdout)
    existing = config.get('geodata')
    if existing is not None and existing != {'cron': CRON}:
        raise ValueError('unexpected existing geodata configuration')
    config['geodata'] = {'cron': CRON}
    test = runner(['docker', 'exec', '-i', CONTAINER, '/opt/hiddify-manager/services/xray/bin/xray',
                   'run', '-test', '-c', 'stdin:'], input=json.dumps(config).encode(),
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=False)
    if test.returncode:
        raise ValueError('Xray rejected the candidate configuration')


def preview(panel):
    """Read and preview the candidate via the panel without changing it."""
    row, updated = select(panel.request('GET', 'proxy-base-configs/?side=server&core=xray'))
    result = panel.request('POST', 'proxy-base-configs/preview/',
                           {'side': 'server', 'core': 'xray', 'version': row['version'], 'content': updated})
    if not isinstance(result, dict) or result.get('ok') is not True:
        raise ValueError('panel rejected candidate base configuration')
    parsed = result.get('parsed')
    if not isinstance(parsed, dict) or parsed.get('geodata') != {'cron': CRON}:
        raise ValueError('panel preview omitted geodata schedule')
    check_generated()
    return row, updated
