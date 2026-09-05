"""Choosing which memories matter to the question in front of her.

The old `as_context` pasted the last two dozen memories into every prompt.
That works until it doesn't: at a few hundred memories the prompt is mostly
irrelevant history, the model's attention is spread across it, and the one
line that mattered is competing with fifty that don't. Recency is not
relevance.

So memories are scored against the question instead:

  * BM25 over the words, reusing the vault's own tokeniser and stemmer so a
    memory and a note score the same way. Free, instant, no dependencies, and
    on its own good enough for most turns.
  * Cosine over embeddings when Ollama is running, which catches the turns
    BM25 misses — "what did I decide about charging people" against a memory
    that says "per-case pricing" shares no words at all. Free and local.
    Vectors are cached on disk by content hash, so each memory is embedded
    once and never again.
  * A gentle recency nudge, because a thing he said last week usually does
    beat the same thing said last year. Gentle: it breaks ties, it does not
    decide the ranking.

When Ollama is absent every one of those degrades to the lexical score alone
and nothing announces itself as cleverer than it is.
"""
import hashlib
import json
import math
import re
import urllib.error
import urllib.request
from datetime import datetime

from . import data, memory
from .vault import tokenize

CACHE_DIR = memory.ROOT / ".cache"
VECTOR_CACHE = CACHE_DIR / "embeddings.json"

K1, B = 1.4, 0.72
EMBED_TIMEOUT = 20

# How much each signal is worth once both are available.
W_LEXICAL, W_SEMANTIC = 0.65, 0.35

_vectors = None


# --------------------------------------------------------------------------
# embeddings — free, local, optional
# --------------------------------------------------------------------------

def embed_model():
    return data.env("OLLAMA_EMBED_MODEL", "nomic-embed-text").strip()


def _load_cache():
    global _vectors
    if _vectors is not None:
        return _vectors
    _vectors = {}
    if VECTOR_CACHE.is_file():
        try:
            _vectors = json.loads(VECTOR_CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _vectors = {}
    return _vectors


def _save_cache():
    if _vectors is None:
        return
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        VECTOR_CACHE.write_text(json.dumps(_vectors), encoding="utf-8")
    except OSError:
        pass                                   # a cache that cannot save still works


def _digest(text):
    return hashlib.sha256(("%s|%s" % (embed_model(), text)).encode("utf-8")).hexdigest()[:32]


def _embed(text):
    """One vector, or None when Ollama is not there. Never raises."""
    text = " ".join((text or "").split())[:4000]
    if not text:
        return None
    cache = _load_cache()
    key = _digest(text)
    if key in cache:
        return cache[key]

    url = data.env("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    payload = json.dumps({"model": embed_model(), "prompt": text}).encode("utf-8")
    req = urllib.request.Request(url + "/api/embeddings", data=payload, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT) as resp:
            vec = json.loads(resp.read().decode("utf-8")).get("embedding")
    except Exception:                                              # noqa: BLE001
        return None
    if not vec:
        return None
    cache[key] = vec
    return vec


def semantic_available():
    return _embed("ping") is not None


def _cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

def _blob(item):
    return "%s %s" % (item.get("title", ""), item.get("text", ""))


def _age_days(item):
    stamp = (item.get("created") or "")[:10]
    try:
        return max(0.0, (datetime.now() - datetime.fromisoformat(stamp)).days)
    except ValueError:
        return 400.0


def _bm25(items, terms):
    """Standard BM25 over the memory store. Returns {index: score}."""
    docs = [tokenize(_blob(i)) for i in items]
    lengths = [len(d) or 1 for d in docs]
    avg = sum(lengths) / len(lengths)
    n = len(docs)

    df = {}
    for doc in docs:
        for term in set(doc):
            df[term] = df.get(term, 0) + 1

    scores = {}
    for idx, doc in enumerate(docs):
        score = 0.0
        for term in set(terms):
            freq = doc.count(term)
            if not freq:
                continue
            idf = math.log(1 + (n - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
            norm = freq * (K1 + 1) / (freq + K1 * (1 - B + B * lengths[idx] / avg))
            score += idf * norm
        if score:
            scores[idx] = score
    return scores


def search(query, limit=8, kinds=None, use_semantic=True):
    """Memories that bear on this question, best first."""
    items = memory.everything()
    if kinds:
        items = [i for i in items if i.get("kind") in kinds]
    if not items:
        return []

    query = (query or "").strip()
    if not query:
        items.sort(key=_age_days)
        return [dict(i, score=0.0, why="most recent") for i in items[:limit]]

    terms = tokenize(query)
    lexical = _bm25(items, terms) if terms else {}
    top_lex = max(lexical.values()) if lexical else 0.0

    semantic = {}
    if use_semantic:
        q_vec = _embed(query)
        if q_vec:
            for idx, item in enumerate(items):
                vec = _embed(_blob(item))
                if vec:
                    semantic[idx] = _cosine(q_vec, vec)
            _save_cache()

    scored = []
    for idx, item in enumerate(items):
        lex = (lexical.get(idx, 0.0) / top_lex) if top_lex else 0.0
        sem = semantic.get(idx, 0.0)
        if semantic:
            base = W_LEXICAL * lex + W_SEMANTIC * sem
            why = "words %.2f, meaning %.2f" % (lex, sem)
        else:
            base = lex
            why = "words %.2f" % lex
        if base <= 0.001:
            continue
        # Breaks ties towards the recent. Never large enough to outrank a
        # genuinely better match.
        base *= 1.0 + max(0.0, 0.12 - _age_days(item) / 3000.0)
        scored.append(dict(item, score=base, why=why))

    scored.sort(key=lambda i: -i["score"])
    if scored:
        return scored[:limit]

    # Nothing matched. Without Ollama this is common — "how should I charge
    # clients" shares no word with "prefers per-case pricing", and only an
    # embedding closes that gap. Falling back to the most recent keeps her at
    # least as good as she was before retrieval existed, rather than handing
    # the model no memory at all.
    items.sort(key=_age_days)
    return [dict(i, score=0.0, why="no match — most recent") for i in items[:limit]]


def context_for(question="", limit=24):
    """The memory block for the system prompt.

    With a question, the memories that bear on it. Without one — the opening
    of a session, say — the most recent, which is the best guess available.
    """
    hits = search(question, limit=limit)
    if not hits:
        return ""
    lines = []
    for hit in hits:
        when = (hit.get("created") or "")[:10]
        body = hit.get("text") or hit.get("title") or ""
        if hit.get("kind") == "episode":
            lines.append("- (%s, past conversation) %s: %s"
                         % (when, hit.get("title", ""), body[:400]))
        else:
            lines.append("- (%s) %s" % (when, body[:400]))
    return "\n".join(lines)


def stats():
    return {
        "facts": memory.count(),
        "episodes": memory.episode_count(),
        "dir": str(memory.memory_dir()),
        "semantic": bool(_load_cache()) or None,
        "embed_model": embed_model(),
    }
