import copy
import unittest

from relay_hook import (HOOK_GENERATION, REQUIRED, SOURCE, TARGET, HookError,
                        RollbackUnsafe, run)

REVISION = 'a' * 40
MOUNT = {'Destination': TARGET, 'Source': SOURCE, 'Type': 'bind', 'RW': False}


class Adapter:
    def __init__(self, mounted=False):
        self.dispatcher_generation = HOOK_GENERATION
        self.snapshot = {'mounts': [copy.deepcopy(MOUNT)] if mounted else []}
        self.events = []
        self.dependent = False
        self.existing_ok = True
        self.install_ok = True
        self.restore_ok = True
        self.fail_after_change = False
        self.missing_proof = None

    def inspect(self):
        self.events.append('inspect')
        return copy.deepcopy(self.snapshot)

    def verify_existing(self, before, revision):
        self.events.append('verify_existing')
        return self.existing_ok

    def preflight(self, revision, before):
        self.events.append('preflight')
        result = {'revision': revision, **{key: True for key in REQUIRED}}
        if self.missing_proof:
            result[self.missing_proof] = False
        return result

    def templates_depend_on_mount(self):
        self.events.append('template_check')
        return self.dependent

    def install(self, before, revision):
        self.events.append('install')
        self.snapshot['mounts'].append(copy.deepcopy(MOUNT))
        if self.fail_after_change:
            raise OSError('response lost')

    def verify_installed(self, before, after, revision):
        self.events.append('verify_installed')
        return self.install_ok

    def rollback(self, before, revision):
        self.events.append('rollback')
        self.snapshot = copy.deepcopy(before)

    def verify_restored(self, before, after, revision):
        self.events.append('verify_restored')
        return self.restore_ok and before == after


class RelayHookTests(unittest.TestCase):
    def test_existing_mount_is_never_recreated_or_removed(self):
        adapter = Adapter(mounted=True)
        self.assertEqual(run(adapter, REVISION, staging=True), 'unchanged')
        self.assertEqual(adapter.events, ['inspect', 'verify_existing'])

    def test_no_staging_is_read_only(self):
        adapter = Adapter()
        self.assertEqual(run(adapter, REVISION), 'absent')
        self.assertEqual(adapter.events, ['inspect'])

    def test_two_phase_requires_current_process_new_generation(self):
        adapter = Adapter()
        adapter.dispatcher_generation = 0
        with self.assertRaises(HookError):
            run(adapter, REVISION, staging=True)
        self.assertNotIn('install', adapter.events)

    def test_each_missing_preflight_blocks_recreate(self):
        for key in REQUIRED:
            with self.subTest(key=key):
                adapter = Adapter()
                adapter.missing_proof = key
                with self.assertRaises(HookError):
                    run(adapter, REVISION, staging=True)
                self.assertNotIn('install', adapter.events)

    def test_staging_runs_once_and_subsequent_run_is_read_only(self):
        adapter = Adapter()
        self.assertEqual(run(adapter, REVISION, staging=True), 'installed')
        count = adapter.events.count('install')
        self.assertEqual(run(adapter, REVISION, staging=True), 'unchanged')
        self.assertEqual(adapter.events.count('install'), count)

    def test_failed_install_rolls_back_only_when_no_template_depends(self):
        adapter = Adapter()
        adapter.fail_after_change = True
        with self.assertRaises(HookError):
            run(adapter, REVISION, staging=True)
        self.assertIn('rollback', adapter.events)
        self.assertEqual(adapter.snapshot['mounts'], [])

    def test_dependency_appearing_during_failure_keeps_mount(self):
        adapter = Adapter()
        def fail_with_dependency(before, revision):
            adapter.snapshot['mounts'].append(copy.deepcopy(MOUNT))
            adapter.dependent = True
            raise OSError('ambiguous state')
        adapter.install = fail_with_dependency
        with self.assertRaises(RollbackUnsafe):
            run(adapter, REVISION, staging=True)
        self.assertNotIn('rollback', adapter.events)
        self.assertEqual(adapter.snapshot['mounts'], [MOUNT])

    def test_unexpected_mount_or_unknown_dependency_never_recreates(self):
        adapter = Adapter()
        adapter.snapshot['mounts'] = [{**MOUNT, 'RW': True}]
        with self.assertRaises(HookError):
            run(adapter, REVISION, staging=True)
        self.assertNotIn('install', adapter.events)
        adapter = Adapter()
        adapter.snapshot['mounts'] = [None]
        with self.assertRaises(HookError):
            run(adapter, REVISION, staging=True)
        self.assertNotIn('install', adapter.events)
        adapter = Adapter()
        adapter.dependent = None
        with self.assertRaises(HookError):
            run(adapter, REVISION, staging=True)
        self.assertNotIn('install', adapter.events)

    def test_unverified_restoration_is_not_success(self):
        adapter = Adapter()
        adapter.fail_after_change = True
        adapter.restore_ok = False
        with self.assertRaises(RollbackUnsafe):
            run(adapter, REVISION, staging=True)


if __name__ == '__main__':
    unittest.main()
