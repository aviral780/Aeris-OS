"""One connector's failure must never take the whole server down.

This is a live-fire pin, not a hypothetical one. A stray character (U+276F,
'❯' — almost certainly picked up when a Railway token was copy-pasted out of
a decorated terminal prompt) crashed the actual running server: uncaught
UnicodeEncodeError, several layers down inside http.client, past every
except clause in railway.py, past banner()'s bare `mod.status()` call, taking
the process down before it ever bound the port. From that point every single
page load would have failed the same way, forever, until the token was fixed
by hand and nobody could tell why from the outside.

Three independent layers now stand between one bad value and a dead server,
and each is tested here on the exact character that caused the real failure.
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import data, main, railway, setup  # noqa: E402

BAD_CHAR = "❯"                            # ❯ — the exact character from the crash


class HeaderSafetyTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("RAILWAY_TOKEN", None)
        data.reload_env()

    def test_a_corrupted_token_is_refused_at_entry_rather_than_written(self):
        safe, why = setup._header_safe("abc123%sdef456" % BAD_CHAR)
        self.assertFalse(safe)
        self.assertIn(BAD_CHAR, why)

    def test_an_ordinary_token_is_accepted(self):
        safe, _why = setup._header_safe("railway_abcdef0123456789")
        self.assertTrue(safe)

    def test_railway_status_reports_cleanly_instead_of_crashing(self):
        """This is the exact call that crashed the live server."""
        os.environ["RAILWAY_TOKEN"] = "abc123%sdef456" % BAD_CHAR
        data.reload_env()
        st = railway.status()                  # must not raise
        self.assertFalse(st["ok"])
        self.assertIn(BAD_CHAR, st["detail"])

    def test_railway_query_reports_cleanly_instead_of_crashing(self):
        os.environ["RAILWAY_TOKEN"] = "abc123%sdef456" % BAD_CHAR
        data.reload_env()
        out, err = railway.overview()           # must not raise
        self.assertIsNone(out)
        self.assertIn(BAD_CHAR, err)


class SafeStatusTest(unittest.TestCase):
    """The general-purpose guarantee: no connector's status() can propagate,
    whatever it throws — not just this one character, not just Railway."""

    def test_a_crashing_module_is_reported_not_raised(self):
        class Exploding:
            @staticmethod
            def status():
                raise RuntimeError("anything at all could go wrong here")
        out = main._safe_status("exploding", Exploding)
        self.assertFalse(out["ok"])
        self.assertIn("crashed", out["detail"])
        self.assertIn("RuntimeError", out["detail"])

    def test_a_working_module_passes_through_untouched(self):
        class Fine:
            @staticmethod
            def status():
                return {"ok": True, "detail": "all good"}
        self.assertEqual(main._safe_status("fine", Fine), {"ok": True, "detail": "all good"})

    def test_the_real_railway_module_survives_the_real_crash_end_to_end(self):
        """No mocking — the actual module, the actual bad value, through the
        actual helper that both the banner and /api/status call."""
        os.environ["RAILWAY_TOKEN"] = "abc123%sdef456" % BAD_CHAR
        data.reload_env()
        try:
            out = main._safe_status("railway", railway)
        finally:
            os.environ.pop("RAILWAY_TOKEN", None)
            data.reload_env()
        self.assertFalse(out["ok"])


if __name__ == "__main__":
    unittest.main()
