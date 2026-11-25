import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wait_for_panel import wait_for_panel


REVISION = "a" * 40


def run(status, conclusion=None, *, run_id=1, revision=REVISION, event="workflow_run"):
    return {"id": run_id, "head_sha": revision, "event": event,
            "status": status, "conclusion": conclusion}


class WaitForPanelTests(unittest.TestCase):
    def test_waits_until_matching_panel_deployment_succeeds(self):
        responses = iter([[], [run("in_progress")], [run("completed", "success")]])
        pauses = []
        wait_for_panel(REVISION, lambda: next(responses), sleep=pauses.append, attempts=3, interval=5)
        self.assertEqual(pauses, [5, 5])

    def test_rejects_failed_panel_deployment(self):
        with self.assertRaisesRegex(RuntimeError, "did not succeed"):
            wait_for_panel(REVISION, lambda: [run("completed", "failure")], attempts=1)

    def test_newer_run_takes_precedence_over_old_success(self):
        runs = [run("completed", "success", run_id=1), run("in_progress", run_id=2)]
        with self.assertRaisesRegex(RuntimeError, "deadline"):
            wait_for_panel(REVISION, lambda: runs, attempts=1)

    def test_ignores_other_revisions_and_manually_started_runs(self):
        runs = [run("completed", "success", revision="b" * 40),
                run("completed", "success", event="workflow_dispatch")]
        with self.assertRaisesRegex(RuntimeError, "deadline"):
            wait_for_panel(REVISION, lambda: runs, attempts=1)

    def test_retries_a_temporary_api_failure(self):
        responses = iter([ValueError("offline"), [run("completed", "success")]])

        def fetch():
            value = next(responses)
            if isinstance(value, Exception):
                raise value
            return value

        pauses = []
        wait_for_panel(REVISION, fetch, sleep=pauses.append, attempts=2, interval=1)
        self.assertEqual(pauses, [1])


if __name__ == "__main__":
    unittest.main()
