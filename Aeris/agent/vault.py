"""Folders -> a searchable graph.

This module never opens a file. It takes the plain records data.py hands it
and builds three things:

  * an inverted index with BM25 scoring, for search_brain
  * a node/edge graph from [[wikilinks]], for the canvas
  * a BFS shortest path, for shift-click

Keeping it filesystem-free is what makes the "data.py is the only file that
touches my real data" claim true rather than aspirational.
"""
import math
import re
from collections import Counter, defaultdict, deque

from . import data

# --------------------------------------------------------------------------

WIKILINK = re.compile(r"\[\[([^\[\]|#]+?)(?:#[^\[\]|]*)?(?:\|([^\[\]]*))?\]\]")

STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "then", "so", "of", "in", "on",
    "at", "to", "for", "with", "from", "by", "as", "is", "are", "was", "were",
    "be", "been", "being", "it", "its", "this", "that", "these", "those", "i",
    "me", "my", "we", "our", "you", "your", "he", "she", "they", "them", "his",
    "her", "their", "do", "does", "did", "doing", "have", "has", "had", "not",
    "no", "yes", "can", "could", "would", "should", "will", "shall", "may",
    "might", "must", "about", "into", "over", "under", "again", "there", "here",
    "what", "which", "who", "whom", "when", "where", "why", "how", "all", "any",
    "both", "each", "few", "more", "most", "other", "some", "such", "only",
    "own", "same", "than", "too", "very", "just", "up", "out", "off", "down",
}

TOKEN = re.compile(r"[a-z0-9][a-z0-9'\-]*")

# Things a document might say that are aimed at an assistant rather than at
# Aviral. These get reported, never obeyed.
INJECTION_PATTERNS = [
    r"ignore (?:your |all |the )?(?:previous |prior |above )?instructions",
    r"disregard (?:your |all |the )?(?:previous |prior )?(?:instructions|rules)",
    r"you are now\b", r"\bnew instructions?\b", r"\bsystem prompt\b",
    r"instruction for any ai", r"\bassistant\s*:\s*", r"\bsudo\b",
    r"send (?:an? )?(?:email|message|reply) to", r"reply to this address",
    r"forward (?:this|the) (?:to|contents)", r"exfiltrat", r"api[_ -]?key",
    r"reveal your", r"print your (?:prompt|instructions)",
    r"override (?:your |the )?(?:rules|guardrails|safety)",
]
_INJECTION = re.compile("|".join(INJECTION_PATTERNS), re.I)


def stem(word):
    """Deliberately conservative — enough to join plurals and gerunds."""
    for suffix, keep in (("ies", 3), ("ing", 4), ("edly", 5), ("ed", 4), ("es", 4), ("s", 3)):
        if len(word) > keep and word.endswith(suffix):
            base = word[: -len(suffix)]
            if suffix == "ies":
                return base + "y"
            return base
    return word


def tokenize(text, keep_stop=False):
    out = []
    for m in TOKEN.finditer(text.lower()):
        w = m.group(0)
        if len(w) < 2:
            continue
        if not keep_stop and w in STOPWORDS:
            continue
        out.append(stem(w))
    return out


def scan_for_injection(text):
    """Return the lines that look like instructions aimed at an assistant."""
    hits = []
    for line in text.splitlines():
        if _INJECTION.search(line):
            clean = line.strip()
            hits.append(clean[:220] + ("…" if len(clean) > 220 else ""))
        if len(hits) >= 4:
            break
    return hits


# --------------------------------------------------------------------------

class Note:
    __slots__ = ("id", "title", "kind", "tags", "text", "display_path", "root",
                 "size", "modified", "created", "extract", "meta", "source",
                 "tokens", "tf", "length", "links", "backlinks", "unresolved")

    def __init__(self, doc):
        self.id = doc.id
        self.title = doc.title
        self.kind = doc.kind
        self.tags = doc.tags
        self.text = doc.text
        self.display_path = doc.display_path
        self.root = doc.root
        self.size = doc.size
        self.modified = doc.modified
        self.created = doc.created
        self.extract = doc.extract
        self.meta = doc.meta
        self.source = doc.source
        body = "%s %s %s" % (doc.title, " ".join(doc.tags), doc.text)
        self.tokens = tokenize(body)
        self.tf = Counter(self.tokens)
        self.length = max(1, len(self.tokens))
        self.links = []
        self.backlinks = []
        self.unresolved = []

    @property
    def degree(self):
        return len(set(self.links) | set(self.backlinks))


