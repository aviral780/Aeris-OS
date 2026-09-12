"""The standing snapshot that goes into every system prompt.

The point of briefing.py is that Aeris knows the state of his work without
being asked in exactly the right words. That makes it a thing that runs on
every single turn, which makes its failure modes the interesting part: a
connector that is slow, broken, or returning nonsense must cost nothing more
than its own section, and must never be the reason a turn fails.
"""
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import briefing  # noqa: E402


class BriefingTest(unittest.TestCase):
    def setUp(self):
        briefing._cache.update({"text": "", "at": 0.0, "busy": False})
        self._real = (briefing.github, briefing.railway, briefing.notion)

    def tearDown(self):
        briefing.github, briefing.railway, briefing.notion = self._real
        briefing._cache.update({"text": "", "at": 0.0, "busy": False})

    class _Stub:
        def __init__(self, configured=True, overview=None, err=None, search=None):
            self._configured, self._overview = configured, overview
            self._err, self._search = err, search

        def configured(self):
            return self._configured

        def overview(self, *a, **k):
            return (None, self._err) if self._err else (self._overview, None)

        def search(self, *a, **k):
            return (None, self._err) if self._err else (self._search or [], None)

    def test_nothing_configured_is_an_empty_snapshot_not_an_error(self):
        briefing.github = self._Stub(configured=False)
        briefing.railway = self._Stub(configured=False)
        briefing.notion = self._Stub(configured=False)
        self.assertEqual(briefing.build(), "")

    def test_a_connector_that_raises_costs_only_its_own_section(self):
        """This is the whole reason it is wrapped. A Railway token with a
        stray character once crashed urllib several layers below anything
        railway.py catches — if that happened here it would break the system
        prompt on every turn, not just the Railway line."""
        class Exploding:
            def configured(self):
                return True

            def overview(self, *a, **k):
                raise RuntimeError("boom")

        briefing.github = self._Stub(overview={
            "repos": 2, "days": 7, "failing": [], "waiting": [],
            "stale": [], "active": [{"repo": "x/ScamShield", "commits": 4,
                                     "latest": "fix the scorer"}]})
        briefing.railway = Exploding()
        briefing.notion = self._Stub(configured=False)

        out = briefing.build()
        self.assertIn("ScamShield", out)
        self.assertNotIn("Railway", out)

    def test_a_failing_repo_is_named_in_the_snapshot(self):
        briefing.github = self._Stub(overview={
            "repos": 3, "days": 7,
            "failing": [{"repo": "x/ScamShield-web", "why": "tests failed on main",
                         "age_days": 1, "url": "u"}],
            "waiting": [], "stale": [], "active": []})
        briefing.railway = self._Stub(configured=False)
        briefing.notion = self._Stub(configured=False)
        out = briefing.build()
        self.assertIn("FAILING", out)
        self.assertIn("ScamShield-web", out)

    def test_notion_pages_are_listed_by_topic(self):
        briefing.github = self._Stub(configured=False)
        briefing.railway = self._Stub(configured=False)
        briefing.notion = self._Stub(search=[
            {"id": "p1", "title": "Job Tracker", "kind": "page", "edited": "2026-09-01"}])
        out = briefing.build()
        self.assertIn("Job Tracker", out)

    def test_the_same_page_is_not_listed_once_per_topic(self):
        """Four topic searches against one small workspace return the same
        page repeatedly, and a prompt that says 'Job Tracker' four times
        reads as four different pages."""
        briefing.github = self._Stub(configured=False)
        briefing.railway = self._Stub(configured=False)
        briefing.notion = self._Stub(search=[
            {"id": "p1", "title": "Job Tracker", "kind": "page", "edited": "2026-09-01"}])
        self.assertEqual(briefing.build().count("Job Tracker"), 1)

    def test_an_empty_notion_contributes_no_bare_header(self):
        briefing.github = self._Stub(configured=False)
        briefing.railway = self._Stub(configured=False)
        briefing.notion = self._Stub(search=[])
        self.assertEqual(briefing.build(), "")

    def _counting(self, calls, slow=0.0):
        class Counting:
            def configured(self):
                return True

            def overview(self, *a, **k):
                if slow:
                    time.sleep(slow)
                calls.append(1)
                return {"repos": 1, "days": 7, "failing": [], "waiting": [],
                        "stale": [], "active": []}, None
        briefing.railway = self._Stub(configured=False)
        briefing.notion = self._Stub(configured=False)
        return Counting()

    def test_a_warm_cache_costs_nothing(self):
        """summary() is called while building the system prompt, so it is on
        the path of every single question. Three API round trips there would
        make her slower the better connected she got."""
        calls = []
        briefing.github = self._counting(calls)
        briefing._cache.update({"text": "## GitHub — warm", "at": time.time()})
        for _ in range(3):
            self.assertEqual(briefing.summary(), "## GitHub — warm")
        self.assertEqual(calls, [])

    def test_a_stale_cache_answers_now_and_refreshes_behind_the_turn(self):
        """The whole point. A stale snapshot must not make him wait — he gets
        what she has, and the new one lands for the next question."""
        calls = []
        briefing.github = self._counting(calls, slow=0.15)
        briefing._cache.update({"text": "## old", "at": 1.0})     # long stale

        started = time.time()
        first = briefing.summary()
        elapsed = time.time() - started

        self.assertEqual(first, "## old")          # answered from the stale copy
        self.assertLess(elapsed, 0.1)              # without waiting for the rebuild

        for _ in range(40):                        # let the background work land
            if briefing._cache["text"] != "## old":
                break
            time.sleep(0.02)
        self.assertIn("GitHub", briefing._cache["text"])

    def test_only_one_refresh_runs_at_a_time(self):
        calls = []
        briefing.github = self._counting(calls, slow=0.1)
        briefing._cache.update({"text": "", "at": 1.0})
        for _ in range(5):
            briefing.summary()
        for _ in range(40):
            if calls:
                break
            time.sleep(0.02)
        time.sleep(0.1)
        self.assertEqual(len(calls), 1)

    def test_a_forced_refresh_is_synchronous(self):
        """warm() at boot and the tests both need the version that waits."""
        calls = []
        briefing.github = self._counting(calls)
        briefing._cache.update({"text": "", "at": 1.0})
        out = briefing.summary(refresh=True)
        self.assertEqual(len(calls), 1)
        self.assertIn("GitHub", out)


