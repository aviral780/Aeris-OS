"""GitHub, read-mostly, entirely server-side.

Aviral has no notes, so his repositories are the closest thing to a written
record of what he has actually been doing. That makes this the connector worth
building first: "what is broken, what is open, what did I abandon" is a
question his own git history can answer today without him writing a word.

Everything here is free. The API costs nothing on a personal account, and a
fine-grained token with read access is a two-minute job — no OAuth app, no
consent screen, no Google Cloud project.

The split follows the same rule as everything else in Aeris. Reading is local
and immediate. Writing to GitHub is *outward* — an issue he did not mean to
file is visible to other people and cannot be quietly undone — so every write
is registered as a CONFIRM capability and stops for him. The token lives in
Aeris/.env, is read only here, and never crosses into the browser: the page
gets titles and numbers, never credentials.

Responses are cached briefly. A brief across ten repositories is thirty-odd
calls, and pressing the button twice should not spend sixty of them.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import data

API = "https://api.github.com"
TIMEOUT = 20
CACHE_SECONDS = 180
STALE_DAYS = 21

_cache = {}


def _token():
    return data.env("GITHUB_TOKEN", "").strip()


def configured():
    return bool(_token())


def _get(path, params=None, use_cache=True):
    """One GET. Returns (parsed, error). Never raises."""
    if not configured():
        return None, ("No GITHUB_TOKEN in Aeris/.env. Create a token at "
                      "github.com/settings/tokens with repo read access.")
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)

    now = time.time()
    if use_cache:
        hit = _cache.get(url)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1], None

    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer %s" % _token(),
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Aeris/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            parsed = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        friendly = {
            401: "GitHub rejected the token. Check GITHUB_TOKEN in Aeris/.env.",
            403: "GitHub refused that — either the rate limit is spent or the token "
                 "lacks permission for this repository.",
            404: "GitHub has no such path, or the token cannot see it.",
        }.get(exc.code, "GitHub returned HTTP %d." % exc.code)
        return None, friendly
    except (urllib.error.URLError, OSError) as exc:
        return None, "Could not reach GitHub (%s)." % (getattr(exc, "reason", None) or exc)
    except ValueError:
        return None, "GitHub returned something that was not JSON."

    if use_cache:
        _cache[url] = (now, parsed)
    return parsed, None


def _post(path, payload):
    """One POST. Only ever reached through the confirm gate."""
    if not configured():
        return None, "No GITHUB_TOKEN in Aeris/.env."
    req = urllib.request.Request(
        API + path, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Authorization": "Bearer %s" % _token(),
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "User-Agent": "Aeris/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("message", "")
        except Exception:                                          # noqa: BLE001
            pass
        return None, "GitHub returned HTTP %d. %s" % (exc.code, detail)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, "Could not reach GitHub (%s)." % exc


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def _age_days(stamp):
    if not stamp:
        return 999
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return 999
    return (datetime.now(timezone.utc) - when).days


def whoami():
    user, err = _get("/user")
    if err:
        return None, err
    return {"login": user.get("login", ""), "name": user.get("name", ""),
            "repos": user.get("public_repos", 0)}, None


def _named_repos():
    """Repositories he listed explicitly in .env."""
    raw = data.env("GITHUB_REPOS", "")
    return [c.strip() for c in raw.replace(",", " ").split() if "/" in c]


def repos(limit=12):
    """His repositories, most recently pushed first.

    /user/repos needs a token with account-wide scope. A fine-grained token
    limited to selected repositories gets a 403 there — which is a sensible
    token to have, not a misconfiguration — so GITHUB_REPOS names them
    directly and each one is fetched on its own.
    """
    named = _named_repos()
    got, err = (None, None)
    if not named:
        got, err = _get("/user/repos",
                        {"sort": "pushed", "per_page": max(1, min(100, limit)),
                         "affiliation": "owner,collaborator"})
        if err:
            return None, ("%s Either widen the token, or list your repositories as "
                          "GITHUB_REPOS=owner/name,owner/other in Aeris/.env." % err)
    if named:
        got = []
        problems = []
        for full_name in named[:limit]:
            one, one_err = _get("/repos/%s" % full_name)
            if one_err:
                problems.append("%s (%s)" % (full_name, one_err))
                continue
            got.append(one)
        if not got:
            return None, "None of the repositories in GITHUB_REPOS could be read: %s" \
                         % "; ".join(problems)

    out = []
    for r in got:
        out.append({
            "full_name": r.get("full_name", ""),
            "name": r.get("name", ""),
            "owner": (r.get("owner") or {}).get("login", ""),
            "private": r.get("private", False),
            "description": r.get("description") or "",
            "language": r.get("language") or "",
            "pushed_at": r.get("pushed_at", ""),
            "age_days": _age_days(r.get("pushed_at")),
            "open_issues": r.get("open_issues_count", 0),
            "url": r.get("html_url", ""),
            "default_branch": r.get("default_branch", "main"),
        })
    return out, None


def pull_requests(full_name):
    got, err = _get("/repos/%s/pulls" % full_name, {"state": "open", "per_page": 20})
    if err:
        return [], err
    return [{"number": p.get("number"), "title": p.get("title", ""),
             "author": (p.get("user") or {}).get("login", ""),
             "draft": p.get("draft", False),
             "age_days": _age_days(p.get("created_at")),
             "url": p.get("html_url", ""),
             "branch": (p.get("head") or {}).get("ref", "")} for p in got], None


def issues(full_name):
    """Issues only. The endpoint also returns pull requests; they are not issues."""
    got, err = _get("/repos/%s/issues" % full_name, {"state": "open", "per_page": 20})
    if err:
        return [], err
    return [{"number": i.get("number"), "title": i.get("title", ""),
             "age_days": _age_days(i.get("created_at")),
             "labels": [l.get("name", "") for l in (i.get("labels") or [])],
             "url": i.get("html_url", "")}
            for i in got if not i.get("pull_request")], None


def ci_status(full_name, branch=""):
    """The most recent workflow run, and whether it went red."""
    params = {"per_page": 5}
    if branch:
        params["branch"] = branch
    got, err = _get("/repos/%s/actions/runs" % full_name, params)
    if err:
        return None, err
    runs = (got or {}).get("workflow_runs") or []
    if not runs:
        return None, None                      # no CI is not a failure
    latest = runs[0]
    return {"name": latest.get("name", ""),
            "status": latest.get("status", ""),
            "conclusion": latest.get("conclusion", ""),
            "failed": latest.get("conclusion") in ("failure", "timed_out", "startup_failure"),
            "branch": latest.get("head_branch", ""),
            "age_days": _age_days(latest.get("created_at")),
            "url": latest.get("html_url", "")}, None


def recent_commits(full_name, days=7, author=""):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    params = {"since": since, "per_page": 30}
    if author:
        params["author"] = author
    got, err = _get("/repos/%s/commits" % full_name, params)
    if err:
        return [], err
    out = []
    for c in got or []:
        message = ((c.get("commit") or {}).get("message") or "").split("\n")[0]
        out.append({"sha": (c.get("sha") or "")[:7], "message": message,
                    "when": ((c.get("commit") or {}).get("author") or {}).get("date", ""),
                    "url": c.get("html_url", "")})
    return out, None


# --------------------------------------------------------------------------
# the brief
# --------------------------------------------------------------------------

def overview(limit=8, days=7):
    """What is broken, what is waiting, what went quiet.

    Ordered by what actually needs him: red CI first, because a broken build
    blocks everything behind it; then pull requests, which are work already
    done and not landed; then repositories that have gone quiet with issues
    still open, which is the shape of an abandoned project.
    """
    me, err = whoami()
    if err:
        return None, err
    mine, err = repos(limit)
    if err:
        return None, err

    failing, waiting, stale, active = [], [], [], []
    for repo in mine:
        name = repo["full_name"]

        ci, _ = ci_status(name, repo["default_branch"])
        if ci and ci["failed"]:
            failing.append({"repo": name, "why": "%s failed on %s"
                            % (ci["name"] or "CI", ci["branch"]),
                            "age_days": ci["age_days"], "url": ci["url"]})

        prs, _ = pull_requests(name)
        for pr in prs:
            waiting.append({"repo": name, "number": pr["number"], "title": pr["title"],
                            "age_days": pr["age_days"], "draft": pr["draft"],
                            "url": pr["url"]})

        if repo["age_days"] > STALE_DAYS and repo["open_issues"]:
            stale.append({"repo": name, "age_days": repo["age_days"],
                          "open": repo["open_issues"], "url": repo["url"]})

        # Deliberately not filtered by author. GitHub matches `author` against
        # the commit's identity, which is whatever git was configured with —
        # a different laptop, a work email, or a co-authored commit all drop
        # out, and the filter then reports zero commits on a repository that
        # was pushed to an hour ago. Silently answering "you did nothing this
        # week" is worse than counting a collaborator's commit.
        commits, _ = recent_commits(name, days=days)
        if commits:
            active.append({"repo": name, "commits": len(commits),
                           "latest": commits[0]["message"][:90]})

    failing.sort(key=lambda x: x["age_days"])
    waiting.sort(key=lambda x: -x["age_days"])
    stale.sort(key=lambda x: -x["age_days"])
    active.sort(key=lambda x: -x["commits"])

    return {"me": me, "repos": len(mine), "days": days,
            "failing": failing, "waiting": waiting, "stale": stale,
            "active": active}, None


# --------------------------------------------------------------------------
# writing — outward, so it never happens without him
# --------------------------------------------------------------------------

def create_issue(repo="", title="", body="", **_):
    if not repo or not title:
        return {"ok": False, "summary": "Need a repository and a title."}
    got, err = _post("/repos/%s/issues" % repo, {"title": title, "body": body or ""})
    if err:
        return {"ok": False, "summary": err}
    return {"ok": True, "summary": "Opened %s#%d — %s" % (repo, got.get("number", 0), title),
            "url": got.get("html_url", ""), "number": got.get("number")}


def comment(repo="", number=0, body="", **_):
    if not repo or not number or not body:
        return {"ok": False, "summary": "Need a repository, a number and something to say."}
    got, err = _post("/repos/%s/issues/%s/comments" % (repo, number), {"body": body})
    if err:
        return {"ok": False, "summary": err}
    return {"ok": True, "summary": "Commented on %s#%s." % (repo, number),
            "url": got.get("html_url", "")}


def status():
    if not configured():
        return {"ok": False, "detail": "No GITHUB_TOKEN set. github.com/settings/tokens, "
                                       "read access to your repositories."}
    me, err = whoami()
    if err:
        return {"ok": False, "detail": err}
    return {"ok": True, "login": me["login"], "free": True,
            "detail": "Signed in as %s. Reads are free; anything that writes to GitHub "
                      "stops and asks." % me["login"]}
