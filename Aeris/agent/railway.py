"""Railway: is anything deployed broken right now.

    python3 -m agent.railway

That command is here for a reason. Unlike GitHub, this module was written
without a Railway token to test against — the schema below came from public
documentation and a third-party schema mirror, not from a call that actually
succeeded. So it is built to fail loudly and specifically rather than quietly
and plausibly, and `check()` exists so Aviral can verify it in one command and
send back the exact error if a field name is wrong.

The trap this module is shaped around: GraphQL answers HTTP 200 even when the
query was rubbish, putting the problem in an `errors` array beside a null
`data`. A client that only checks the status code sees a successful response
with nothing in it and reports "no deployments" — which reads exactly like a
healthy account with nothing deployed. That is the same silent-zero that made
the GitHub brief claim a week with no commits, and it is worse here, because
the answer it fakes is "nothing is broken".

Read-only, deliberately. Redeploying or restarting a service is a mutation
that could take down something live, and writing an untested mutation against
a schema I could not verify is not a risk worth taking with his production
account. Once he confirms the read path works, redeploy can follow — through
the confirm gate, like every other outward action.
"""
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import data

API = "https://backboard.railway.com/graphql/v2"
TIMEOUT = 25
CACHE_SECONDS = 120

BROKEN = {"FAILED", "CRASHED"}
IN_FLIGHT = {"BUILDING", "DEPLOYING", "INITIALIZING", "QUEUED", "WAITING"}
LIVE = {"SUCCESS"}

_cache = {}


def _token():
    return data.env("RAILWAY_TOKEN", "").strip()


def configured():
    return bool(_token())


def _is_project_token():
    """Project tokens authenticate with a different header entirely.

    Account, workspace and OAuth tokens use `Authorization: Bearer`. Project
    tokens use `Project-Access-Token`. Sending the wrong one is a 401 that
    looks like a bad token rather than a wrong header, so it is worth being
    explicit about which he has.
    """
    kind = data.env("RAILWAY_TOKEN_TYPE", "auto").strip().lower()
    if kind in ("project", "project-access-token"):
        return True
    if kind in ("account", "workspace", "team", "oauth", "bearer"):
        return False
    return bool(data.env("RAILWAY_PROJECT_ID", "").strip())


def _headers():
    # Python's default urllib User-Agent ("Python-urllib/3.x") is a common bot
    # signature, and Cloudflare in front of backboard.railway.com will answer
    # it with an HTML challenge page instead of the API response — which is
    # exactly what "returned something that was not JSON" turned out to be.
    base = {"Content-Type": "application/json", "User-Agent": "Aeris/1.0"}
    if _is_project_token():
        return dict(base, **{"Project-Access-Token": _token()})
    return dict(base, Authorization="Bearer %s" % _token())


