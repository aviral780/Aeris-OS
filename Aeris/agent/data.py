"""THE ONLY FILE THAT TOUCHES AVIRAL'S REAL DATA.

Two jobs, and nothing else:

  1. Decide whether we are looking at invented fixtures or the real folders.
     `AERIS_DEMO` is read here and nowhere else in the codebase. It defaults
     to demo, so the real folders are something you opt *in* to.

  2. Open files read-only and hand plain records to the rest of the program.

Nothing in here writes, moves, renames or deletes anything under a vault
root — every read goes through `_read_bytes`, which opens "rb" and nothing
else. Nothing anywhere else in Aeris writes under a vault root either. The
only two writers in the project are memory.py, which writes `memory/`, and
voice.py, which rewrites the single ELEVENLABS_VOICE_ID line of Aeris/.env
when you pick a voice in the UI.
"""
import json
import os
import re
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MAX_BYTES = 2 * 1024 * 1024          # skip anything over 2 MB
TEXT_SUFFIXES = {".md", ".markdown", ".mdx", ".txt", ".text", ".rst"}
PDF_SUFFIXES = {".pdf"}
SKIP_DIRS = {
    "node_modules", ".git", ".svn", ".hg", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".cache", ".obsidian", ".trash", ".DS_Store",
    "site-packages", ".pytest_cache", ".mypy_cache", "target",
}

# --------------------------------------------------------------------------
# .env
# --------------------------------------------------------------------------

_ENV_CACHE = None


def env(key, default=""):
    """Read from Aeris/.env, falling back to the process environment."""
    global _ENV_CACHE
    if _ENV_CACHE is None:
        _ENV_CACHE = {}
        path = ROOT / ".env"
        if path.exists():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                _ENV_CACHE[k.strip()] = v.strip().strip('"').strip("'")
    if key in os.environ and os.environ[key] != "":
        return os.environ[key]
    return _ENV_CACHE.get(key, default)


def reload_env():
    global _ENV_CACHE
    _ENV_CACHE = None


# --------------------------------------------------------------------------
# the demo switch — read here, once, and nowhere else
# --------------------------------------------------------------------------

def is_demo():
    """True unless Aviral has explicitly opted into his real life."""
    return env("AERIS_DEMO", "1").strip() not in ("0", "false", "no", "off")


def mode_label():
    return "demo" if is_demo() else "live"


def vault_roots():
    """Folders to index. Demo mode never looks outside data/demo."""
    if is_demo():
        return [ROOT / "data" / "demo"]
    raw = env("AERIS_VAULT_ROOTS", "")
    roots = []
    for chunk in raw.split(":"):
        chunk = chunk.strip()
        if not chunk:
            continue
        p = Path(os.path.expanduser(chunk)).resolve()
        if p.is_dir():
            roots.append(p)
    return roots


def roots_report():
    """What we were asked to index vs what actually exists. Degrade loudly."""
    if is_demo():
        d = ROOT / "data" / "demo"
        return {"mode": "demo", "ok": [str(d)] if d.is_dir() else [], "missing": []}
    asked = [c.strip() for c in env("AERIS_VAULT_ROOTS", "").split(":") if c.strip()]
    ok, missing = [], []
    for chunk in asked:
        p = Path(os.path.expanduser(chunk))
        (ok if p.is_dir() else missing).append(str(p))
    return {"mode": "live", "ok": ok, "missing": missing}


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def _read_bytes(path):
    """The only way this program reads a vault file. Binary, read-only."""
    with open(path, "rb") as fh:          # noqa: SIM115 - explicit on purpose
        return fh.read(MAX_BYTES + 1)


_PDF_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.S)
_PDF_TEXT_OPS = re.compile(rb"\((?:\\.|[^\\()])*\)")


def _pdf_text(raw):
    """Best-effort PDF text with the standard library only.

    Inflates each content stream and pulls the literal strings out of the
    text-showing operators. That covers most PDFs written by word processors
    and export tools. It does not cover PDFs whose fonts use a custom CID
    encoding, and it never guesses: when it gets little or nothing back it
    says `partial` or `none` and the UI shows that on the note.
    """
    chunks = []
    for match in _PDF_STREAM.finditer(raw):
        blob = match.group(1)
        try:
            blob = zlib.decompress(blob)
        except zlib.error:
            try:
                blob = zlib.decompressobj().decompress(blob)
            except zlib.error:
                continue
        if b"TJ" not in blob and b"Tj" not in blob:
            continue
        for lit in _PDF_TEXT_OPS.finditer(blob):
            s = lit.group(0)[1:-1]
            s = (s.replace(rb"\(", b"(").replace(rb"\)", b")")
                  .replace(rb"\\", b"\\").replace(rb"\n", b"\n")
                  .replace(rb"\r", b" ").replace(rb"\t", b" "))
            chunks.append(s.decode("latin-1", errors="replace"))
        chunks.append("\n")
    text = "".join(chunks)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) >= 200:
        return text, "ok"
    if text:
        return text, "partial"
    return "", "none"


