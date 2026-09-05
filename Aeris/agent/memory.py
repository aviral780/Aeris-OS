"""One remembered fact, one dated markdown file, in memory/ and nowhere else.

This is the only module in Aeris that writes to disk. Every path it touches is
resolved and checked against MEMORY_DIR before anything is opened, so a
crafted title cannot walk out of the folder.

Nothing is ever written silently: `write` returns the exact sentence the voice
has to say, and main.py refuses to return a memory result without it.
"""
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEMORY_DIR = ROOT / "memory"

MAX_FACT = 600


def _slug(text, limit=52):
    out = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (out[:limit].rstrip("-") or "fact")


def _safe(path):
    """Refuse anything that resolves outside memory/."""
    resolved = Path(path).resolve()
    if resolved != MEMORY_DIR and MEMORY_DIR not in resolved.parents:
        raise ValueError("memory.py refused a write outside memory/: %s" % resolved)
    return resolved


def write(fact, source="asked", tags=None):
    """Persist one fact. Returns a dict including `spoken_receipt`."""
    fact = " ".join(str(fact).split())
    if not fact:
        raise ValueError("nothing to remember")
    truncated = len(fact) > MAX_FACT
    if truncated:
        fact = fact[:MAX_FACT].rsplit(" ", 1)[0] + "…"

    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d")
    base = "%s-%s" % (stamp, _slug(fact))
    path = _safe(MEMORY_DIR / (base + ".md"))
    n = 2
    while path.exists():
        path = _safe(MEMORY_DIR / ("%s-%d.md" % (base, n)))
        n += 1

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

    with open(path, "w", encoding="utf-8") as fh:      # the only write in Aeris
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


def all_memories(limit=200):
    if not MEMORY_DIR.is_dir():
        return []
    out = []
    for path in sorted(MEMORY_DIR.glob("*.md"), reverse=True)[:limit]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fact = ""
        for line in reversed(text.strip().splitlines()):
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("---") and ":" not in line[:12]:
                fact = line
                break
        created = ""
        m = re.search(r"^created:\s*(.+)$", text, re.M)
        if m:
            created = m.group(1).strip()
        out.append({"file": path.name, "path": "memory/" + path.name,
                    "fact": fact or text.strip()[:200], "created": created})
    return out


def as_context(limit=24):
    """The lines fed back into the system prompt each session."""
    mems = all_memories(limit)
    if not mems:
        return ""
    return "\n".join("- (%s) %s" % (m["created"][:10], m["fact"]) for m in mems)


def count():
    return len(list(MEMORY_DIR.glob("*.md"))) if MEMORY_DIR.is_dir() else 0
