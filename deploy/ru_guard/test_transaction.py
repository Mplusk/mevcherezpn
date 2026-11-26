import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import json

from outbound_install import marked
from test_outbound_install import Panel as OutboundPanel
from test_template_install import Panel as TemplatePanel, fixture as template_fixture
from transaction import RollbackIncomplete, TransactionError, run
from journal import Journal, JournalError

NODES = ('main', 'nl', 'de', 'pl')


class Panel:
    def __init__(self):
        self.outbounds = OutboundPanel()
        self.templates = TemplatePanel(template_fixture())

    def request(self, *args):
        return self.outbounds.request(*args)

    def get_templates(self):
        return self.templates.get_templates()

    def patch_template(self, *args):
        return self.templates.patch_template(*args)


class Driver:
    def __init__(self, panel):
        self.events = []
        self.failed = None
        self.apply_count = 0
        self.parent_node = 'main'
        self.panels = {node: (panel if node == 'main' else TemplatePanel(template_fixture()))
                       for node in NODES}

    def node_panels(self):
        return self.panels

    def facts(self, name, keys):
        self.events.append(name)
        result = {node: {key: True for key in keys} for node in NODES}
        if self.failed == name:
            result['nl'][keys[0]] = False
        return result

    def preflight(self):
        return self.facts('preflight', ('host_verified', 'version_pinned', 'baseline_healthy'))

    def prepare_mounts(self):
        self.events.append('prepare_mounts')
        if self.failed == 'prepare_mounts':
            raise RuntimeError('partial preparation')

    def verify_mounts(self):
        return self.facts('verify_mounts', ('host_verified', 'version_pinned', 'mount_persistent',
                                             'assets_valid', 'xray_valid', 'singbox_valid'))

    def apply(self):
        self.apply_count += 1
        name = ('stage_apply' if self.apply_count == 1 else
                'final_apply' if self.apply_count == 2 else 'rollback_apply')
        return self.facts(name, ('applied',))

    def verify_sync(self):
        return self.facts('verify_sync', ('guard_active', 'exception_active'))

    def verify_new(self):
        return self.facts('verify_new', ('vpn_healthy', 'blocked_via_vpn', 'ru_guarded',
                                          'private_guarded', 'torrent_guarded'))

    def verify_subscription(self):
        self.events.append('verify_subscription')
        return self.failed != 'verify_subscription'

    def verify_baseline(self):
        return self.facts('verify_baseline', ('baseline_restored', 'vpn_healthy'))

    def rollback_mounts(self):
        self.events.append('rollback_mounts')


