import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reduce import RULES, publish, reduce_dat


def v(n):
    result = bytearray()
    while n > 127:
        result.append((n & 127) | 128)
        n >>= 7
    result.append(n)
    return bytes(result)


def entry(code, payload=b"\x0a\x04test"):
    code = code.encode("ascii")
    value = b"\x0a" + v(len(code)) + code + b"\x12" + v(len(payload)) + payload
    return b"\x0a" + v(len(value)) + value


class ReduceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "upstream"
        self.source.mkdir()
        self.dest = self.root / "published"
        for name, wanted in RULES.items():
            (self.source / name).write_bytes(b"".join(entry(code) for code in sorted(wanted)) + entry("UNUSED"))

    def tearDown(self):
        self.temp.cleanup()

    def test_selected_entries_retained_verbatim_and_pair_published(self):
        revision = publish(self.source, self.dest)
        manifest = json.loads((self.dest / "current.json").read_text())
        self.assertEqual(manifest["revision"], revision)
        for name, wanted in RULES.items():
            result = (self.dest / "revisions" / revision / name).read_bytes()
            self.assertEqual(result, b"".join(entry(code) for code in sorted(wanted)))
            self.assertNotIn(b"UNUSED", result)
        self.assertEqual(publish(self.source, self.dest), revision)

    def test_missing_and_duplicate_categories_rejected(self):
        for data in (entry("A"), entry("A") + entry("A") + entry("B")):
            with self.subTest(data=data), self.assertRaises(ValueError):
                reduce_dat(data, frozenset(("A", "B")))

    def test_corrupt_data_rejected(self):
        for data in (b"", b"\x0a\x80", b"\x0a\x05\x0a\x10A", b"\x0a\x03\x0a\x01A\x00", entry("A")[:-1],
                     b"\x0a\x08\x0a\x01A\x12\x03\x0a\x05X"):
            with self.subTest(data=data), self.assertRaises(ValueError):
                reduce_dat(data, frozenset(("A",)))

    def test_input_failure_keeps_prior_pair(self):
        revision = publish(self.source, self.dest)
        manifest = (self.dest / "current.json").read_bytes()
        before = {name: (self.dest / "revisions" / revision / name).read_bytes() for name in RULES}
        (self.source / "geosite.dat").write_bytes(b"broken")
        with self.assertRaises(ValueError):
            publish(self.source, self.dest)
        self.assertEqual((self.dest / "current.json").read_bytes(), manifest)
        for name in RULES:
            self.assertEqual((self.dest / "revisions" / revision / name).read_bytes(), before[name])

    def test_manifest_failure_keeps_prior_pair(self):
        publish(self.source, self.dest)
        manifest = (self.dest / "current.json").read_bytes()
        (self.source / "geoip.dat").write_bytes(b"".join(entry(code, b"\x0a\x03new") for code in sorted(RULES["geoip.dat"])))
        with patch("reduce.os.replace", side_effect=OSError("simulated publication failure")):
            with self.assertRaises(OSError):
                publish(self.source, self.dest)
        self.assertEqual((self.dest / "current.json").read_bytes(), manifest)


if __name__ == "__main__":
    unittest.main()
