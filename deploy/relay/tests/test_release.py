import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from renderer import render
from release import Deployment, RollbackError
from test_renderer import fixture


SOURCE = Path(__file__).resolve().parents[1]
REVISION = 'a' * 40


class FakeRunner:
    def __init__(self, fail_second_up=False):
        self.commands = []
        self.fail_second_up = fail_second_up

    def run(self, command):
        self.commands.append(command)
        if self.fail_second_up and 'up' in command:
            if sum('up' in item for item in self.commands) == 2:
                raise RuntimeError('simulated rollback failure')


@unittest.skipUnless(hasattr(os, 'geteuid'), 'deployment runs on Linux')
class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / 'root'
        self.directory = self.root / 'subscription-relay-backup'
        self.ci = self.root / 'relay-ci'
        self.units = base / 'systemd'
        for directory in (self.directory, self.ci, self.units):
            directory.mkdir(parents=True)
        routes = self.directory / 'routes.json'
        routes.write_text(json.dumps(fixture()), encoding='utf-8')
        routes.chmod(0o600)
        self.paths = [self.directory / 'Caddyfile', self.directory / 'compose.yaml',
                      self.units / 'gamelife-subrelay-refresh.service',
                      self.units / 'gamelife-subrelay-refresh.timer', self.ci / 'dispatcher.py']
        self.original = {}
        for i, path in enumerate(self.paths):
            value = ('old fixture ' + str(i)).encode()
            path.write_bytes(value)
            path.chmod(0o644 if path.parent == self.units else 0o600)
            self.original[path] = value

    def tearDown(self):
        self.temp.cleanup()

    def deployer(self, runner, health):
        return Deployment(root=self.root, units=self.units, source=SOURCE,
                          runner=runner, health=health)

    def test_applies_verified_config_and_installs_dispatcher(self):
        runner = FakeRunner()
        self.assertTrue(self.deployer(runner, lambda _: None).apply(REVISION))
        self.assertEqual(self.paths[0].read_text(), render(fixture()))
        self.assertEqual(self.paths[1].read_bytes(), (SOURCE / 'compose.yaml').read_bytes())
        self.assertEqual(self.paths[4].read_bytes(), (SOURCE / 'dispatcher.py').read_bytes())
        self.assertEqual(stat.S_IMODE(self.paths[4].stat().st_mode), 0o700)
        self.assertEqual((self.ci / 'current-revision').read_text().strip(), REVISION)
        self.assertTrue(any('validate' in command for command in runner.commands))
        self.assertTrue(any('--force-recreate' in command for command in runner.commands))

    def test_failed_health_check_restores_previous_config(self):
        runner = FakeRunner()
        def fail(_):
            raise RuntimeError('simulated health failure')
        with self.assertRaises(RuntimeError):
            self.deployer(runner, fail).apply(REVISION)
        for path, expected in self.original.items():
            self.assertEqual(path.read_bytes(), expected)
        self.assertEqual(sum('up' in command for command in runner.commands), 2)
        self.assertFalse((self.ci / 'current-revision').exists())

    def test_failed_rollback_is_signalled(self):
        runner = FakeRunner(fail_second_up=True)
        def fail(_):
            raise RuntimeError('simulated health failure')
        with self.assertRaises(RollbackError):
            self.deployer(runner, fail).apply(REVISION)

    def test_unchanged_deploy_does_not_restart_relay(self):
        runner = FakeRunner()
        deployment = self.deployer(runner, lambda _: None)
        deployment.apply(REVISION)
        runner.commands.clear()
        self.assertFalse(deployment.apply(REVISION))
        self.assertFalse(any('up' in command for command in runner.commands))

    def test_private_route_state_must_not_be_world_readable(self):
        (self.directory / 'routes.json').chmod(0o644)
        with self.assertRaises(RuntimeError):
            self.deployer(FakeRunner(), lambda _: None).apply(REVISION)


if __name__ == '__main__':
    unittest.main()
