"""Gmail, Calendar and Notion — the boundary tests.

The one that matters most is test_there_is_no_way_to_send_mail. Aviral's
standing rule is that nothing is ever sent, and the strongest way to keep a
rule like that is not a prompt or a permission check but the absence of the
code: there is no send function in google.py, and this asserts it stays that
way even if someone later grants the scope that would allow one.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import actions, data, google, notion  # noqa: E402


class NoSendTest(unittest.TestCase):
    def test_there_is_no_way_to_send_mail(self):
        """Not 'sending is refused' — sending is not implemented."""
        source = Path(google.__file__).read_text()
        self.assertNotIn("/messages/send", source)
        self.assertNotIn("users/me/messages/send", source)
        for name in dir(google):
            self.assertNotIn("send", name.lower(),
                             "google.py grew a %s function" % name)

    def test_the_send_scope_is_never_requested(self):
        os.environ["GOOGLE_ALLOW_DRAFTS"] = "1"
        data.reload_env()
        try:
            self.assertNotIn("https://www.googleapis.com/auth/gmail.send",
                             google.scopes())
        finally:
            os.environ.pop("GOOGLE_ALLOW_DRAFTS", None)
            data.reload_env()

    def test_scopes_are_read_only_by_default(self):
        os.environ.pop("GOOGLE_ALLOW_DRAFTS", None)
        data.reload_env()
        self.assertFalse(google.drafts_allowed())
        for scope in google.scopes():
            self.assertTrue(scope.endswith("readonly"), scope)

    def test_drafting_refuses_when_it_was_not_opted_into(self):
        os.environ.pop("GOOGLE_ALLOW_DRAFTS", None)
        data.reload_env()
        out = google.create_draft(to="a@b.com", subject="hi", body="x")
        self.assertFalse(out["ok"])
        self.assertIn("GOOGLE_ALLOW_DRAFTS", out["summary"])


class DraftGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aeris-conn-")
        actions.AUDIT_DIR = Path(self.tmp) / "audit"
        actions.AUDIT_LOG = actions.AUDIT_DIR / "actions.jsonl"
        actions._pending.clear()

    def test_a_draft_still_stops_and_asks(self):
        """It writes to an account outside this machine, so it is outward."""
        cap = actions.REGISTRY["draft_email"]
        self.assertEqual(cap.risk, actions.CONFIRM)
        self.assertTrue(cap.outward)

    def test_no_draft_is_created_before_approval(self):
        called = []
        real = google.create_draft
        google.create_draft = lambda **kw: called.append(kw) or {"ok": True,
                                                                 "summary": "saved"}
        try:
            out = actions.propose("draft_email",
                                  {"to": "a@b.com", "subject": "hi", "body": "x"})
            self.assertEqual(out["status"], "needs_confirmation")
            self.assertEqual(called, [])
            actions.confirm(out["token"], approved=True)
            self.assertEqual(len(called), 1)
        finally:
            google.create_draft = real

    def test_the_approval_line_says_it_will_not_be_sent(self):
        out = actions.propose("draft_email",
                              {"to": "ada@x.com", "subject": "Pricing", "body": "x"})
        self.assertIn("not sent", out["summary"])


class GmailParsingTest(unittest.TestCase):
    def test_an_address_is_split_from_a_display_name(self):
        self.assertEqual(google._split_address("Ada Lovelace <ada@x.com>"),
                         ("Ada Lovelace", "ada@x.com"))
        self.assertEqual(google._split_address("ada@x.com"),
                         ("ada@x.com", "ada@x.com"))

    def test_a_body_is_found_however_deeply_gmail_nests_it(self):
        import base64
        buried = {"mimeType": "multipart/mixed", "parts": [
            {"mimeType": "multipart/alternative", "parts": [
                {"mimeType": "text/html", "body": {"data": ""}},
                {"mimeType": "text/plain",
                 "body": {"data": base64.urlsafe_b64encode(b"the real text").decode()}},
            ]}]}
        self.assertEqual(google._body_text(buried), "the real text")

    def test_nesting_cannot_loop_forever(self):
        deep = {"mimeType": "multipart/mixed"}
        deep["parts"] = [deep]                 # a message that references itself
        self.assertEqual(google._body_text(deep), "")


class NotionTest(unittest.TestCase):
    def test_the_title_is_found_whatever_the_property_is_called(self):
        page = {"properties": {"Name": {"type": "title", "title": [
            {"plain_text": "Pricing decisions"}]}}}
        self.assertEqual(notion._title_of(page), "Pricing decisions")

    def test_an_untitled_page_does_not_crash(self):
        self.assertEqual(notion._title_of({"properties": {}}), "untitled")

    def test_without_a_token_it_says_so(self):
        os.environ["NOTION_TOKEN"] = ""
        data.reload_env()
        try:
            st = notion.status()
            self.assertFalse(st["ok"])
            self.assertIn("NOTION_TOKEN", st["detail"])
        finally:
            os.environ.pop("NOTION_TOKEN", None)
            data.reload_env()

    def test_a_403_explains_that_sharing_is_the_missing_step(self):
        """Notion shows an integration nothing until pages are shared with it,
        and 'no results' would read as an empty workspace."""
        import io
        import urllib.error
        os.environ["NOTION_TOKEN"] = "stub"
        data.reload_env()
        real = notion.urllib.request.urlopen

        def fake(req, timeout=None):
            raise urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b"{}"))
        notion.urllib.request.urlopen = fake
        try:
            _out, err = notion.search("anything")
            self.assertIn("share", err.lower())
        finally:
            notion.urllib.request.urlopen = real
            os.environ.pop("NOTION_TOKEN", None)
            data.reload_env()


if __name__ == "__main__":
    unittest.main()