class SystemPromptTest(unittest.TestCase):
    """What the model is actually told before it picks a tool.

    This exists because of a live failure: with a model connected and an
    empty vault, "what am I working on" went to search_brain and came back
    with nothing. prompt.md listed six tools and none of the connectors, so
    the model had no way to know that his work lives in GitHub and Notion
    rather than in his files.
    """
    def setUp(self):
        from collections import Counter

        from agent import briefing as b
        from agent import data
        self.b = b
        self.data = data
        b._cache.update({"text": "", "at": 0.0})

        class StubVault:
            notes, edges, roots, warnings = {}, [], [], []

            def kind_counts(self):
                return Counter()

            def top_hubs(self, n=5):
                return []

            def search(self, *a, **k):
                return []
        self.vault = StubVault()
        self._demo = os.environ.get("AERIS_DEMO")
        os.environ["AERIS_DEMO"] = "0"
        data.reload_env()

    def tearDown(self):
        if self._demo is None:
            os.environ.pop("AERIS_DEMO", None)
        else:
            os.environ["AERIS_DEMO"] = self._demo
        self.data.reload_env()
        self.b._cache.update({"text": "", "at": 0.0})

    def test_an_empty_vault_steers_the_model_to_the_connectors(self):
        from agent import main
        prompt = main.system_prompt(self.vault, "what am I working on")
        self.assertIn("check_repos", prompt)
        self.assertIn("search_notion", prompt)
        self.assertIn("not where the answer is", prompt)

    def test_the_connector_tools_are_documented_to_the_model_at_all(self):
        """The original list stopped at plan_day, so check_repos,
        check_deploys and search_notion were invisible to the model."""
        from agent import main
        prompt = main.system_prompt(self.vault, "anything")
        for tool in ("check_repos", "check_deploys", "search_notion",
                     "capture_note", "look_at_screen"):
            self.assertIn(tool, prompt, tool)

    def test_the_snapshot_reaches_the_prompt(self):
        from agent import main
        self.b._cache.update({"text": "## GitHub — 1 repo\n- x/ScamShield: 4 commits",
                              "at": 9e18})       # far future: never rebuilt here
        prompt = main.system_prompt(self.vault, "what am I working on")
        self.assertIn("ScamShield", prompt)


