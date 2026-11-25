import unittest
from datetime import datetime, timedelta, timezone

from fix_node_timestamps import OLD_IMPORT, OLD_ISO, transform


FIXTURE = OLD_IMPORT + "\n" + OLD_ISO + '''
def payload(last_seen, last_usage_report, child):
    return {
        "last_seen": last_seen.isoformat() if last_seen else None,
        "last_usage_report": last_usage_report(child.id),
    }
'''


class NodeTimestampTests(unittest.TestCase):
    def setUp(self):
        self.updated = transform(FIXTURE)
        self.namespace = {}
        exec(compile(self.updated, "nodes_api.py", "exec"), self.namespace)

    def test_naive_utc_time_is_explicit(self):
        self.assertEqual(self.namespace["_iso"](datetime(2026, 11, 25, 10, 20)), "2026-11-25T10:20:00Z")

    def test_aware_time_converted_to_utc(self):
        local = datetime(2026, 11, 25, 13, 20, tzinfo=timezone(timedelta(hours=3)))
        self.assertEqual(self.namespace["_iso"](local), "2026-11-25T10:20:00Z")
        self.assertIsNone(self.namespace["_iso"](None))
        self.assertIsNone(self.namespace["_iso"](datetime(2000, 1, 1)))

    def test_usage_report_preserves_fields_and_handles_invalid_values(self):
        normalize = self.namespace["_usage_report_utc"]
        self.assertEqual(normalize({"time": "2026-11-25T10:20:00", "count": 2}),
                         {"time": "2026-11-25T10:20:00Z", "count": 2})
        self.assertEqual(normalize({"time": "invalid"}), {"time": "invalid"})
        self.assertEqual(normalize({"count": 2}), {"count": 2})
        self.assertIsNone(normalize(None))

    def test_patch_is_idempotent(self):
        self.assertEqual(transform(self.updated), self.updated)

    def test_unexpected_or_partially_patched_source_is_rejected(self):
        with self.assertRaises(ValueError):
            transform(FIXTURE.replace("last_seen.isoformat()", "str(last_seen)"))
        with self.assertRaises(ValueError):
            transform(self.updated.replace("_iso(last_seen)", "str(last_seen)"))


if __name__ == "__main__":
    unittest.main()
