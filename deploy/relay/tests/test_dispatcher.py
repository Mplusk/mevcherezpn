import contextlib
import io
import os
import stat
import sys
import tempfile
from types import SimpleNamespace
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dispatcher
from dispatcher import REPOSITORY, RU_ASSET_FILES, extract_ru_assets, parse_command, require_main, require_verified


REVISION = 'a' * 40


class DispatcherTests(unittest.TestCase):
    def test_only_fixed_commands_are_accepted(self):
        self.assertEqual(parse_command('relay-probe'), ('probe', None))
        self.assertEqual(parse_command('relay-deploy ' + REVISION), ('deploy', REVISION))
        for command in ('', 'id', 'relay-probe; id', 'relay-deploy ' + REVISION + ';id',
                        'relay-deploy ' + 'A' * 40, 'relay-deploy ' + 'a' * 41,
                        'relay-deploy  ' + REVISION):
            with self.subTest(command=command), self.assertRaises(ValueError):
                parse_command(command)

    def test_ref_must_be_current_main(self):
        require_main(REVISION, lambda _: {'object': {'sha': REVISION}})
        with self.assertRaises(RuntimeError):
            require_main(REVISION, lambda _: {'object': {'sha': 'b' * 40}})

    def test_ci_must_be_successful_push_for_exact_repo_and_revision(self):
        good = {'head_sha': REVISION, 'head_branch': 'main', 'event': 'push',
                'conclusion': 'success', 'head_repository': {'full_name': REPOSITORY}}
        require_verified(REVISION, lambda _: {'workflow_runs': [good]})
        for field, value in [('head_sha', 'b' * 40), ('head_branch', 'other'),
                             ('event', 'pull_request'), ('conclusion', 'failure'),
                             ('head_repository', {'full_name': 'other/repo'})]:
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                require_verified(REVISION, lambda _, f=field, v=value:
                                 {'workflow_runs': [{**good, f: v}]})
        with self.assertRaises(RuntimeError):
            require_verified(REVISION, lambda _: {'workflow_runs': []})

    def test_extracts_only_fixed_sources_at_verified_revision(self):
        self.assertEqual(RU_ASSET_FILES, ('installer.py', 'reduce.py', 'sync.py',
                                          'compose.ru-assets.yaml', 'compose_plan.py', 'relay_hook.py',
                                          'gamelife-ru-assets.service', 'gamelife-ru-assets.timer'))
        calls = []
        def get(arguments, *, env, output):
            calls.append((arguments, env, output))
            return arguments[-1].encode('ascii')
        with tempfile.TemporaryDirectory() as temp:
            env = {'PATH': '/test'}
            extract_ru_assets(REVISION, temp, env, get)
            folder = Path(temp) / 'ru_assets'
            self.assertEqual({p.name for p in folder.iterdir()}, set(RU_ASSET_FILES))
            if os.name != 'nt':
                self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
            for name, (arguments, passed_env, output) in zip(RU_ASSET_FILES, calls):
                self.assertEqual(arguments, ['git', '-C', temp, 'show', REVISION + ':deploy/ru_assets/' + name])
                self.assertEqual(passed_env, env)
                self.assertTrue(output)
                self.assertEqual((folder / name).read_bytes(), arguments[-1].encode('ascii'))
                if os.name != 'nt':
                    self.assertEqual(stat.S_IMODE((folder / name).stat().st_mode), 0o600)

    def test_rejects_untrusted_revision_and_existing_asset_directory(self):
        for revision in ('../bad', 'a' * 40 + ':../../x', 'A' * 40, ''):
            with tempfile.TemporaryDirectory() as temp, self.subTest(revision=revision):
                with self.assertRaises(ValueError):
                    extract_ru_assets(revision, temp, {}, lambda *args, **kwargs: self.fail('unexpected fetch'))
                self.assertEqual(list(Path(temp).iterdir()), [])
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / 'ru_assets').mkdir()
            with self.assertRaises(FileExistsError):
                extract_ru_assets(REVISION, temp, {}, lambda *args, **kwargs: self.fail('unexpected fetch'))

    def test_deploy_stages_sources_but_executes_only_relay_release(self):
        calls = []
        def fake_run(arguments, **kwargs):
            calls.append(arguments)
            if 'rev-parse' in arguments:
                return (REVISION + '\n').encode('ascii')
            if 'show' in arguments:
                return b'staged public source\n'
            return None
        def fake_subprocess(arguments, **kwargs):
            staged = Path(kwargs['cwd']) / 'ru_assets'
            self.assertEqual({p.name for p in staged.iterdir()}, set(RU_ASSET_FILES))
            calls.append(arguments)
            return SimpleNamespace(returncode=0, stdout=b'RELAY_STATUS unchanged\n')
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(dispatcher, 'ROOT', Path(temp)), patch.object(dispatcher, 'run', fake_run), \
                 patch.object(dispatcher, 'require_main'), patch.object(dispatcher, 'require_verified'), \
                 patch.object(dispatcher.subprocess, 'run', fake_subprocess), contextlib.redirect_stdout(io.StringIO()):
                dispatcher.deploy(REVISION)
            launches = [args for args in calls if args and args[0] == '/usr/bin/python3']
            self.assertEqual(len(launches), 1)
            self.assertEqual(Path(launches[0][1]).name, 'release.py')
            self.assertEqual(launches[0][2:], ['apply', REVISION])
            self.assertEqual(list(Path(temp).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