def _query(query, variables=None, cache_key=""):
    """One GraphQL call. Returns (data, error). Never raises."""
    if not configured():
        return None, ("No RAILWAY_TOKEN in Aeris/.env. Create one at "
                      "railway.com/account/tokens.")

    now = time.time()
    if cache_key:
        hit = _cache.get(cache_key)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1], None

    headers = _headers()
    # HTTP headers can only carry latin-1. A token corrupted by copy-paste —
    # this is exactly what happened live: a stray "❯" landed in
    # RAILWAY_TOKEN — crashes several layers down inside http.client with a
    # bare UnicodeEncodeError that has nothing to do with Railway at all, and
    # nothing above catches it as a URLError or OSError. Checked here,
    # up front, so the message names the actual problem instead of a
    # traceback naming a stdlib internal.
    try:
        for value in headers.values():
            value.encode("latin-1")
    except UnicodeEncodeError as exc:
        return None, ("RAILWAY_TOKEN contains a character (%r) that can't be sent in an "
                      "HTTP header — almost certainly picked up by copy-paste. Copy it "
                      "fresh from railway.com/account/tokens, watching for stray "
                      "characters at either end, and re-enter it with "
                      "`python3 -m agent.setup --secrets`." % exc.object[exc.start:exc.end])

    body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(API, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:                                          # noqa: BLE001
            pass
        friendly = {
            401: "Railway rejected the token. If it is a project token, set "
                 "RAILWAY_TOKEN_TYPE=project in Aeris/.env — project tokens use a "
                 "different header from account tokens.",
            403: "Railway refused that — the token cannot see this project.",
        }.get(exc.code, "Railway returned HTTP %d." % exc.code)
        return None, "%s %s" % (friendly, detail)
    except (urllib.error.URLError, OSError) as exc:
        return None, "Could not reach Railway (%s)." % (getattr(exc, "reason", None) or exc)

    try:
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        # The previous version of this line reported "not JSON" and discarded
        # the body that would have said why, which is exactly the kind of
        # unfalsifiable error this project exists to not have. Whatever
        # Railway actually sent — a Cloudflare challenge page, a login
        # redirect, an empty body — is now in the message itself.
        snippet = " ".join(raw.decode("utf-8", errors="replace").split())[:300]
        return None, ("Railway's response wasn't JSON. First 300 characters: %s"
                      % (snippet or "(empty response body)"))

    # The whole point of this module. A GraphQL error arrives with HTTP 200
    # and a null `data`; treating that as an empty result would report a
    # broken query as a healthy account.
    if isinstance(parsed, dict) and parsed.get("errors"):
        messages = []
        for err in parsed["errors"]:
            if isinstance(err, dict) and err.get("message"):
                messages.append(str(err["message"]))
        return None, ("Railway rejected the query: %s"
                      % ("; ".join(messages)[:400] or "no reason given"))

    payload = (parsed or {}).get("data")
    if payload is None:
        return None, "Railway returned no data and no error, which should not happen."
    if cache_key:
        _cache[cache_key] = (now, payload)
    return payload, None


def _edges(node, key):
    """Unwrap Railway's edges/node pagination without assuming it is there."""
    block = (node or {}).get(key) or {}
    return [e.get("node") or {} for e in (block.get("edges") or []) if isinstance(e, dict)]


def _age(stamp):
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    seconds = (datetime.now(timezone.utc) - when).total_seconds()
    if seconds < 3600:
        return "%dm ago" % max(1, seconds // 60)
    if seconds < 86400:
        return "%dh ago" % (seconds // 3600)
    return "%dd ago" % (seconds // 86400)


# --------------------------------------------------------------------------
# queries
# --------------------------------------------------------------------------

ME = "query { me { id name email } }"

PROJECTS = """
query {
  projects(first: 20) {
    edges { node {
      id name description
      services { edges { node { id name } } }
      environments { edges { node { id name } } }
    } }
  }
}
"""

DEPLOYMENTS = """
query Deployments($input: JSON!, $first: Int!) {
  deployments(input: $input, first: $first) {
    edges { node { id status createdAt staticUrl url serviceId environmentId } }
  }
}
"""


def whoami():
    got, err = _query(ME, cache_key="me")
    if err:
        return None, err
    me = (got or {}).get("me") or {}
    return {"name": me.get("name") or me.get("email") or "unknown",
            "email": me.get("email", "")}, None


def projects():
    got, err = _query(PROJECTS, cache_key="projects")
    if err:
        return None, err
    out = []
    for node in _edges(got, "projects"):
        out.append({
            "id": node.get("id", ""),
            "name": node.get("name", ""),
            "services": [{"id": s.get("id"), "name": s.get("name")}
                         for s in _edges(node, "services")],
            "environments": [{"id": e.get("id"), "name": e.get("name")}
                             for e in _edges(node, "environments")],
        })
    return out, None


def deployments(project_id, first=10):
    got, err = _query(DEPLOYMENTS, {"input": {"projectId": project_id}, "first": first},
                      cache_key="dep:%s:%d" % (project_id, first))
    if err:
        return None, err
    out = []
    for node in _edges(got, "deployments"):
        status = (node.get("status") or "").upper()
        out.append({
            "id": node.get("id", ""),
            "status": status,
            "broken": status in BROKEN,
            "in_flight": status in IN_FLIGHT,
            "live": status in LIVE,
            "created": node.get("createdAt", ""),
            "age": _age(node.get("createdAt")),
            "url": node.get("staticUrl") or node.get("url") or "",
            "service_id": node.get("serviceId", ""),
        })
    return out, None


# --------------------------------------------------------------------------
# the brief
# --------------------------------------------------------------------------

def overview(per_project=8):
    """What is broken, what is mid-deploy, what is live."""
    mine, err = projects()
    if err:
        return None, err
    if not mine:
        return {"projects": 0, "broken": [], "in_flight": [], "live": [],
                "problems": []}, None

    broken, in_flight, live, problems = [], [], [], []
    for project in mine:
        names = {s["id"]: s["name"] for s in project["services"]}
        deps, dep_err = deployments(project["id"], first=per_project)
        if dep_err:
            problems.append("%s: %s" % (project["name"], dep_err))
            continue

        # One project's newest deployment per service is the state that
        # matters. An older failure that has since been redeployed over is
        # history, not a problem.
        newest = {}
        for dep in deps:
            key = dep["service_id"] or project["id"]
            if key not in newest:
                newest[key] = dep

        for key, dep in newest.items():
            row = {"project": project["name"], "service": names.get(key, "service"),
                   "status": dep["status"], "age": dep["age"], "url": dep["url"]}
            if dep["broken"]:
                broken.append(row)
            elif dep["in_flight"]:
                in_flight.append(row)
            elif dep["live"]:
                live.append(row)

    return {"projects": len(mine), "broken": broken, "in_flight": in_flight,
            "live": live, "problems": problems}, None


def status():
    if not configured():
        return {"ok": False, "free": True,
                "detail": "No RAILWAY_TOKEN set. railway.com/account/tokens."}
    me, err = whoami()
    if err:
        return {"ok": False, "free": True, "detail": err}
    return {"ok": True, "free": True, "who": me["name"],
            "detail": "Signed in to Railway as %s. Read-only." % me["name"]}


# --------------------------------------------------------------------------

def check():
    """`python3 -m agent.railway` — prove the connection before trusting it."""
    print("Aeris — Railway check")
    if not configured():
        print("  token     MISSING — add RAILWAY_TOKEN to Aeris/.env")
        print("            Get one at railway.com/account/tokens")
        return 1
    token = _token()
    print("  token     set (%s…%s)" % (token[:4], token[-4:]))
    print("  header    %s" % ("Project-Access-Token" if _is_project_token()
                              else "Authorization: Bearer"))

    me, err = whoami()
    if err:
        print("  whoami    \033[91mFAILED\033[0m — %s" % err)
        print("\n  If that names a field, the schema here is wrong. Send me the line.")
        return 1
    print("  account   %s" % me["name"])

    mine, err = projects()
    if err:
        print("  projects  \033[91mFAILED\033[0m — %s" % err)
        print("\n  Send me that error — it names the field the schema got wrong.")
        return 1
    print("  projects  %d" % len(mine))
    for project in mine[:5]:
        print("              %s (%d services)" % (project["name"], len(project["services"])))

    out, err = overview()
    if err:
        print("  overview  \033[91mFAILED\033[0m — %s" % err)
        return 1
    print("  broken    %d" % len(out["broken"]))
    print("  deploying %d" % len(out["in_flight"]))
    print("  live      %d" % len(out["live"]))
    for problem in out["problems"]:
        print("  \033[93mproblem   %s\033[0m" % problem)
    print("\nAll of it worked. Ask her \"is anything broken on Railway\".")
    return 0


if __name__ == "__main__":
    sys.exit(check())