class TransactionTests(unittest.TestCase):
    def test_success_requires_every_verification(self):
        panel = Panel()
        driver = Driver(panel)
        with tempfile.TemporaryDirectory() as d:
            folder = run(panel, driver, NODES, d)
            self.assertTrue(all((folder / f'templates-{index}.json').is_file() for index in range(4)))
            self.assertTrue((folder / 'outbound.json').is_file())
            self.assertTrue(marked(panel.outbounds.rows)['enabled'])
            self.assertNotIn('rollback_mounts', driver.events)
            second_panel = Panel()
            self.assertTrue(run(second_panel, Driver(second_panel), NODES, d).is_dir())

    def test_preflight_failure_has_no_writes(self):
        panel = Panel()
        driver = Driver(panel)
        driver.failed = 'preflight'
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(TransactionError):
                run(panel, driver, NODES, d)
        self.assertNotIn('prepare_mounts', driver.events)
        self.assertEqual(panel.outbounds.mutations, [])
        self.assertEqual(panel.templates.calls, [])

    def test_child_templates_are_staged_before_parent_exception(self):
        panel = Panel()
        driver = Driver(panel)
        original_request = panel.request
        def guarded_request(method, path, data=None):
            if method == 'POST' and path == 'outbounds/':
                self.assertIn('stage_apply', driver.events)
                for child_panel in driver.panels.values():
                    self.assertTrue(all(row['builtin_override'] for row in child_panel.get_templates()))
            return original_request(method, path, data)
        panel.request = guarded_request
        with tempfile.TemporaryDirectory() as d:
            run(panel, driver, NODES, d)

    def test_child_template_failure_prevents_parent_exception(self):
        panel = Panel()
        driver = Driver(panel)
        driver.panels['de'].fail_id = 2
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(TransactionError):
                run(panel, driver, NODES, d)
        self.assertFalse(any(method == 'POST' for method, _ in panel.outbounds.mutations))
        self.assertEqual(panel.templates.rows, panel.templates.originals)
        for node in ('nl', 'de', 'pl'):
            self.assertEqual(driver.panels[node].rows, driver.panels[node].originals)

    def test_partial_mount_failure_unmounts_only_after_baseline(self):
        panel = Panel()
        driver = Driver(panel)
        driver.failed = 'prepare_mounts'
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(TransactionError):
                run(panel, driver, NODES, d)
        self.assertLess(driver.events.index('verify_baseline'), driver.events.index('rollback_mounts'))
        self.assertEqual(panel.templates.calls, [])

    def test_verification_failure_restores_before_unmount(self):
        panel = Panel()
        driver = Driver(panel)
        driver.failed = 'verify_new'
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(TransactionError):
                run(panel, driver, NODES, d)
        self.assertFalse(marked(panel.outbounds.rows)['enabled'])
        self.assertEqual(panel.templates.rows, panel.templates.originals)
        rollback_index = driver.events.index('rollback_apply')
        baseline_index = driver.events.index('verify_baseline', rollback_index)
        self.assertLess(rollback_index, baseline_index)
        self.assertLess(baseline_index, driver.events.index('rollback_mounts'))

    def test_rollback_apply_failure_keeps_mounts(self):
        panel = Panel()
        driver = Driver(panel)
        original_verify = driver.verify_new
        def fail_then_break_rollback():
            result = original_verify()
            driver.failed = 'rollback_apply'
            result['pl']['vpn_healthy'] = False
            return result
        driver.verify_new = fail_then_break_rollback
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RollbackIncomplete):
                run(panel, driver, NODES, d)
        self.assertNotIn('rollback_mounts', driver.events)

    def test_missing_node_is_rejected(self):
        panel = Panel()
        driver = Driver(panel)
        driver.preflight = lambda: {n: {'host_verified': True, 'version_pinned': True,
                                         'baseline_healthy': True} for n in NODES[:3]}
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(TransactionError):
                run(panel, driver, NODES, d)
        self.assertNotIn('prepare_mounts', driver.events)

    def test_process_interruption_is_detected_before_new_writes(self):
        panel = Panel()
        driver = Driver(panel)
        def crash():
            driver.events.append('prepare_mounts')
            raise SystemExit('simulated interruption')
        driver.prepare_mounts = crash
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SystemExit):
                run(panel, driver, NODES, d)
            journals = list(Path(d).glob('route-*/journal.json'))
            self.assertEqual(len(journals), 1)
            record = json.loads(journals[0].read_text())
            self.assertEqual(record['status'], 'running')
            self.assertEqual(record['nodes'], list(NODES))
            self.assertEqual(record['events'][-1], {'phase': 'prepare-mounts', 'event': 'intent'})
            self.assertNotIn('rollback_mounts', driver.events)
            other_panel = Panel()
            other_driver = Driver(other_panel)
            with self.assertRaises(RollbackIncomplete):
                run(other_panel, other_driver, NODES, d)
            self.assertEqual(other_driver.events, [])

    def test_journal_failure_after_mount_never_removes_mount(self):
        panel = Panel()
        driver = Driver(panel)
        real_write = Journal.write
        calls = 0
        def fail_after_operation(journal):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise JournalError('simulated disk failure')
            return real_write(journal)
        with tempfile.TemporaryDirectory() as d, patch.object(Journal, 'write', fail_after_operation):
            with self.assertRaises(RollbackIncomplete):
                run(panel, driver, NODES, d)
            self.assertIn('prepare_mounts', driver.events)
            self.assertNotIn('rollback_mounts', driver.events)

    def test_broken_or_missing_journal_fails_closed(self):
        for contents in (None, '{bad', '{"version":1,"status":"running"}',
                         '{"version":1,"status":"complete","events":[]}',
                         '{"version":1,"status":"rolled_back","events":[]}'):
            panel = Panel()
            driver = Driver(panel)
            with self.subTest(contents=contents), tempfile.TemporaryDirectory() as d:
                unfinished = Path(d) / 'route-abandoned'
                unfinished.mkdir()
                if contents is not None:
                    (unfinished / 'journal.json').write_text(contents)
                with self.assertRaises(RollbackIncomplete):
                    run(panel, driver, NODES, d)
                self.assertEqual(driver.events, [])


if __name__ == '__main__':
    unittest.main()