class FastPathTest(unittest.TestCase):
    """Thirty to forty seconds for "is anything down" was two claude CLI
    invocations — one to pick the tool, one to reword the answer it came
    back with. A question that names a connector unmistakably should reach
    that connector and nothing else.
    """
    def setUp(self):
        from collections import Counter

        from agent import main, tools
        self.main, self.tools = main, tools
        self._run = tools.run
        self._probe = main.llm.probe
        self._call = main.llm.call
        main.STATE["history"].clear()

        self.ran = []
        tools.run = lambda name, v, args: (
            self.ran.append((name, args))
            or {"tool": name, "spoken": "Aviral, SIR — web on scam-api is crashed.",
                "card": {"title": name}})

        # Any model use at all is a failure of the fast path, so make it loud
        # rather than slow: these raise instead of returning something.
        def no_model(*a, **k):
            raise AssertionError("the model was called on a fast-path turn")
        main.llm.probe = no_model
        main.llm.call = no_model

        class StubVault:
            notes, edges, roots, warnings = {}, [], [], []

            def kind_counts(self):
                return Counter()

            def top_hubs(self, n=5):
                return []

            def search(self, *a, **k):
                return []
        self._vault_get = main.vault_mod.get
        main.vault_mod.get = lambda *a, **k: StubVault()

    def tearDown(self):
        self.tools.run = self._run
        self.main.llm.probe = self._probe
        self.main.llm.call = self._call
        self.main.vault_mod.get = self._vault_get
        self.main.STATE["history"].clear()

    def test_a_deploy_question_never_reaches_the_model(self):
        out = self.main.handle_turn("is anything down on railway")
        self.assertEqual(out["by"], "direct")
        self.assertEqual(out["tool"], "check_deploys")
        self.assertEqual([n for n, _ in self.ran], ["check_deploys"])
        self.assertFalse(out["degraded"])

    def test_the_notion_query_survives_the_fast_path(self):
        """The fast path must carry the extracted query, or it reintroduces
        the empty-query bug by a different door."""
        self.main.handle_turn("show me my job tracker")
        self.assertEqual(self.ran[0][0], "search_notion")
        self.assertEqual(self.ran[0][1].get("query"), "job tracker")

    def test_a_fast_answer_still_lands_in_history(self):
        """Skipping the model must not skip the context the next turn needs."""
        self.main.handle_turn("is anything down on railway")
        roles = [t["role"] for t in self.main.STATE["history"]]
        self.assertEqual(roles, ["user", "assistant"])

    def test_searching_his_files_is_not_on_the_fast_path(self):
        """Deciding a question is about his notes is judgement, not pattern
        matching — that one goes to the model, which is allowed to be slow."""
        self.assertNotIn("search_brain", self.main.FAST_TOOLS)

    def test_the_second_model_call_is_reserved_for_real_synthesis(self):
        self.assertEqual(self.main.PHRASE_WORTH_IT, {"search_brain", "research_web"})


if __name__ == "__main__":
    unittest.main()