_FM = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def _front_matter(text):
    m = _FM.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if v.startswith("[") and v.endswith("]"):
            meta[k] = [x.strip() for x in v[1:-1].split(",") if x.strip()]
        else:
            meta[k] = v
    return meta, text[m.end():]


@dataclass
class Doc:
    id: str
    title: str
    kind: str
    tags: list
    text: str
    display_path: str        # relative to its root — never the absolute path
    root: str
    size: int
    modified: str
    created: str
    extract: str = "ok"      # ok | partial | none
    meta: dict = field(default_factory=dict)
    source: str = "demo"


def _kind_of(meta, rel):
    if meta.get("type"):
        return str(meta["type"]).strip().lower()
    parts = rel.parts
    if len(parts) > 1:
        folder = parts[0].lower().rstrip("s")
        known = {"project", "client", "note", "research", "model", "dataset",
                 "meeting", "idea", "task", "invoice", "paper", "archive",
                 "journal", "daily", "people", "person"}
        if folder in known:
            return folder
        return parts[0].lower()
    return "note"


def _title_of(meta, text, rel):
    if meta.get("title"):
        return str(meta["title"]).strip()
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
        if line:
            break
    return rel.stem.replace("-", " ").replace("_", " ")


def iter_documents():
    """Yield every indexable document. Read-only, always."""
    demo = is_demo()
    seen = set()
    for root in vault_roots():
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            suffix = path.suffix.lower()
            if suffix not in TEXT_SUFFIXES and suffix not in PDF_SUFFIXES:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size > MAX_BYTES:
                continue
            try:
                raw = _read_bytes(path)
            except OSError:
                continue
            if len(raw) > MAX_BYTES:
                continue

            extract = "ok"
            if suffix in PDF_SUFFIXES:
                text, extract = _pdf_text(raw)
            else:
                text = raw.decode("utf-8", errors="replace")

            meta, body = _front_matter(text)
            rel = path.relative_to(root)
            doc_id = str(rel.with_suffix("")).replace(os.sep, "/")
            if doc_id in seen:
                doc_id = "%s~%d" % (doc_id, len(seen))
            seen.add(doc_id)

            tags = meta.get("tags") or []
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]
            tags += re.findall(r"(?<!\w)#([a-zA-Z][\w/-]{1,30})", body)

            yield Doc(
                id=doc_id,
                title=_title_of(meta, body, rel),
                kind=_kind_of(meta, rel),
                tags=sorted(set(t.lower() for t in tags))[:12],
                text=body,
                display_path=str(rel),
                root=root.name if demo else str(root),
                size=stat.st_size,
                modified=datetime.fromtimestamp(stat.st_mtime).date().isoformat(),
                created=str(meta.get("created") or
                            datetime.fromtimestamp(stat.st_mtime).date().isoformat()),
                extract=extract,
                meta={k: v for k, v in meta.items() if k not in ("title", "type", "tags")},
                source="demo" if demo else "vault",
            )


# --------------------------------------------------------------------------
# inbox + calendar
# --------------------------------------------------------------------------

def _load_json(name):
    path = ROOT / "data" / "demo" / name
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def inbox():
    """(messages, note). Real mail in live mode, fixtures in demo.

    Imported inside the function on purpose: google.py reads its settings from
    this module, so importing it at the top would be a cycle.
    """
    if is_demo():
        return _load_json("inbox.json") or [], ""
    from . import google
    if not google.configured() or not google.authorized():
        return [], ("No mail account is connected. %s" % google.status()["detail"])
    messages, err = google.messages(limit=10)
    if err:
        return [], "Gmail could not be read: %s" % err
    return messages, ""


def calendar():
    if is_demo():
        return _load_json("calendar.json") or [], ""
    from . import google
    if not google.configured() or not google.authorized():
        return [], ("No calendar is connected. %s" % google.status()["detail"])
    events, err = google.events(days=7)
    if err:
        return [], "The calendar could not be read: %s" % err
    return events, ""
