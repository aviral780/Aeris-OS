"""An agent must have exactly Aeris's permissions and not one more.

The loop decides its own next step, so the thing worth testing is not whether
it plans well — it is whether a plan that wants to do something dangerous can
get past the gate by being inside an agent. It cannot, and these tests pin
that down without spending a model call: `_decide` is stubbed so the plan is
whatever the test says it is.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import actions, agents, data  # noqa: E402


class FakeVault:
    notes = {}
    edges = []

    def search(self, *a, **k):
        return []


class AgentGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aeris-agent-")
        self.root = Path(self.tmp) / "work"
        self.root.mkdir()
        os.environ["AERIS_WRITE_ROOTS"] = str(self.root)
        data.reload_env()
        actions.AUDIT_DIR = Path(self.tmp) / "audit"
        actions.AUDIT_LOG = actions.AUDIT_DIR / "actions.jsonl"
        actions._pending.clear()
        agents.RUNS.clear()
        self._real_decide = agents._decide

    def tearDown(self):
        agents._decide = self._real_decide
        os.environ.pop("AERIS_WRITE_ROOTS", None)
        data.reload_env()

    def _plans(self, *plans):
        """Feed the loop a fixed sequence of decisions."""
        seq = list(plans)

        def fake(run, vault):
            return (seq.pop(0), None) if seq else ({"done": True, "say": "finished"}, None)
        agents._decide = fake

    def test_a_dangerous_step_halts_the_run(self):
        marker = self.root / "must-not-exist"
        self._plans({"thought": "delete it", "tool": "run_command",
                     "args": {"command": "touch %s" % marker, "cwd": str(self.root)}})
        out = agents.start("tidy up", FakeVault())

        self.assertEqual(out["status"], "waiting")
        self.assertFalse(marker.exists(), "an agent ran a gated command unapproved")
        self.assertIsNotNone(out["pending"])

    def test_an_agent_cannot_approve_itself(self):
        """The run stops and stays stopped until something outside it answers."""
        marker = self.root / "still-must-not-exist"
        self._plans({"tool": "run_command",
                     "args": {"command": "touch %s" % marker, "cwd": str(self.root)}},
                    {"tool": "run_command",
                     "args": {"command": "touch %s" % marker, "cwd": str(self.root)}})
        out = agents.start("tidy up", FakeVault())

        # Resuming without an answer must not carry on regardless.
        again = agents.resume(out["id"], FakeVault())
        self.assertEqual(again["status"], "waiting")
        self.assertFalse(marker.exists())

    def test_it_carries_on_once_he_approves(self):
        marker = self.root / "approved"
        self._plans({"tool": "run_command",
                     "args": {"command": "touch %s" % marker, "cwd": str(self.root)}},
                    {"done": True, "say": "done, SIR"})
        out = agents.start("make the file", FakeVault())
        self.assertEqual(out["status"], "waiting")

        actions.confirm(out["pending"]["token"], approved=True)
        self.assertTrue(marker.exists())

        finished = agents.resume(out["id"], FakeVault())
        self.assertEqual(finished["status"], "done")

    def test_denial_leaves_the_disk_alone(self):
        marker = self.root / "denied"
        self._plans({"tool": "run_command",
                     "args": {"command": "touch %s" % marker, "cwd": str(self.root)}},
                    {"done": True, "say": "stopped"})
        out = agents.start("make the file", FakeVault())
        actions.confirm(out["pending"]["token"], approved=False)
        agents.resume(out["id"], FakeVault())
        self.assertFalse(marker.exists())

    # -- the bounds --------------------------------------------------------

    def test_it_gives_up_rather_than_looping_forever(self):
        agents._decide = lambda run, vault: (
            {"thought": "again", "tool": "search_brain", "args": {"query": "x"}}, None)
        out = agents.start("something impossible", FakeVault())
        self.assertEqual(out["status"], "failed")
        self.assertLessEqual(len(out["steps"]), agents.MAX_STEPS)

    def test_a_planning_failure_stops_cleanly(self):
        agents._decide = lambda run, vault: (None, "model unreachable")
        out = agents.start("anything", FakeVault())
        self.assertEqual(out["status"], "failed")
        self.assertIn("couldn't plan", out["say"])

    def test_an_unknown_tool_is_observed_not_executed(self):
        self._plans({"tool": "rm_minus_rf", "args": {}},
                    {"done": True, "say": "gave up on that"})
        out = agents.start("do something odd", FakeVault())
        self.assertEqual(out["status"], "done")
        self.assertIn("no tool called", out["steps"][0]["observation"].lower())


if __name__ == "__main__":
    unittest.main()