class Vault:
    def __init__(self):
        self.notes = {}
        self.order = []
        self.df = Counter()
        self.avg_len = 1.0
        self.edges = []
        self.by_title = {}
        self.warnings = []
        self.roots = {}

    # ---- build ----------------------------------------------------------
    @classmethod
    def build(cls):
        v = cls()
        for doc in data.iter_documents():
            note = Note(doc)
            v.notes[note.id] = note
            v.order.append(note.id)
        v._resolve_links()
        v._index()
        v.roots = data.roots_report()
        for path in v.roots.get("missing", []):
            v.warnings.append("Folder not found, so nothing from it is indexed: %s" % path)
        if not v.notes:
            v.warnings.append("No documents indexed. Check AERIS_VAULT_ROOTS, or set AERIS_DEMO=1.")
        broken = [n.id for n in v.notes.values() if n.extract == "none"]
        if broken:
            v.warnings.append(
                "%d PDF(s) gave up no text — they are in the graph but not searchable: %s"
                % (len(broken), ", ".join(broken[:3]) + ("…" if len(broken) > 3 else "")))
        return v

    def _resolve_links(self):
        # Wikilinks resolve by exact title, then by file stem, both case-folded.
        title_map, stem_map = {}, {}
        for note in self.notes.values():
            title_map.setdefault(note.title.lower(), note.id)
            stem_map.setdefault(note.id.rsplit("/", 1)[-1].lower(), note.id)
            self.by_title[note.title.lower()] = note.id
        seen = set()
        for note in self.notes.values():
            for m in WIKILINK.finditer(note.text):
                target = m.group(1).strip()
                key = target.lower()
                dest = title_map.get(key) or stem_map.get(key) or stem_map.get(
                    key.replace(" ", "-"))
                if not dest or dest == note.id:
                    if not dest:
                        note.unresolved.append(target)
                    continue
                note.links.append(dest)
                self.notes[dest].backlinks.append(note.id)
                pair = (note.id, dest) if note.id < dest else (dest, note.id)
                if pair not in seen:
                    seen.add(pair)
                    self.edges.append(pair)
        for note in self.notes.values():
            note.links = sorted(set(note.links))
            note.backlinks = sorted(set(note.backlinks))
            note.unresolved = sorted(set(note.unresolved))

    def _index(self):
        total = 0
        for note in self.notes.values():
            for term in note.tf:
                self.df[term] += 1
            total += note.length
        self.avg_len = (total / len(self.notes)) if self.notes else 1.0

    # ---- search ---------------------------------------------------------
    def idf(self, term):
        n = len(self.notes) or 1
        df = self.df.get(term, 0)
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def search(self, query, limit=8, kinds=None):
        terms = tokenize(query)
        if not terms:
            return []
        k1, b = 1.4, 0.72
        phrase = query.lower().strip()
        scored = []
        for note in self.notes.values():
            if kinds and note.kind not in kinds:
                continue
            score = 0.0
            matched = []
            for term in set(terms):
                f = note.tf.get(term, 0)
                if not f:
                    continue
                matched.append(term)
                denom = f + k1 * (1 - b + b * note.length / self.avg_len)
                score += self.idf(term) * (f * (k1 + 1)) / denom
            if not score:
                continue
            title_tokens = set(tokenize(note.title))
            score *= 1 + 0.55 * len(title_tokens & set(terms)) / max(1, len(set(terms)))
            if len(phrase) > 6 and phrase in note.text.lower():
                score *= 1.6
            score *= 1 + min(note.degree, 20) * 0.012        # hubs break ties
            scored.append((score, len(matched), note))
        scored.sort(key=lambda r: (-r[0], -r[1], r[2].title))
        out = []
        for score, n_matched, note in scored[:limit]:
            out.append({
                "id": note.id,
                "title": note.title,
                "kind": note.kind,
                "path": note.display_path,
                "score": round(score, 3),
                "terms_matched": n_matched,
                "modified": note.modified,
                "snippet": self.snippet(note, terms),
                "degree": note.degree,
                "extract": note.extract,
            })
        return out

    def snippet(self, note, terms, width=240):
        text = re.sub(r"\s+", " ", WIKILINK.sub(lambda m: m.group(2) or m.group(1), note.text))
        low = text.lower()
        best, best_hits = 0, -1
        step = 40
        for start in range(0, max(1, len(text) - width + 1), step):
            window = low[start:start + width]
            hits = sum(1 for t in set(terms) if t[:6] in window)
            if hits > best_hits:
                best_hits, best = hits, start
        chunk = text[best:best + width].strip()
        return ("…" if best else "") + chunk + ("…" if best + width < len(text) else "")

    # ---- graph ----------------------------------------------------------
    def adjacency(self):
        adj = defaultdict(set)
        for a, b in self.edges:
            adj[a].add(b)
            adj[b].add(a)
        return adj

    def path(self, a, b):
        if a not in self.notes or b not in self.notes:
            return []
        if a == b:
            return [a]
        adj = self.adjacency()
        prev, q = {a: None}, deque([a])
        while q:
            cur = q.popleft()
            for nxt in adj[cur]:
                if nxt in prev:
                    continue
                prev[nxt] = cur
                if nxt == b:
                    out, node = [], b
                    while node is not None:
                        out.append(node)
                        node = prev[node]
                    return list(reversed(out))
                q.append(nxt)
        return []

    def kind_counts(self):
        return Counter(n.kind for n in self.notes.values())

    def top_hubs(self, limit=10):
        ranked = sorted(self.notes.values(), key=lambda n: (-n.degree, n.title))
        return [{"id": n.id, "title": n.title, "kind": n.kind, "degree": n.degree}
                for n in ranked[:limit]]

    def note_payload(self, note_id):
        note = self.notes.get(note_id)
        if not note:
            return None
        text = note.text
        return {
            "id": note.id, "title": note.title, "kind": note.kind,
            "tags": note.tags, "path": note.display_path, "root": note.root,
            "modified": note.modified, "created": note.created,
            "size": note.size, "extract": note.extract, "meta": note.meta,
            "degree": note.degree,
            "links": [{"id": i, "title": self.notes[i].title} for i in note.links],
            "backlinks": [{"id": i, "title": self.notes[i].title} for i in note.backlinks],
            "unresolved": note.unresolved,
            "body": text[:20000],
            "truncated": len(text) > 20000,
            "injection": scan_for_injection(text),
        }

    def graph_payload(self):
        counts = self.kind_counts()
        nodes = []
        for nid in self.order:
            n = self.notes[nid]
            nodes.append({
                "id": n.id, "title": n.title, "kind": n.kind,
                "degree": n.degree, "modified": n.modified,
                "tags": n.tags[:4],
            })
        return {
            "nodes": nodes,
            "edges": [list(e) for e in self.edges],
            "counts": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
            "hubs": self.top_hubs(12),
            "mode": data.mode_label(),
            "roots": self.roots,
            "warnings": self.warnings,
            "total_notes": len(self.notes),
            "total_edges": len(self.edges),
        }


# --------------------------------------------------------------------------

_VAULT = None


def get(refresh=False):
    global _VAULT
    if _VAULT is None or refresh:
        _VAULT = Vault.build()
    return _VAULT


if __name__ == "__main__":                                    # step 1 report
    v = get()
    print("Aeris index — mode: %s" % data.mode_label())
    print("  roots      : %s" % (", ".join(v.roots["ok"]) or "none"))
    print("  documents  : %d" % len(v.notes))
    print("  wikilinks  : %d edges" % len(v.edges))
    print()
    print("  by type")
    for kind, count in v.kind_counts().most_common():
        print("    %-12s %4d" % (kind, count))
    print()
    print("  top 10 hubs")
    for h in v.top_hubs(10):
        print("    %-46s %-9s %3d links" % (h["title"][:46], h["kind"], h["degree"]))
    unresolved = sum(len(n.unresolved) for n in v.notes.values())
    print()
    print("  unresolved wikilinks : %d" % unresolved)
    for warn in v.warnings:
        print("  ! %s" % warn)
