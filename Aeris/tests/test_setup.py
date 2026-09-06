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


if __name__ == "__main__":
    unittest.main()
