"""A standing snapshot of his real work, always in the model's system prompt.

Without this, the model only ever learns about a repo, a deploy or a Notion
page when a tool call happens to run — and a tool call only happens if the
question is phrased in a way the router (or the model's own judgement)
recognises as needing one. Aviral's ask was direct: he does not want to have
to phrase a question exactly right before Aeris "knows" his repos, his
deploys, his Notion pages. He wants her to already know, the way a person who
sat down and read everything would.

This builds a compact snapshot from the connectors already wired up
(read-only, same as everywhere else), caches it for a few minutes, and hands
it to system_prompt() so ordinary conversation starts from the real state of
his projects instead of from nothing. The live tools still exist and still
give the detailed, current answer — this is only what she starts already
knowing, so a plain "what am I working on" doesn't depend on a regex match.
"""
import time

from . import github, notion, railway

TTL = 600  # seconds. Long enough not to hit three APIs every single turn,
           # short enough that "what's failing" is never stale by more than
           # ten minutes.

NOTION_TOPICS = ["job tracker", "skills", "projects", "future os"]

_cache = {"text": "", "at": 0.0}


def _github_lines():
    if not github.configured():
        return []
    out, err = github.overview()
    if err or not out:
        return []
    lines = ["## GitHub — %d repo%s, last %d days"
             % (out["repos"], "" if out["repos"] == 1 else "s", out["days"])]
    for row in out["failing"][:5]:
        lines.append("- FAILING: %s — %s" % (row["repo"], row["why"]))
    for row in out["waiting"][:5]:
        lines.append("- open PR #%d on %s: %s" % (row["number"], row["repo"], row["title"]))
    for row in out["active"][:8]:
        lines.append("- %s: %d commit(s), latest \"%s\""
                     % (row["repo"], row["commits"], row["latest"]))
    if len(lines) == 1:
        lines.append("- nothing failing, nothing open, nothing committed in the window")
    return lines


def _railway_lines():
    if not railway.configured():
        return []
    out, err = railway.overview()
    if err or not out:
        return []
    lines = ["## Railway — %d project(s)" % out["projects"]]
    for row in out["broken"][:5]:
        lines.append("- BROKEN: %s / %s — %s" % (row["project"], row["service"], row["status"]))
    for row in out["live"][:8]:
        lines.append("- live: %s / %s" % (row["project"], row["service"]))
    if out["problems"]:
        lines.append("- couldn't read: %s" % "; ".join(out["problems"][:3]))
    if len(lines) == 1:
        lines.append("- nothing deployed yet")
    return lines


def _notion_lines():
    if not notion.configured():
        return []
    lines = ["## Notion"]
    seen = set()
    for topic in NOTION_TOPICS:
        pages, err = notion.search(topic, limit=2)
        if err or not pages:
            continue
        for p in pages:
            if p["id"] in seen:
                continue
            seen.add(p["id"])
            lines.append("- %s (%s, edited %s)" % (p["title"], p["kind"], p["edited"]))
    if len(lines) == 1:
        return []  # nothing shared with the integration yet — say nothing rather than a bare header
    return lines


def build():
    """One fresh snapshot. A connector that errors or isn't configured
    contributes nothing rather than breaking the other two."""
    sections = []
    for fn in (_github_lines, _railway_lines, _notion_lines):
        try:
            lines = fn()
        except Exception:                                       # noqa: BLE001
            lines = []
        if lines:
            sections.append("\n".join(lines))
    return "\n\n".join(sections)


def summary(refresh=False):
    """The cached snapshot text, rebuilt at most once per TTL.

    Never raises. A connector timing out must not take the system prompt
    down with it — worst case this hands back what it built last time, or
    an empty string on the very first call.
    """
    now = time.time()
    if refresh or now - _cache["at"] > TTL:
        try:
            _cache["text"] = build()
        except Exception:                                       # noqa: BLE001
            pass
        _cache["at"] = now
    return _cache["text"]
