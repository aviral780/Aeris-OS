"""The setup tool appends to the one file in this project that must not be
lost, so what is tested here is restraint: that it never rewrites a value,
never drops one, never prints one, and does nothing at all the second time.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import setup  # noqa: E402

EXAMPLE = """# Aeris
ELEVENLABS_API_KEY=

# The model.
AERIS_LLM=auto

# GitHub — read access is enough.
GITHUB_TOKEN=
GITHUB_REPOS=

# Where she may write.
AERIS_WRITE_ROOTS=
"""

EXISTING = """# Aeris — secrets.
ELEVENLABS_API_KEY=sk_secret_value_here

AERIS_LLM=auto
PORT=4719
"""


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aeris-setup-"))
        self._real_env, self._real_example = setup.ENV, setup.EXAMPLE
        setup.ENV = self.tmp / ".env"
        setup.EXAMPLE = self.tmp / ".env.example"
        setup.EXAMPLE.write_text(EXAMPLE)

    def tearDown(self):
        setup.ENV, setup.EXAMPLE = self._real_env, self._real_example

    def test_an_existing_secret_survives_untouched(self):
        setup.ENV.write_text(EXISTING)
        setup.merge()
        text = setup.ENV.read_text()
        self.assertIn("ELEVENLABS_API_KEY=sk_secret_value_here", text)
        self.assertEqual(text.count("ELEVENLABS_API_KEY="), 1,
                         "the key was duplicated, which would shadow the real one")

    def test_settings_he_already_has_are_not_re_added(self):
        setup.ENV.write_text(EXISTING)
        out = setup.merge()
        self.assertNotIn("AERIS_LLM", out["added"])
        self.assertEqual(setup.ENV.read_text().count("AERIS_LLM="), 1)

    def test_missing_settings_are_added(self):
        setup.ENV.write_text(EXISTING)
        out = setup.merge()
        for key in ("GITHUB_TOKEN", "GITHUB_REPOS", "AERIS_WRITE_ROOTS"):
            self.assertIn(key, out["added"])
            self.assertIn(key + "=", setup.ENV.read_text())

    def test_a_setting_he_does_not_have_in_the_example_is_kept(self):
        """PORT is only in his file. It must not be dropped."""
        setup.ENV.write_text(EXISTING)
        setup.merge()
        self.assertIn("PORT=4719", setup.ENV.read_text())

    def test_running_it_twice_changes_nothing_the_second_time(self):
        setup.ENV.write_text(EXISTING)
        setup.merge()
        after_first = setup.ENV.read_text()
        out = setup.merge()
        self.assertEqual(out["added"], [])
        self.assertEqual(setup.ENV.read_text(), after_first)

    def test_a_backup_is_taken_before_writing(self):
        setup.ENV.write_text(EXISTING)
        out = setup.merge()
        self.assertTrue(out["backup"])
        self.assertIn("sk_secret_value_here", Path(out["backup"]).read_text())

    def test_check_mode_writes_nothing(self):
        setup.ENV.write_text(EXISTING)
        out = setup.merge(write=False)
        self.assertTrue(out["added"])
        self.assertEqual(setup.ENV.read_text(), EXISTING)

    def test_no_secret_value_is_ever_in_the_report(self):
        setup.ENV.write_text(EXISTING)
        out = setup.merge()
        self.assertNotIn("sk_secret_value_here", repr(out))

    def test_a_missing_env_is_created_from_the_example(self):
        out = setup.merge()
        self.assertTrue(out["created"])
        self.assertTrue(setup.ENV.is_file())
        self.assertEqual(oct(setup.ENV.stat().st_mode)[-3:], "600")

    def test_it_names_the_settings_still_needing_a_value(self):
        setup.ENV.write_text(EXISTING)
        out = setup.merge()
        self.assertIn("GITHUB_TOKEN", out["missing_values"])

    def test_a_filled_setting_is_not_reported_as_missing(self):
        setup.ENV.write_text(EXISTING + "\nGITHUB_TOKEN=ghp_something\n")
        out = setup.merge()
        self.assertNotIn("GITHUB_TOKEN", out["missing_values"])


def _fixture(prefix, length=34):
    """A string shaped like a credential without being one.

    Deliberately assembled rather than written out. A literal token in a test
    file is still a literal token in a repository: a realistic-looking sample
    trips GitHub's push protection at best, and at worst someone reaches for a
    real key to make the test convincing. Filler digits satisfy the patterns
    and convince nobody.
    """
    return prefix + "0" * length


class RedactionTest(unittest.TestCase):
    """A key pasted into a Claude chat a year ago must not end up sitting in
    his vault in plain text because the importer copied it there."""

    def test_real_key_shapes_are_removed(self):
        from agent.ingest import redact
        for prefix, label in (("sk-", "sk-"), ("sk_", "sk_"), ("ghp_", "gh"),
                              ("github_pat_", "github_pat"), ("xoxb-", "xox"),
                              ("AIzaSy", "AIza"), ("ntn_", "ntn_"), ("AKIA", "AKIA")):
            token = _fixture(prefix)
            clean, found = redact("my key is %s ok" % token)
            self.assertIn(label, found, prefix)
            self.assertNotIn(token, clean, prefix)
            self.assertIn("ok", clean, "redaction ate the surrounding sentence")

    def test_ordinary_prose_is_left_alone(self):
        from agent.ingest import redact
        for sample in ("nothing secret in this sentence",
                       "the scam detection platform is the main one",
                       "I prefer per-case pricing over a retainer"):
            clean, found = redact(sample)
            self.assertEqual(found, [])
            self.assertEqual(clean, sample)

    def test_the_secret_is_never_in_what_is_returned(self):
        from agent.ingest import redact
        secret = _fixture("sk_", 40)
        clean, found = redact("here it is: %s ok" % secret)
        self.assertNotIn(secret, clean)
        self.assertNotIn(secret, repr(found))


if __name__ == "__main__":
    unittest.main()
