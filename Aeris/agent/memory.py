"""What Aeris keeps, as plain markdown and nothing cleverer.

Two kinds of thing live here:

    memory/*.md           facts. One fact, one dated file, written only when
                          Aviral asks or says something that will still matter
                          in three months.
    memory/episodes/*.md  episodes. What a past conversation was actually
                          about, distilled to a few lines. Written by the
                          importer, not by hand.

The store is deliberately dumb. Every file is markdown with YAML front matter,
so Obsidian reads it natively, wikilinks between memories and his real notes
work, and if Aeris is deleted tomorrow the memory is still a folder of notes he
can open. Retrieval is the clever part and it lives in recall.py — nothing in
this file scores, ranks or decides.

Set AERIS_MEMORY_DIR to put all of it inside an Obsidian vault, so her memory
and his notes are one graph.

Every path is resolved and checked against the memory directory before
anything is opened, so a crafted title cannot walk out of the folder.
"""
import re
from datetime import datetime
from pathlib import Path

from . import data

ROOT = Path(__file__).resolve().parent.parent

MAX_FACT = 600


def memory_dir():
    """Where memory lives. Inside Aeris unless he points it at his vault."""
    configured = data.env("AERIS_MEMORY_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (ROOT / "memory").resolve()


def episodes_dir():
    return memory_dir() / "episodes"


def _slug(text, limit=52):
    out = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (out[:limit].rstrip("-") or "fact")


def _safe(path):
    """Refuse anything that resolves outside the memory directory."""
    base = memory_dir()
    resolved = Path(path).resolve()
    if resolved != base and base not in resolved.parents:
        raise ValueError("memory.py refused a write outside %s: %s" % (base, resolved))
    return resolved


def _unique(directory, base, suffix=".md"):
    path = _safe(directory / (base + suffix))
    n = 2
    while path.exists():
        path = _safe(directory / ("%s-%d%s" % (base, n, suffix)))
        n += 1
    return path


def write(fact, source="asked", tags=None):
    """Persist one fact. Returns a dict including `spoken_receipt`."""
    fact = " ".join(str(fact).split())
    if not fact:
        raise ValueError("nothing to remember")
    truncated = len(fact) > MAX_FACT
    if truncated:
        fact = fact[:MAX_FACT].rsplit(" ", 1)[0] + "…"

    base_dir = memory_dir()
    base_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d")
    path = _unique(base_dir, "%s-%s" % (stamp, _slug(fact)))

    tags = tags or []
    body = (
        "---\n"
        "type: memory\n"
        "created: %s\n"
        "source: %s\n"
        "tags: [%s]\n"
        "---\n\n"
        "# Remembered %s\n\n"
        "%s\n"
    ) % (now.isoformat(timespec="seconds"), source, ", ".join(tags), stamp, fact)

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)

    return {
        "file": path.name,
        "path": "memory/" + path.name,
        "fact": fact,
        "created": now.isoformat(timespec="seconds"),
        "source": source,
        "truncated": truncated,
        # Said out loud, every time. No silent writes.
        "spoken_receipt": "I wrote to memory slash %s: %s" % (path.name, fact),
    }


def write_episode(title, summary, when="", source="import", key="", tags=None):
    """One past conversation, distilled. `key` makes the import idempotent:
    the same conversation re-imported overwrites rather than duplicates."""
    summary = " ".join(str(summary).split())
    if not summary:
        raise ValueError("nothing to record")
    directory = episodes_dir()
    directory.mkdir(parents=True, exist_ok=True)

    stamp = (when or datetime.now().isoformat(timespec="seconds"))[:10]
    base = "%s-%s" % (stamp, _slug(title or summary, 60))
    path = _safe(directory / (base + ".md"))
    if key:
        existing = _by_key(key)
        if existing:
            path = existing
    elif path.exists():
        path = _unique(directory, base)

    body = (
        "---\n"
        "type: episode\n"
        "created: %s\n"
        "source: %s\n"
        "key: %s\n"
        "tags: [%s]\n"
        "---\n\n"
        "# %s\n\n"
        "%s\n"
    ) % (when or datetime.now().isoformat(timespec="seconds"), source, key,
         ", ".join(tags or []), title or stamp, summary)

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return {"file": path.name, "path": str(path), "title": title, "summary": summary}


def _by_key(key):
    directory = episodes_dir()
    if not directory.is_dir():
        return None
    for path in directory.glob("*.md"):
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:400]
        except OSError:
            continue
        if re.search(r"^key:\s*%s\s*$" % re.escape(key), head, re.M):
            return path
    return None


# --------------------------------------------------------------------------
# reading it back
# --------------------------------------------------------------------------

def _parse(path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    meta = {}
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, re.S)
    body = text[m.end():] if m else text
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
    lines = [l.strip() for l in body.splitlines() if l.strip()]
    title = ""
    content = []
    for line in lines:
        if line.startswith("#") and not title:
            title = line.lstrip("# ").strip()
            continue
        content.append(line)
    return {
        "file": path.name,
        "path": str(path),
        "kind": meta.get("type", "memory"),
        "created": meta.get("created", ""),
        "source": meta.get("source", ""),
        "key": meta.get("key", ""),
        "title": title,
        "text": " ".join(content),
    }


def all_memories(limit=200):
    """Facts only, newest first — what the memory card in the UI shows."""
    directory = memory_dir()
    if not directory.is_dir():
        return []
    out = []
    for path in sorted(directory.glob("*.md"), reverse=True)[:limit]:
        parsed = _parse(path)
        if not parsed:
            continue
        out.append({"file": parsed["file"], "path": "memory/" + parsed["file"],
                    "fact": parsed["text"] or parsed["title"],
                    "created": parsed["created"]})
    return out


def all_episodes(limit=2000):
    directory = episodes_dir()
    if not directory.is_dir():
        return []
    out = []
    for path in sorted(directory.glob("*.md"), reverse=True)[:limit]:
        parsed = _parse(path)
        if parsed:
            out.append(parsed)
    return out


def everything():
    """Facts and episodes together, as recall.py wants them."""
    items = []
    directory = memory_dir()
    if directory.is_dir():
        for path in sorted(directory.glob("*.md")):
            parsed = _parse(path)
            if parsed:
                parsed["kind"] = "fact"
                items.append(parsed)
    for parsed in all_episodes():
        parsed["kind"] = "episode"
        items.append(parsed)
    return items


def count():
    directory = memory_dir()
    return len(list(directory.glob("*.md"))) if directory.is_dir() else 0


def episode_count():
    directory = episodes_dir()
    return len(list(directory.glob("*.md"))) if directory.is_dir() else 0


def as_context(limit=24, question=""):
    """The lines fed into the system prompt.

    Retrieval when there is a question to retrieve against, most-recent when
    there is not. Pasting the whole store in stopped being viable somewhere
    around fifty memories.
    """
    from . import recall
    return recall.context_for(question, limit=limit)
