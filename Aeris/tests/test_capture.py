"""Capture writes into Aviral's vault, so the things worth pinning down are
that it lands in the shape the indexer expects, that a day's log is never
overwritten by the next line, and that it does not get to skip the gate.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import actions, capture, data  # noqa: E402


class CaptureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aeris-capture-")
        os.environ["AERIS_WRITE_ROOTS"] = self.tmp
        os.environ["AERIS_CAPTURE_DIR"] = self.tmp
        data.reload_env()
        actions.AUDIT_DIR = Path(self.tmp) / "audit"
        actions.AUDIT_LOG = actions.AUDIT_DIR / "actions.jsonl"
        actions._pending.clear()

    def tearDown(self):
        for key in ("AERIS_WRITE_ROOTS", "AERIS_CAPTURE_DIR"):
            os.environ.pop(key, None)
        data.reload_env()

    def _files(self):
        return sorted(p for p in Path(self.tmp).rglob("*.md"))

    def test_a_note_lands_with_front_matter(self):
        capture.note(title="Per-case pricing", body="Charge per case.",
                     kind="decision", tags=["pricing"], links=["Scam Platform"])
        files = self._files()
        self.assertEqual(len(files), 1)
        text = files[0].read_text()
        self.assertIn("type: decision", text)
        self.assertIn("tags: [pricing]", text)
        self.assertIn("[[Scam Platform]]", text)
        self.assertIn("# Per-case pricing", text)

    def test_kind_decides_the_folder(self):
        capture.note(title="A project", body="x", kind="project")
        self.assertEqual(self._files()[0].parent.name, "projects")

    def test_an_unknown_kind_falls_back_rather_than_making_a_folder(self):
        capture.note(title="Odd", body="x", kind="../../escape")
        self.assertEqual(self._files()[0].parent.name, "notes")

    def test_the_daily_log_appends_and_never_overwrites(self):
        capture.log("first thing")
        capture.log("second thing")
        capture.log("third thing")
        daily = [f for f in self._files() if f.parent.name == "daily"]
        self.assertEqual(len(daily), 1, "a second line started a second file")
        text = daily[0].read_text()
        for line in ("first thing", "second thing", "third thing"):
            self.assertIn(line, text)

    def test_capture_still_goes_through_the_gate(self):
        """No write root means no note, exactly as for any other write."""
        os.environ["AERIS_WRITE_ROOTS"] = ""
        data.reload_env()
        out = capture.note(title="Nowhere", body="x")
        self.assertFalse(out["result"]["ok"])
        self.assertEqual(self._files(), [])

    def test_empty_capture_writes_nothing(self):
        out = capture.note(title="", body="")
        self.assertFalse(out["result"]["ok"])
        self.assertEqual(self._files(), [])


if __name__ == "__main__":
    unittest.main()
