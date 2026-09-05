"""GitHub is the first connector that reaches outside Aviral's machine, so the
things worth pinning are the boundary ones: that writing to GitHub cannot
happen without him, that the brief ranks by what actually blocks him, and that
the endpoint which returns issues and pull requests together is not allowed to
report a pull request as an issue.

The HTTP layer is stubbed. These are tests of Aeris's logic, not of GitHub's.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import actions, data, github  # noqa: E402


class GitHubTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aeris-gh-")
        os.environ["GITHUB_TOKEN"] = "stub-token"
        data.reload_env()
        github._cache.clear()
        actions.AUDIT_DIR = Path(self.tmp) / "audit"
        actions.AUDIT_LOG = actions.AUDIT_DIR / "actions.jsonl"
        actions._pending.clear()
        self._real_get, self._real_post = github._get, github._post

    def tearDown(self):
        github._get, github._post = self._real_get, self._real_post
        for key in ("GITHUB_TOKEN", "GITHUB_REPOS"):
            os.environ.pop(key, None)
        data.reload_env()

    def _routes(self, table):
        # Longest prefix wins: "/user/repos" must not be answered by "/user".
        ordered = sorted(table.items(), key=lambda kv: -len(kv[0]))

        def fake_get(path, params=None, use_cache=True):
            for prefix, payload in ordered:
                if path.startswith(prefix):
                    return payload, None
            return [], None
        github._get = fake_get

    # -- the boundary ------------------------------------------------------

    def test_opening_an_issue_needs_approval(self):
        sent = []
        github._post = lambda path, payload: (sent.append((path, payload)),
                                              ({"number": 7, "html_url": "u"}, None))[1]
        out = actions.propose("github_create_issue",
                              {"repo": "a/b", "title": "Broken", "body": "x"})
        self.assertEqual(out["status"], "needs_confirmation")
        self.assertEqual(sent, [], "an issue was filed before he approved it")

        actions.confirm(out["token"], approved=True)
        self.assertEqual(len(sent), 1)

    def test_denying_files_nothing(self):
        sent = []
        github._post = lambda path, payload: (sent.append(path),
                                              ({"number": 1, "html_url": ""}, None))[1]
        out = actions.propose("github_comment",
                              {"repo": "a/b", "number": 3, "body": "hi"})
        actions.confirm(out["token"], approved=False)
        self.assertEqual(sent, [])

    def test_github_writes_are_marked_outward(self):
        for name in ("github_create_issue", "github_comment"):
            self.assertTrue(actions.REGISTRY[name].outward)
            self.assertEqual(actions.REGISTRY[name].risk, actions.CONFIRM)

    # -- reading -----------------------------------------------------------

    def test_a_pull_request_is_not_reported_as_an_issue(self):
        """/issues returns both. Only one of them is an issue."""
        self._routes({"/repos/a/b/issues": [
            {"number": 1, "title": "A real issue", "created_at": "2026-09-01T00:00:00Z"},
            {"number": 2, "title": "A pull request", "created_at": "2026-09-01T00:00:00Z",
             "pull_request": {"url": "..."}},
        ]})
        got, err = github.issues("a/b")
        self.assertIsNone(err)
        self.assertEqual([i["number"] for i in got], [1])

    def test_the_brief_puts_a_red_build_first(self):
        self._routes({
            "/user": {"login": "aviral", "name": "Aviral", "public_repos": 2},
            "/user/repos": [
                {"full_name": "aviral/scam", "name": "scam", "owner": {"login": "aviral"},
                 "pushed_at": "2026-09-05T00:00:00Z", "open_issues_count": 1,
                 "default_branch": "main", "html_url": "u"},
            ],
            "/repos/aviral/scam/actions/runs": {"workflow_runs": [
                {"name": "tests", "status": "completed", "conclusion": "failure",
                 "head_branch": "main", "created_at": "2026-09-05T00:00:00Z",
                 "html_url": "ci"}]},
            "/repos/aviral/scam/pulls": [
                {"number": 4, "title": "Add thing", "user": {"login": "aviral"},
                 "draft": False, "created_at": "2026-08-01T00:00:00Z",
                 "html_url": "pr", "head": {"ref": "feat"}}],
            "/repos/aviral/scam/commits": [
                {"sha": "abc1234", "html_url": "c",
                 "commit": {"message": "did a thing\n\nbody",
                            "author": {"date": "2026-09-05T00:00:00Z"}}}],
        })
        out, err = github.overview()
        self.assertIsNone(err)
        self.assertEqual(len(out["failing"]), 1)
        self.assertEqual(out["failing"][0]["repo"], "aviral/scam")
        self.assertEqual(len(out["waiting"]), 1)
        self.assertEqual(out["active"][0]["commits"], 1)

    def test_a_green_build_is_not_reported_as_failing(self):
        self._routes({
            "/user": {"login": "aviral"},
            "/user/repos": [{"full_name": "aviral/ok", "name": "ok",
                             "owner": {"login": "aviral"},
                             "pushed_at": "2026-09-05T00:00:00Z",
                             "open_issues_count": 0, "default_branch": "main"}],
            "/repos/aviral/ok/actions/runs": {"workflow_runs": [
                {"name": "tests", "conclusion": "success", "head_branch": "main",
                 "created_at": "2026-09-05T00:00:00Z"}]},
        })
        out, _ = github.overview()
        self.assertEqual(out["failing"], [])

    def test_named_repos_are_used_when_the_account_endpoint_is_forbidden(self):
        """A fine-grained token scoped to selected repositories gets 403 on
        /user/repos. That is a sensible token, not a misconfiguration."""
        os.environ["GITHUB_REPOS"] = "aviral/scam, aviral/clinic"
        data.reload_env()
        asked = []

        def fake_get(path, params=None, use_cache=True):
            asked.append(path)
            if path == "/user/repos":
                return None, "GitHub refused that"
            return {"full_name": path.replace("/repos/", ""), "name": "x",
                    "owner": {"login": "aviral"}, "pushed_at": "2026-09-05T00:00:00Z",
                    "open_issues_count": 0, "default_branch": "main"}, None
        github._get = fake_get

        got, err = github.repos()
        self.assertIsNone(err)
        self.assertEqual([r["full_name"] for r in got], ["aviral/scam", "aviral/clinic"])
        self.assertNotIn("/user/repos", asked, "asked an endpoint it was told to skip")

    def test_commits_are_not_filtered_by_author(self):
        """GitHub matches `author` against the commit identity, so a different
        laptop or work email reports zero commits on an active repo. Answering
        'you did nothing this week' when he did is the failure to avoid."""
        seen = {}

        def fake_get(path, params=None, use_cache=True):
            if "/commits" in path:
                seen.update(params or {})
                return [{"sha": "a" * 7, "html_url": "",
                         "commit": {"message": "work", "author": {"date": "2026-09-05"}}}], None
            if path == "/user":
                return {"login": "aviral"}, None
            if path == "/user/repos":
                return [{"full_name": "aviral/x", "name": "x",
                         "owner": {"login": "aviral"},
                         "pushed_at": "2026-09-05T00:00:00Z",
                         "open_issues_count": 0, "default_branch": "main"}], None
            return {"workflow_runs": []} if "runs" in path else [], None
        github._get = fake_get

        out, _ = github.overview()
        self.assertNotIn("author", seen)
        self.assertEqual(out["active"][0]["commits"], 1)

    def test_no_ci_is_not_a_failure(self):
        self._routes({"/repos/a/b/actions/runs": {"workflow_runs": []}})
        ci, err = github.ci_status("a/b")
        self.assertIsNone(ci)
        self.assertIsNone(err)

    # -- degrading ---------------------------------------------------------

    def test_without_a_token_it_says_so_rather_than_guessing(self):
        os.environ["GITHUB_TOKEN"] = ""
        data.reload_env()
        self.assertFalse(github.configured())
        st = github.status()
        self.assertFalse(st["ok"])
        self.assertIn("GITHUB_TOKEN", st["detail"])

    def test_an_api_failure_is_reported_not_swallowed(self):
        github._get = lambda path, params=None, use_cache=True: (None, "GitHub said no.")
        out, err = github.overview()
        self.assertIsNone(out)
        self.assertEqual(err, "GitHub said no.")

    def test_the_token_never_appears_in_status(self):
        self._routes({"/user": {"login": "aviral"}})
        self.assertNotIn("stub-token", repr(github.status()))


if __name__ == "__main__":
    unittest.main()
