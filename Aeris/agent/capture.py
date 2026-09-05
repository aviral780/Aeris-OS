"""Aeris writing notes, because Aviral has not written any.

Everything else in this program reads a vault: search_brain scores it, the
graph draws it, brief_me and plan_day rank out of it, recall scores memories
against it. On his machine that vault is empty, which makes the rest of Aeris
an engine with no fuel. Reading was never the bottleneck. Capture is.

So this writes notes in the shape the indexer already expects — YAML front
matter, a `type` that becomes the node colour, tags, and [[wikilinks]] that
become edges — which means a captured note is a first-class citizen of the
graph the moment it lands, not a second-class thing bolted on beside it.

Two shapes, because they are genuinely different:

    note    a thing worth its own file. A decision, a client, a project, an
            idea. Titled, linkable, and something the graph can hang edges on.
    log     a line in today's daily note. Not worth a file of its own, still
            worth having said. Appended, never overwriting the day.

Nothing here touches the disk directly. Every write goes out through
actions.propose(), so the write-root confinement, the backup-before-overwrite
and the audit line all apply exactly as they do everywhere else. A note is not
a special case that gets to skip the gate.
"""
import os
import re
from datetime import datetime
from pathlib import Path

from . import actions, data

KINDS = {"note", "idea", "decision", "project", "client", "person", "meeting",
         "task", "research", "log"}


def capture_dir():
    """Where new notes land.

    His first vault root, so captures join the graph he already has. Falls
    back to a folder inside Aeris when he has not pointed at a vault yet, so
    capture works on day one rather than refusing until he configures it.
    """
    configured = data.env("AERIS_CAPTURE_DIR", "").strip()
    if configured:
        return Path(os.path.expanduser(configured)).resolve()
    roots = data.vault_roots()
    if roots:
        return roots[0]
    return (Path(__file__).resolve().parent.parent / "notes").resolve()


def _slug(text, limit=60):
    out = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return out[:limit].rstrip("-") or "note"


def _front_matter(kind, tags, links, created):
    lines = ["---", "type: %s" % kind, "created: %s" % created,
             "source: aeris"]
    if tags:
        lines.append("tags: [%s]" % ", ".join(tags))
    if links:
        lines.append("links: [%s]" % ", ".join(links))
    lines.append("---")
    return "\n".join(lines)


def _clean_tags(tags):
    if isinstance(tags, str):
        tags = re.split(r"[,\s]+", tags)
    out = []
    for tag in tags or []:
        tag = re.sub(r"[^a-z0-9/-]", "", str(tag).lower().lstrip("#"))
        if tag and tag not in out:
            out.append(tag)
    return out[:8]


def note(title="", body="", kind="note", tags=None, links=None):
    """One note, one file. Returns the proposal from the gate."""
    title = " ".join(str(title or "").split())
    body = str(body or "").strip()
    if not title and not body:
        return {"status": "done", "action": "capture_note",
                "result": {"ok": False, "summary": "Nothing to write down."}}
    if not title:
        title = body.split(".")[0][:70]

    kind = str(kind or "note").lower().strip()
    if kind not in KINDS:
        kind = "note"
    tags = _clean_tags(tags)
    links = [str(l).strip() for l in (links or []) if str(l).strip()][:12]

    now = datetime.now()
    created = now.isoformat(timespec="seconds")
    directory = capture_dir() / (kind + "s" if kind != "note" else "notes")
    path = directory / ("%s-%s.md" % (now.strftime("%Y-%m-%d"), _slug(title)))

    parts = [_front_matter(kind, tags, links, created), "",
             "# %s" % title, "", body or "_(no detail yet)_"]
    if links:
        parts += ["", "## Links", ""]
        parts += ["- [[%s]]" % l for l in links]
    content = "\n".join(parts) + "\n"

    return actions.propose("write_file", {"path": str(path), "content": content},
                           reason="captured a %s: %s" % (kind, title))


def log(text=""):
    """One line in today's daily note. Appends — a day is never overwritten."""
    text = " ".join(str(text or "").split())
    if not text:
        return {"status": "done", "action": "capture_log",
                "result": {"ok": False, "summary": "Nothing to log."}}

    now = datetime.now()
    day = now.strftime("%Y-%m-%d")
    path = capture_dir() / "daily" / ("%s.md" % day)

    existing = ""
    if path.is_file():
        try:
            existing = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            existing = ""
    if not existing.strip():
        existing = ("---\ntype: daily\ncreated: %s\nsource: aeris\n---\n\n# %s\n\n"
                    % (now.isoformat(timespec="seconds"), now.strftime("%A %d %B %Y")))

    line = "- **%s** %s\n" % (now.strftime("%H:%M"), text)
    return actions.propose("write_file",
                           {"path": str(path), "content": existing.rstrip("\n") + "\n" + line},
                           reason="logged to today: %s" % text[:60])


def status():
    directory = capture_dir()
    existing = len(list(directory.rglob("*.md"))) if directory.is_dir() else 0
    inside, why = actions._inside_write_root(directory)
    return {"dir": str(directory), "notes": existing, "writable": inside,
            "detail": "" if inside else why}
