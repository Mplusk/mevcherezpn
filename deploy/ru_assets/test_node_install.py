import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

try:
    import node_install as node
except ImportError:
    node = None


@unittest.skipUnless(node is not None and os.name == 'posix', 'Linux only')
class NodeInstallTests(unittest.TestCase):
    def setUp(self):
        self.profile = {'protocol': 'vless',
                        'settings': {'vnext': [{'address': 'de.gamelife.shop',
                                                'users': [{'id': 'synthetic-test-secret'}]}]},
                        'streamSettings': {'network': 'ws', 'security': 'tls'}}
        self.request = {'action': 'stage', 'node': 'de', 'outbound': self.profile}

    def test_request_is_strict_and_node_specific(self):
        self.assertEqual(node.validate_request(self.request)[:2], ('stage', 'de'))
        for mutate in ({'node': 'nl'}, {'node': 'main'}, {'action': 'rollback'},
                       {'unknown': 'x'}, {'transaction': 'a' * 32}):
            value = dict(self.request, **mutate)
            with self.subTest(value=mutate), self.assertRaises(ValueError):
                node.validate_request(value)
        wrong = copy.deepcopy(self.request)
        wrong['outbound']['streamSettings']['security'] = 'none'
        with self.assertRaises(ValueError):
            node.validate_request(wrong)
        rollback = dict(self.request, action='rollback', transaction='a' * 32)
        self.assertEqual(node.validate_request(rollback)[0], 'rollback')
        with self.assertRaises(ValueError):
            node.validate_request(dict(rollback, transaction='../bad'))

    def test_monitor_profile_is_stdin_only_and_process_is_terminated(self):
        class Pipe(io.BytesIO):
            def close(self):
                self.captured = self.getvalue()
        class Proc:
            def __init__(self):
                self.stdin = Pipe()
                self.terminated = False
            def poll(self):
                return None
            def terminate(self):
                self.terminated = True
            def wait(self, timeout):
                return 0
        process = Proc()
        with patch.object(node.subprocess, 'Popen', return_value=process) as popen, \
             patch.object(node.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'2.27.75.91')), \
             patch.object(node.time, 'sleep'):
            self.assertTrue(node.probe('/synthetic/xray', self.profile, '2.27.75.91'))
        args, kwargs = popen.call_args
        self.assertEqual(args[0], ['/synthetic/xray', 'run', '-c', 'stdin:'])
        self.assertNotIn('synthetic-test-secret', repr(args))
        self.assertNotIn('synthetic-test-secret', repr(kwargs))
        self.assertIn(b'synthetic-test-secret', process.stdin.captured)
        self.assertTrue(process.terminated)

    def test_compose_path_is_pinned_and_overlay_is_exact(self):
        release = '/root/services/gamelife-vpn/releases/' + 'a' * 40 + '/compose.panel.yaml'
        with patch.object(Path, 'is_symlink', return_value=False), \
             patch.object(Path, 'is_file', return_value=True), \
             patch.object(Path, 'resolve', autospec=True, side_effect=lambda p: p):
            self.assertEqual(str(node.compose_from_snapshot({'config_files': release})), release)
            self.assertEqual(str(node.compose_from_snapshot({'config_files': release + ',' + str(node.OVERLAY)})), release)
            for value in (release + ',/tmp/untrusted', '/tmp/compose.yaml', release + ',x,y'):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    node.compose_from_snapshot({'config_files': value})

    def test_rollback_refuses_when_templates_depend_on_mount(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tx = 'a' * 32
            txdir = root / tx
            txdir.mkdir()
            data = b'safe compose'
            (txdir / 'compose.before').write_bytes(data)
            (txdir / 'compose.before').chmod(0o600)
            release = '/root/services/gamelife-vpn/releases/' + 'a' * 40 + '/compose.panel.yaml'
            journal = {'version': 1, 'status': 'committed', 'node': 'de', 'compose': release,
                       'sha256': hashlib.sha256(data).hexdigest()}
            (txdir / 'journal.json').write_text(json.dumps(journal))
            (txdir / 'journal.json').chmod(0o600)
            with patch.object(node, 'TXROOT', root), patch.object(node, 'template_dependency', return_value=True), \
                 patch.object(node, 'compose_up') as up, patch.object(node, 'root_file'):
                with self.assertRaisesRegex(RuntimeError, 'retain'):
                    node.apply_rollback(tx, 'de', lambda: None)
                up.assert_not_called()

    def test_subprocess_environment_drops_proxy_and_monitor_values(self):
        result = subprocess.CompletedProcess([], 0)
        with patch.dict(os.environ, {'MONITOR_PASSWORD': 'sensitive', 'HTTP_PROXY': 'http://unsafe'}, clear=False), \
             patch.object(node.subprocess, 'run', return_value=result) as run:
            node.run_silent(['docker', 'version'], 5)
        env = run.call_args.kwargs['env']
        self.assertNotIn('MONITOR_PASSWORD', env)
        self.assertNotIn('HTTP_PROXY', env)
        self.assertEqual(env['VPN_RU_ASSET_ROOT'], str(node.ASSETS))

    def test_marker_is_private_atomic_idempotent_and_fails_on_drift(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'compose-enabled'
            with patch.object(node, 'MARKER', marker):
                self.assertFalse(node.marker_exists())
                node.enable_marker()
                node.enable_marker()
                self.assertEqual(marker.read_bytes(), b'v1\n')
                self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
                marker.write_bytes(b'wrong')
                with self.assertRaises(ValueError):
                    node.marker_exists()
                marker.unlink()
                marker.symlink_to('/etc/passwd')
                with self.assertRaises(ValueError):
                    node.marker_exists()

    def test_installed_monitor_profiles_match_node_validation(self):
        credentials = Path('/etc/gamelife-vpn-monitor/credentials.json')
        if not credentials.is_file():
            self.skipTest('monitor credentials not installed on this host')
        node.root_file(credentials)
        profiles = json.loads(json.loads(credentials.read_text())['MONITOR_PROFILE_JSON'])
        self.assertEqual(set(profiles), set(node.NODES))
        for name in node.NODES:
            node.validate_request({'action': 'stage', 'node': name, 'outbound': profiles[name]})

    def test_stage_failure_attempts_rollback_and_keeps_original_release(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            compose = root / 'compose.panel.yaml'
            compose.write_bytes(b'original immutable release')
            state = root / 'state.json'
            state.write_text(json.dumps({'node_domain': 'de.gamelife.shop'}))
            txroot = root / 'transactions'
            snapshot = {'config_files': str(compose)}
            def fail_stage(path, staged):
                self.assertTrue(staged)
                raise RuntimeError('simulated interrupted stage')
            with patch.object(node, 'STATE', state), patch.object(node, 'TXROOT', txroot), \
                 patch.object(node, 'root_file'), patch.object(node, 'locks', return_value=[]), \
                 patch.object(node, 'run_silent'), patch.object(Path, 'chmod'), \
                 patch.object(node.installer, 'inspect_container', return_value=snapshot), \
                 patch.object(node, 'compose_from_snapshot', return_value=compose), \
                 patch.object(node, 'verify_assets'), patch.object(node, 'compare_runtime'), \
                 patch.object(node, 'template_dependency', return_value=False), \
                 patch.object(node, 'healthy'), patch.object(node, 'prepare_overlay'), \
                 patch.object(node, 'marker_exists', return_value=False), patch.object(node, 'enable_marker'), \
                 patch.object(node, 'compose_up', side_effect=fail_stage), \
                 patch.object(node, 'apply_rollback') as rollback:
                with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                    node.run(self.request)
                rollback.assert_called_once()
            self.assertEqual(compose.read_bytes(), b'original immutable release')
            txdir = next(txroot.iterdir())
            self.assertEqual((txdir / 'compose.before').read_bytes(), compose.read_bytes())
            journal = (txdir / 'journal.json').read_text()
            self.assertNotIn('synthetic-test-secret', journal)
            self.assertEqual(json.loads(journal)['status'], 'pending')

    def test_stage_commits_journal_without_changing_release_or_recording_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            compose = root / 'compose.panel.yaml'
            compose.write_bytes(b'original immutable release')
            state = root / 'state.json'
            state.write_text(json.dumps({'node_domain': 'de.gamelife.shop'}))
            txroot = root / 'transactions'
            snapshot = {'config_files': str(compose)}
            with patch.object(node, 'STATE', state), patch.object(node, 'TXROOT', txroot), \
                 patch.object(node, 'root_file'), patch.object(node, 'locks', return_value=[]), \
                 patch.object(node, 'run_silent'), patch.object(Path, 'chmod'), \
                 patch.object(node.installer, 'inspect_container', return_value=snapshot), \
                 patch.object(node, 'compose_from_snapshot', return_value=compose), \
                 patch.object(node, 'verify_assets'), patch.object(node, 'compare_runtime'), \
                 patch.object(node, 'template_dependency', return_value=False), \
                 patch.object(node, 'healthy') as healthy, patch.object(node, 'prepare_overlay'), \
                 patch.object(node, 'marker_exists', return_value=False), patch.object(node, 'enable_marker') as marker, \
                 patch.object(node, 'compose_up') as up:
                result = node.run(self.request)
            self.assertEqual(result['status'], 'staged')
            up.assert_called_once_with(compose, True)
            self.assertEqual(healthy.call_count, 2)
            marker.assert_called_once()
            txdir = txroot / result['transaction']
            self.assertEqual((txdir / 'compose.before').read_bytes(), compose.read_bytes())
            journal = (txdir / 'journal.json').read_text()
            self.assertNotIn('synthetic-test-secret', journal)
            self.assertEqual(json.loads(journal)['status'], 'committed')

    @unittest.skipUnless(os.environ.get('RUN_NODE_PREFLIGHT') == '1', 'explicit live preflight only')
    def test_live_node_baseline_without_mutation(self):
        try:
            node.root_file(node.STATE)
            node.root_file(node.ENVFILE)
            snapshot = node.installer.inspect_container()
            compose = node.compose_from_snapshot(snapshot)
            node.verify_assets()
            node.compare_runtime(compose, snapshot, False)
            self.assertFalse(node.template_dependency())
        except Exception:
            self.fail('live baseline did not match required safe configuration')


if __name__ == '__main__':
    unittest.main()
