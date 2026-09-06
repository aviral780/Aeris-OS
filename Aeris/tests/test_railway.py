"""Railway was written without a token to test against, so these tests carry
more weight than usual — they are the only thing standing between a wrong
schema guess and Aeris confidently reporting that nothing is broken.

The one that matters most is test_a_graphql_error_is_not_an_empty_result.
GraphQL answers HTTP 200 with a null `data` and an `errors` array when the
query is wrong. A client that only checks the status code reads that as a
healthy account with nothing deployed — the exact shape of the GitHub
author-filter bug, except the lie it tells is "nothing is on fire".
"""
import io
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import data, llm, railway  # noqa: E402


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class RailwayTest(unittest.TestCase):
    def setUp(self):
        os.environ["RAILWAY_TOKEN"] = "stub-token"
        os.environ.pop("RAILWAY_TOKEN_TYPE", None)
        os.environ.pop("RAILWAY_PROJECT_ID", None)
        data.reload_env()
        railway._cache.clear()
        self._real_urlopen = railway.urllib.request.urlopen

    def tearDown(self):
        railway.urllib.request.urlopen = self._real_urlopen
        for key in ("RAILWAY_TOKEN", "RAILWAY_TOKEN_TYPE", "RAILWAY_PROJECT_ID"):
            os.environ.pop(key, None)
        data.reload_env()

    def _reply(self, payload, capture=None):
        def fake(req, timeout=None):
            if capture is not None:
                capture.append(req)
            return FakeResponse(json.dumps(payload).encode("utf-8"))
        railway.urllib.request.urlopen = fake

    def _reply_raw(self, raw_bytes):
        railway.urllib.request.urlopen = lambda req, timeout=None: FakeResponse(raw_bytes)

    # -- the whole point of this module ------------------------------------

    def test_a_graphql_error_is_not_an_empty_result(self):
        """HTTP 200 + errors + null data must never read as 'nothing broken'."""
        self._reply({"data": None, "errors": [
            {"message": "Cannot query field \"staticUrl\" on type \"Deployment\"."}]})
        out, err = railway.overview()
        self.assertIsNone(out)
        self.assertIn("staticUrl", err)

    def test_a_non_json_response_shows_what_it_actually_was(self):
        """The old version discarded the body and said only 'not JSON', which
        is unfalsifiable — there is no way to act on it. Whatever Railway
        actually sent (a Cloudflare challenge page, a login redirect, an
        empty body) must be visible in the error itself."""
        self._reply_raw(b"<html><title>Just a moment...</title></html>")
        out, err = railway.overview()
        self.assertIsNone(out)
        self.assertIn("Just a moment", err)

    def test_an_empty_non_json_response_says_so_rather_than_showing_nothing(self):
        self._reply_raw(b"")
        _out, err = railway.projects()
        self.assertIn("empty", err.lower())

    def test_null_data_without_errors_is_still_an_error(self):
        self._reply({"data": None})
        out, err = railway.projects()
        self.assertIsNone(out)
        self.assertIn("should not happen", err)

    def test_an_unreadable_project_is_not_a_healthy_project(self):
        """If deployments fail for a project, it lands in problems, not silence."""
        calls = []

        def fake(req, timeout=None):
            calls.append(req)
            body = json.loads(req.data.decode())
            if "projects" in body["query"]:
                return FakeResponse(json.dumps({"data": {"projects": {"edges": [
                    {"node": {"id": "p1", "name": "scam-api",
                              "services": {"edges": [{"node": {"id": "s1",
                                                               "name": "web"}}]},
                              "environments": {"edges": []}}}]}}}).encode())
            return FakeResponse(json.dumps(
                {"data": None, "errors": [{"message": "boom"}]}).encode())
        railway.urllib.request.urlopen = fake

        out, err = railway.overview()
        self.assertIsNone(err)
        self.assertEqual(out["broken"], [])
        self.assertEqual(len(out["problems"]), 1)
        self.assertIn("scam-api", out["problems"][0])

    # -- auth --------------------------------------------------------------

    def test_a_real_user_agent_is_sent(self):
        """Python's default urllib User-Agent is a common bot signature, and
        Cloudflare in front of backboard.railway.com answers it with an HTML
        challenge page instead of JSON — the exact failure this pins."""
        captured = []
        self._reply({"data": {"me": {"name": "Aviral"}}}, captured)
        railway.whoami()
        headers = {k.lower(): v for k, v in captured[0].header_items()}
        self.assertIn("user-agent", headers)
        self.assertNotIn("python-urllib", headers["user-agent"].lower())

    def test_an_account_token_uses_bearer(self):
        captured = []
        self._reply({"data": {"me": {"name": "Aviral"}}}, captured)
        railway.whoami()
        headers = {k.lower(): v for k, v in captured[0].header_items()}
        self.assertIn("authorization", headers)
        self.assertNotIn("project-access-token", headers)

    def test_a_project_token_uses_its_own_header(self):
        os.environ["RAILWAY_TOKEN_TYPE"] = "project"
        data.reload_env()
        captured = []
        self._reply({"data": {"me": {"name": "Aviral"}}}, captured)
        railway.whoami()
        headers = {k.lower(): v for k, v in captured[0].header_items()}
        self.assertIn("project-access-token", headers)
        self.assertNotIn("authorization", headers)

    def test_a_401_explains_the_header_rather_than_blaming_the_token(self):
        def fake(req, timeout=None):
            raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b"{}"))
        railway.urllib.request.urlopen = fake
        _out, err = railway.whoami()
        self.assertIn("RAILWAY_TOKEN_TYPE=project", err)

    # -- reading -----------------------------------------------------------

    def _project_and_deploys(self, deploys):
        def fake(req, timeout=None):
            body = json.loads(req.data.decode())
            if "projects" in body["query"]:
                return FakeResponse(json.dumps({"data": {"projects": {"edges": [
                    {"node": {"id": "p1", "name": "scam-api",
                              "services": {"edges": [
                                  {"node": {"id": "s1", "name": "web"}},
                                  {"node": {"id": "s2", "name": "worker"}}]},
                              "environments": {"edges": []}}}]}}}).encode())
            return FakeResponse(json.dumps(
                {"data": {"deployments": {"edges": [{"node": d} for d in deploys]}}}).encode())
        railway.urllib.request.urlopen = fake

    def test_a_crashed_service_is_reported_broken(self):
        self._project_and_deploys([
            {"id": "d1", "status": "CRASHED", "serviceId": "s1",
             "createdAt": "2026-09-06T10:00:00Z"},
            {"id": "d2", "status": "SUCCESS", "serviceId": "s2",
             "createdAt": "2026-09-06T09:00:00Z"}])
        out, err = railway.overview()
        self.assertIsNone(err)
        self.assertEqual(len(out["broken"]), 1)
        self.assertEqual(out["broken"][0]["service"], "web")
        self.assertEqual(len(out["live"]), 1)

    def test_only_the_newest_deployment_per_service_counts(self):
        """A failure that has since been redeployed over is history."""
        self._project_and_deploys([
            {"id": "new", "status": "SUCCESS", "serviceId": "s1",
             "createdAt": "2026-09-06T12:00:00Z"},
            {"id": "old", "status": "FAILED", "serviceId": "s1",
             "createdAt": "2026-09-06T09:00:00Z"}])
        out, _ = railway.overview()
        self.assertEqual(out["broken"], [])
        self.assertEqual(len(out["live"]), 1)

    def test_a_deploy_in_flight_is_neither_broken_nor_live(self):
        self._project_and_deploys([
            {"id": "d1", "status": "BUILDING", "serviceId": "s1",
             "createdAt": "2026-09-06T12:00:00Z"}])
        out, _ = railway.overview()
        self.assertEqual(out["broken"], [])
        self.assertEqual(out["live"], [])
        self.assertEqual(len(out["in_flight"]), 1)

    def test_an_unknown_status_is_not_silently_called_live(self):
        self._project_and_deploys([
            {"id": "d1", "status": "SOMETHING_NEW", "serviceId": "s1",
             "createdAt": "2026-09-06T12:00:00Z"}])
        out, _ = railway.overview()
        self.assertEqual((out["broken"], out["in_flight"], out["live"]), ([], [], []))

    # -- degrading ---------------------------------------------------------

    def test_without_a_token_it_says_so(self):
        os.environ["RAILWAY_TOKEN"] = ""
        data.reload_env()
        self.assertFalse(railway.configured())
        st = railway.status()
        self.assertFalse(st["ok"])
        self.assertIn("RAILWAY_TOKEN", st["detail"])

    def test_railway_is_free(self):
        os.environ["RAILWAY_TOKEN"] = ""
        data.reload_env()
        self.assertTrue(railway.status()["free"])

    # -- routing -----------------------------------------------------------

    def test_asking_if_it_is_down_routes_to_deploys_not_repos(self):
        for question in ("is anything down?", "did my deploy fail?",
                         "is the site up?", "what's broken?"):
            hit = next((name for name, pattern in llm.TOOL_CUES
                        if __import__("re").search(pattern, question.lower())), None)
            self.assertEqual(hit, "check_deploys", question)

    def test_asking_about_ci_still_routes_to_repos(self):
        import re
        for question in ("any open prs?", "what's failing in ci?", "my repos"):
            hit = next((name for name, pattern in llm.TOOL_CUES
                        if re.search(pattern, question.lower())), None)
            self.assertEqual(hit, "check_repos", question)


if __name__ == "__main__":
    unittest.main()
