"""The gate is the only thing standing between Aeris and Aviral's disk.

These tests are about what she must refuse, not what she can do. Standard
library only, in keeping with the rest of the project:

    cd Aeris && python3 -m unittest discover tests -v
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import actions, data  # noqa: E402


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aeris-test-")
        self.root = Path(self.tmp) / "work"
        self.root.mkdir()
        os.environ["AERIS_WRITE_ROOTS"] = str(self.root)
        data.reload_env()
        actions.AUDIT_DIR = Path(self.tmp) / "audit"
        actions.AUDIT_LOG = actions.AUDIT_DIR / "actions.jsonl"
        actions._pending.clear()

    def tearDown(self):
        os.environ.pop("AERIS_WRITE_ROOTS", None)
        data.reload_env()

    # -- the split ---------------------------------------------------------

    def test_local_action_runs_on_sight(self):
        target = self.root / "note.md"
        out = actions.propose("write_file", {"path": str(target), "content": "hello"})
        self.assertEqual(out["status"], "done")
        self.assertEqual(target.read_text(), "hello")

    def test_confirm_action_does_not_run_until_approved(self):
        marker = self.root / "should-not-exist"
        out = actions.propose("run_command",
                              {"command": "touch %s" % marker, "cwd": str(self.root)})
        self.assertEqual(out["status"], "needs_confirmation")
        self.assertFalse(marker.exists(), "the command ran before it was approved")

        actions.confirm(out["token"], approved=True)
        self.assertTrue(marker.exists(), "approval did not run the command")

    def test_denial_never_executes(self):
        marker = self.root / "denied"
        out = actions.propose("run_command",
                              {"command": "touch %s" % marker, "cwd": str(self.root)})
        done = actions.confirm(out["token"], approved=False)
        self.assertEqual(done["status"], "denied")
        self.assertFalse(marker.exists())

    def test_a_token_is_single_use(self):
        out = actions.propose("run_command", {"command": "true", "cwd": str(self.root)})
        actions.confirm(out["token"], approved=True)
        again = actions.confirm(out["token"], approved=True)
        self.assertEqual(again["status"], "expired")

    # -- where she may write ----------------------------------------------

    def test_write_outside_the_root_is_refused(self):
        outside = Path(self.tmp) / "outside.md"
        out = actions.propose("write_file", {"path": str(outside), "content": "x"})
        self.assertEqual(out["status"], "done")
        self.assertFalse(out["result"]["ok"])
        self.assertFalse(outside.exists())

    def test_traversal_out_of_the_root_is_refused(self):
        escape = str(self.root / ".." / "escaped.md")
        out = actions.propose("write_file", {"path": escape, "content": "x"})
        self.assertFalse(out["result"]["ok"])
        self.assertFalse((Path(self.tmp) / "escaped.md").exists())

    def test_no_write_roots_means_no_writing(self):
        os.environ["AERIS_WRITE_ROOTS"] = ""
        data.reload_env()
        out = actions.propose("write_file", {"path": str(self.root / "a.md"), "content": "x"})
        self.assertFalse(out["result"]["ok"])
        self.assertIn("write root", out["result"]["summary"].lower())

    # -- the things she may not quietly touch ------------------------------

    def test_env_file_escalates_to_confirmation(self):
        self.assertEqual(
            actions.risk_of("write_file", {"path": str(self.root / ".env")}),
            actions.CONFIRM)

    def test_agent_code_escalates_to_confirmation(self):
        own_code = actions.ROOT / "agent" / "actions.py"
        self.assertEqual(
            actions.risk_of("write_file", {"path": str(own_code)}), actions.CONFIRM)

    # -- commands ----------------------------------------------------------

    def test_command_does_not_reach_a_shell(self):
        """A semicolon is part of the argument, not a second command."""
        victim = self.root / "victim"
        victim.write_text("still here")
        out = actions.propose(
            "run_command",
            {"command": "echo hello ; rm %s" % victim, "cwd": str(self.root)})
        actions.confirm(out["token"], approved=True)
        self.assertTrue(victim.exists(), "the shell interpreted a metacharacter")

    def test_unknown_command_fails_honestly(self):
        out = actions.propose("run_command",
                              {"command": "definitelynotarealbinary", "cwd": str(self.root)})
        done = actions.confirm(out["token"], approved=True)
        self.assertFalse(done["result"]["ok"])

    # -- reversibility -----------------------------------------------------

    def test_overwrite_keeps_the_old_bytes(self):
        target = self.root / "keep.md"
        target.write_text("original")
        actions.propose("write_file", {"path": str(target), "content": "replaced"})
        backups = list((self.root / actions.BACKUP_DIRNAME).glob("keep.md.*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "original")

    def test_delete_keeps_the_old_bytes(self):
        target = self.root / "gone.md"
        target.write_text("precious")
        out = actions.propose("delete_file", {"path": str(target)})
        done = actions.confirm(out["token"], approved=True)
        self.assertFalse(target.exists())
        self.assertEqual(Path(done["result"]["backup"]).read_text(), "precious")

    # -- the record --------------------------------------------------------

    def test_everything_reaches_the_audit_log(self):
        actions.propose("write_file", {"path": str(self.root / "a.md"), "content": "x"})
        out = actions.propose("run_command", {"command": "true", "cwd": str(self.root)})
        actions.confirm(out["token"], approved=False)

        rows = [json.loads(l) for l in
                actions.AUDIT_LOG.read_text().splitlines() if l.strip()]
        events = [r["event"] for r in rows]
        self.assertIn("proposed", events)
        self.assertIn("done", events)
        self.assertIn("denied", events)

    def test_unknown_capability_is_refused(self):
        out = actions.propose("format_the_disk", {})
        self.assertEqual(out["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
