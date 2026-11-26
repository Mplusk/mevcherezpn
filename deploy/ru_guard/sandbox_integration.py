"""Run inside an isolated Hiddify container. No production panel is contacted."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from unittest.mock import patch

import apply_monitor
import node_operator as op
import outbound_agent
import outbound_install
import remote_agent
import test_template_install as fixtures
import test_outbound_install as outbound_fixtures
import verify_generated


class Panel(fixtures.Panel):
    def __init__(self):
        super().__init__(fixtures.fixture())
        self.busy = False
        self.fail_apply = False
        self.applied = False

    def request(self, method, suffix, data=None):
        if (method, suffix) == ('GET', 'apply/'):
            return {'running': ['apply'] if self.busy else []}
        if (method, suffix) == ('GET', 'outbounds/'):
            return {'outbounds': []}
        if (method, suffix) == ('POST', 'apply/apply/'):
            if self.fail_apply:
                # Simulate lost acknowledgement after the operation started.
                self.busy = True
                raise RuntimeError('lost acknowledgement')
            self.applied = True
            return {'action': 'apply', 'started': time.time()}
        if method == 'GET' and suffix.startswith('apply/log/?'):
            return {'started': True, 'finished': True}
        raise ValueError('unexpected mock API operation')


def exercise(fail_apply):
    panel = Panel()
    original = copy.deepcopy(panel.rows)
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / 'transactions'
        tx = ('b' if fail_apply else 'a') * 32
        with patch.object(op, 'panel_for', return_value=panel), \
             patch.object(op, 'locks_for', return_value=[]), \
             patch.object(op, 'require_mount'), \
             patch.object(op, 'verify_staged'), \
             patch.object(remote_agent, 'ROOT', root):
            panel.fail_apply = fail_apply
            if fail_apply:
                try:
                    op.execute('stage', 'de', tx)
                except apply_monitor.ApplyError:
                    pass
                else:
                    raise AssertionError('ambiguous apply was not rejected')
                journal = json.loads((root / tx / 'operator.json').read_text())
                assert journal['status'] == 'stage-intent'
                try:
                    op.execute('rollback', 'de', tx)
                except RuntimeError:
                    pass
                else:
                    raise AssertionError('rollback raced busy apply')
                panel.fail_apply = False
                panel.busy = False
            else:
                op.execute('stage', 'de', tx)
                assert json.loads((root / tx / 'operator.json').read_text())['status'] == 'stage-complete'
            backup = root / tx / 'templates.json'
            before = backup.read_bytes()
            rows = json.loads(before)
            assert len(rows) == 2
            assert [row['content'] for row in rows] == [row['content'] for row in original]
            assert backup.stat().st_mode & 0o777 == 0o600
            op.execute('rollback', 'de', tx)
            assert backup.read_bytes() == before
            assert panel.rows == original
            assert json.loads((root / tx / 'operator.json').read_text())['status'] == 'rolled-back'


def core_smoke():
    xray = '/opt/hiddify-manager/services/xray/bin/xray'
    sing = '/opt/hiddify-manager/services/hiddify-core/hiddify-core'
    with tempfile.TemporaryDirectory() as d:
        xp = Path(d) / 'xray.json'
        xp.write_text(json.dumps({'log': {'loglevel': 'none'},
                                  'inbounds': [{'listen': '127.0.0.1', 'port': 48231, 'protocol': 'socks'}],
                                  'outbounds': [{'protocol': 'freedom', 'tag': 'freedom'}]}))
        subprocess.run([xray, 'run', '-test', '-c', str(xp)], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=20, check=True)
        sp = Path(d) / 'sing.json'
        sp.write_text(json.dumps({'log': {'disabled': True},
                                  'inbounds': [{'type': 'socks', 'listen': '127.0.0.1', 'listen_port': 48232}],
                                  'outbounds': [{'type': 'direct', 'tag': 'freedom'}],
                                  'route': {'final': 'freedom'}}))
        process = subprocess.Popen([sing, 'srun', '-c', str(sp)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(2)
            if process.poll() is not None:
                raise RuntimeError('Hiddify core failed sandbox startup')
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)


class OutboundPanel(outbound_fixtures.Panel):
    def __init__(self):
        super().__init__()
        self.busy = False
        self.fail_apply = False

    def request(self, method, path, data=None):
        if (method, path) == ('GET', 'apply/'):
            return {'running': ['apply'] if self.busy else []}
        if (method, path) == ('POST', 'apply/apply/'):
            if self.fail_apply:
                self.busy = True
                raise RuntimeError('lost acknowledgement')
            return {'action': 'apply', 'started': time.time()}
        if method == 'GET' and path.startswith('apply/log/?'):
            return {'started': True, 'finished': True}
        return super().request(method, path, data)


def exercise_outbound(fail_apply, existing=False):
    panel = OutboundPanel()
    if existing:
        item = {**copy.deepcopy(outbound_install.PAYLOAD), 'id': 4, 'enabled': False,
                'is_builtin': False, 'is_default': False}
        panel.rows.insert(2, item)
        panel.original = copy.deepcopy(panel.rows)
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / 'transactions'
        root.mkdir(mode=0o700)
        tx = ('d' if fail_apply else 'c') * 32
        directory = root / tx
        directory.mkdir(mode=0o700)
        op.journal_write(directory, 'stage-complete')
        with patch.object(op, 'panel_for', return_value=panel), \
             patch.object(op, 'locks_for', return_value=[]), \
             patch.object(op, 'require_mount'), patch.object(op, 'verify_staged'), \
             patch.object(op, 'verify_services'), \
             patch.object(verify_generated, 'read_config', return_value={}), \
             patch.object(verify_generated, 'verify'), \
             patch.object(remote_agent, 'ROOT', root):
            panel.fail_apply = fail_apply
            if fail_apply:
                try:
                    outbound_agent.execute('activate', 'main', tx)
                except apply_monitor.ApplyError:
                    pass
                else:
                    raise AssertionError('activation ambiguity was not rejected')
                assert outbound_agent.read_state(directory) == 'activate-intent'
                try:
                    outbound_agent.execute('disable', 'main', tx)
                except RuntimeError:
                    pass
                else:
                    raise AssertionError('deactivation raced busy apply')
                panel.fail_apply = False
                panel.busy = False
            else:
                outbound_agent.execute('activate', 'main', tx)
                outbound_agent.execute('verify-active', 'main', tx)
                assert outbound_agent.read_state(directory) == 'active'
            before = (directory / 'outbounds-before.json').read_bytes()
            assert json.loads(before) == panel.original
            assert (directory / 'outbounds-before.json').stat().st_mode & 0o777 == 0o600
            outbound_agent.execute('disable', 'main', tx)
            assert (directory / 'outbounds-before.json').read_bytes() == before
            assert outbound_agent.read_state(directory) == 'disabled'
            assert next(row for row in panel.rows if row['name'] == 'GameLife blocked sites')['enabled'] is False
            if existing:
                assert panel.rows == panel.original


def main():
    exercise(False)
    exercise(True)
    exercise_outbound(False)
    exercise_outbound(True)
    exercise_outbound(False, existing=True)
    core_smoke()
    print('ISOLATED_STAGE_ROLLBACK_AND_CORES_OK')
