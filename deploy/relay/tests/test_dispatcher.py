import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dispatcher import REPOSITORY, parse_command, require_main, require_verified


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


if __name__ == '__main__':
    unittest.main()
