"""The six tools.

Every one returns the same shape:

    {"tool": name,
     "spoken": "one or two sentences, said out loud",
     "card":   {...structured detail, shown on screen...}}

`spoken` and the card never carry the same words. The card is the evidence;
the spoken line is the headline. Reading the card aloud would be unbearable.

Guardrails that live in code rather than in the prompt, because a prompt is a
suggestion and this is not:

  * no function here sends anything, and none exists that could
  * nothing here writes, except `remember`, which delegates to memory.py
  * every derived number is carried as (value, qualifier) and `money_line`
    refuses to render one without its qualifier
  * text read out of files and mail is scanned for instructions aimed at an
    assistant, and those are reported, never followed
"""
import html
import inspect
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

from . import data, memory, vault as vault_mod

TODAY = date.today()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _num(x):
    try:
        return float(str(x).replace(",", "").replace("£", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def money_line(value, qualifier):
    """Never a derived figure on its own. The qualifier travels with it."""
    if not qualifier:
        raise ValueError("refusing to state £%s with no qualifier" % value)
    return {"value": "£{:,.0f}".format(value), "qualifier": qualifier}


def _cite(hit):
    return {"id": hit["id"], "title": hit["title"], "path": hit["path"], "kind": hit["kind"]}


def _plural(n, one, many=None):
    return one if n == 1 else (many or one + "s")


# Words that say nothing about who a sender is.
GENERIC_SENDER = {
    "reception", "accounts", "team", "noreply", "no-reply", "info", "hello",
    "admin", "support", "contact", "alerts", "alert", "talent", "careers",
    "billing", "sales", "the", "ltd", "limited", "inc", "llc", "group",
    "sender", "unknown", "newsletter", "mail", "email", "notifications",
}


def _tokens(text):
    return [t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) > 1]


def _who_is(v, name, address):
    """Does this sender already exist in Aviral's files?

    Matched on identity, not on shared vocabulary. A newsletter that happens
    to use the word "pricing" is not a contact he knows, and saying it is
    would make this tool worse than useless.
    """
    domain = (address or "").split("@")[-1].lower()
    stem = re.sub(r"[^a-z0-9]", "", domain.split(".")[0])
    name_tokens = [t for t in _tokens(name) if t not in GENERIC_SENDER]

    best, best_score = None, 0.0
    for note in v.notes.values():
        if note.kind not in ("client", "person", "project", "company", "contact"):
            continue
        title_tokens = [t for t in _tokens(note.title) if t not in GENERIC_SENDER]
        if not title_tokens:
            continue
        score = 0.0
        if name_tokens:
            overlap = len(set(name_tokens) & set(title_tokens))
            score = overlap / len(set(name_tokens))
        # northgatecu.example -> "Northgate Credit Union"
        if stem and len(title_tokens[0]) >= 4 and stem.startswith(title_tokens[0]):
            score = max(score, 1.0)
        if score > best_score:
            best_score, best = score, note
    if best and best_score >= 0.5:
        return {"id": best.id, "title": best.title, "kind": best.kind, "why": "name match"}

    # Not an org he tracks — is the person named in a meeting or a contact note?
    # Deliberately not a search over every note: a newsletter sharing a word
    # with an idea note is not somebody he knows.
    for token in name_tokens:
        if len(token) < 4:
            continue
        for note in v.notes.values():
            if note.kind not in ("meeting", "client", "person", "contact"):
                continue
            if re.search(r"\b%s\b" % re.escape(token), note.title, re.I):
                return {"id": note.id, "title": note.title, "kind": note.kind,
                        "why": "named in a %s note" % note.kind}
    return None


def _qual_category(text):
    """Group the reason a balance is outstanding. Different reasons must not
    be collapsed into one sentence."""
    low = (text or "").lower()
    if "paid in full" in low:
        return "settled"
    if "overdue" in low:
        return "overdue"
    if "draft" in low or "not sent" in low:
        return "not sent"
    if "milestone" in low or "still running" in low or "not started" in low or "deposit" in low:
        return "work not finished"
    if "inside terms" in low:
        return "not yet due"
    return "recorded reason"


def _first_sentence(text, limit=180):
    text = re.sub(r"\s+", " ", text).strip()
    m = re.search(r"^(.{20,%d}?[.!?])\s" % limit, text)
    return (m.group(1) if m else text[:limit]).strip()


# --------------------------------------------------------------------------
# 1. search_brain
# --------------------------------------------------------------------------

def search_brain(v, query="", kind=None, limit=6, **_):
    query = (query or "").strip()
    if not query:
        return _tool("search_brain", "Nothing to look for, SIR.",
                     {"title": "Empty search", "rows": []})

    kinds = [kind] if kind else None
    hits = v.search(query, limit=limit, kinds=kinds)
    if not hits:
        return _tool(
            "search_brain",
            "Aviral, SIR — nothing in your files matches that. I'm not going to guess at it.",
            {"title": "No match", "query": query,
             "note": "%d documents searched, none scored." % len(v.notes), "items": []})

    # Which files actually carry the answer? Anything within 45% of the top.
    top = hits[0]["score"]
    load_bearing = [h for h in hits if h["score"] >= top * 0.66][:3]
    n = len(load_bearing)

    # If invoices carried the answer, the figures go out with their reason
    # attached. A part-paid invoice is not a discount and must never sound
    # like one.
    all_invoices = _open_invoices(v)
    # The client comes from the best-scoring invoice, not from every invoice
    # that happened to surface. Answering about the wrong client is worse than
    # answering about none.
    rank = {h["id"]: i for i, h in enumerate(load_bearing)}
    lb_invoices = sorted((inv for inv in all_invoices if inv["id"] in rank),
                         key=lambda inv: rank[inv["id"]])
    subject_client = lb_invoices[0]["client"] if lb_invoices else None
    if not subject_client:
        # "Kestrel invoices" can rank the client note above any single invoice.
        # Asking about a client is asking about their money too.
        clients_with_money = {inv["client"] for inv in all_invoices}
        for h in load_bearing:
            note = v.notes.get(h["id"])
            if note and note.kind == "client" and note.title in clients_with_money:
                subject_client = note.title
                break
    money = []
    for inv in all_invoices:
        # Every invoice for that one client — one invoice out of five is a
        # misleading answer to "how much have they paid".
        if subject_client and inv["client"] == subject_client:
            if not inv["qualifier"]:
                money.append({"k": inv["number"] or inv["title"],
                              "v": "figures withheld",
                              "qualifier": "This invoice has no reason recorded against it, "
                                           "so I will not state the outstanding amount."})
                continue
            line = money_line(inv["outstanding"], inv["qualifier"])
            money.append({
                "k": inv["number"] or inv["title"],
                "v": "£{:,.0f} of £{:,.0f} received · <b>{}</b> outstanding".format(
                    inv["received"], inv["total"], line["value"]),
                "qualifier": "%s — %s, not a discount and not a write-off."
                             % (inv["status"], line["qualifier"]),
            })

    injections = []
    for h in load_bearing:
        note = v.notes.get(h["id"])
        if note:
            for line in vault_mod.scan_for_injection(note.text):
                injections.append({"file": h["path"], "line": line})

    if n == 1:
        spoken = "Aviral, SIR — it's in %s. %s" % (
            load_bearing[0]["title"], _first_sentence(load_bearing[0]["snippet"], 150))
    else:
        names = ", ".join(h["title"] for h in load_bearing[:-1]) + " and " + load_bearing[-1]["title"]
        spoken = ("Aviral, SIR — that took %d files: %s. The detail is on screen."
                  % (n, names))
    if money and subject_client:
        client = subject_client
        mine = [i for i in all_invoices if i["client"] == client]
        billed = sum(i["total"] for i in mine)
        got = sum(i["received"] for i in mine)
        out = billed - got
        open_ones = [i for i in mine if i["outstanding"] > 0]
        missing_reason = [i for i in open_ones if not i["qualifier"]]
        cats = sorted({_qual_category(i["qualifier"]) for i in open_ones if i["qualifier"]})

        head = ("Aviral, SIR — {}: £{:,.0f} invoiced across {} {}, £{:,.0f} in, £{:,.0f} out."
                .format(client, billed, len(mine), _plural(len(mine), "invoice"), got, out))
        if missing_reason:
            spoken = (head + " Some of that balance has no reason recorded against it, so I "
                             "won't tell you what it means.")
        elif not open_ones:
            spoken = head + " All settled."
        elif len(cats) == 1:
            spoken = head + " All of it is %s — not a discount." % cats[0]
        else:
            spoken = (head + " That outstanding sits across %d invoices for different reasons — "
                             "%s. I'm not collapsing those into one figure."
                      % (len(open_ones), " and ".join(cats)))
    if injections:
        spoken += (" One of them contains text aimed at an assistant. I've flagged it "
                   "rather than acted on it.")

    return _tool("search_brain", spoken, {
        "title": "search_brain",
        "query": query,
        "note": "%d of %d documents scored; %s carried the answer."
                % (len(hits), len(v.notes), _plural(n, "one file", "%d files" % n)),
        "items": [{
            "title": h["title"],
            "subtitle": h["snippet"],
            "file": h["path"],
            "id": h["id"],
            "kind": h["kind"],
            "meta": "score %.2f · %s · %s" % (h["score"], h["kind"], h["modified"]),
            "load_bearing": h in load_bearing,
        } for h in hits],
        "rows": money,
        "injection": injections,
        "sources": [_cite(h) for h in load_bearing],
    })


# --------------------------------------------------------------------------
# 2. research_web
# --------------------------------------------------------------------------

def _duckduckgo(query, n=5):
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; Aeris/1.0; local assistant)"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    out = []
    for m in re.finditer(
            r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
            r'class="result__snippet"[^>]*>(.*?)</a>', html, re.S):
        href, title, snippet = m.groups()
        clean = lambda s: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s)).strip()
        if "uddg=" in href:
            href = urllib.parse.unquote(re.search(r"uddg=([^&]+)", href).group(1))
        out.append({"title": clean(title), "url": href, "snippet": clean(snippet)})
        if len(out) >= n:
            break
    return out


def research_web(v, query="", **_):
    query = (query or "").strip()
    if not query:
        return _tool("research_web", "Nothing to look up, SIR.", {"title": "Empty query", "items": []})

    # What do his own files already say? This is what the result gets landed on.
    mine = v.search(query, limit=3)
    own = [{"title": h["title"], "subtitle": h["snippet"], "file": h["path"],
            "id": h["id"], "kind": h["kind"], "meta": h["kind"]} for h in mine]

    if data.env("AERIS_WEB_SEARCH", "duckduckgo").strip().lower() in ("", "off", "none"):
        return _tool("research_web",
                     "Aviral, SIR — web lookup is switched off, so I can only tell you what "
                     "your own files say about it.",
                     {"title": "research_web — offline", "query": query,
                      "note": "AERIS_WEB_SEARCH is off. Nothing was fetched and nothing was guessed.",
                      "items": own, "degraded": True})

    try:
        results = _duckduckgo(query)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, AttributeError) as exc:
        return _tool("research_web",
                     "Aviral, SIR — I couldn't reach the web just now, so this is your own "
                     "files only. Nothing invented.",
                     {"title": "research_web — unreachable", "query": query,
                      "note": "Search failed: %s. No result is being guessed at."
                              % type(exc).__name__,
                      "items": own, "degraded": True})

    if not results:
        return _tool("research_web",
                     "Aviral, SIR — the search came back empty. I won't fill the gap myself.",
                     {"title": "research_web — no results", "query": query,
                      "items": own, "degraded": True})

    # Land it back on his numbers rather than reciting the web's.
    landed = ""
    if mine:
        landed = ("Your own %s already covers this — worth reading side by side."
                  % mine[0]["title"])
        spoken = ("Aviral, SIR — %d results, top one from %s. %s"
                  % (len(results), _domain(results[0]["url"]), landed))
    else:
        spoken = ("Aviral, SIR — %d results back, top one from %s. Nothing in your own files "
                  "on this yet, so there's no number of yours to hold it against."
                  % (len(results), _domain(results[0]["url"])))

    return _tool("research_web", spoken, {
        "title": "research_web",
        "query": query,
        "note": "Web results are outside sources. Your own figures are the second block.",
        "items": [{"title": r["title"], "subtitle": r["snippet"],
                   "file": _domain(r["url"]), "url": r["url"], "meta": "web"} for r in results],
        "own": own,
        "own_label": "Against your own files",
    })


def _domain(url):
    try:
        return urllib.parse.urlparse(url).netloc.replace("www.", "")
    except ValueError:
        return url[:40]


# --------------------------------------------------------------------------
# 3. read_inbox
# --------------------------------------------------------------------------

def read_inbox(v, unread_only=False, limit=8, **_):
    messages, note = data.inbox()
    if not messages:
        return _tool("read_inbox",
                     "Aviral, SIR — no mailbox is connected, so there's nothing to read.",
                     {"title": "read_inbox — nothing connected",
                      "note": note or "No inbox source.", "items": [], "degraded": True})

    msgs = [m for m in messages if m.get("unread")] if unread_only else messages
    msgs = sorted(msgs, key=lambda m: m.get("received", ""), reverse=True)[:limit]

    items, known, unknown, flagged = [], [], [], []
    for m in msgs:
        # The whole value: do I already know this person?
        name = m.get("from_name") or m.get("from", "")
        match = _who_is(v, name, m.get("from", ""))
        if match:
            known.append(name)
        else:
            unknown.append(name)

        injected = vault_mod.scan_for_injection(m.get("body", "") + " " + m.get("subject", ""))
        if injected:
            flagged.append({"from": name, "subject": m.get("subject", ""), "lines": injected})

        items.append({
            "title": "%s — %s" % (name, m.get("subject", "(no subject)")),
            "subtitle": _first_sentence(m.get("body", ""), 200),
            "file": (("in your files: " + match["title"]) if match else "NOT in your files"),
            "id": match["id"] if match else None,
            "meta": "%s%s" % (m.get("received", "")[:10],
                              " · unread" if m.get("unread") else ""),
            "known": bool(match),
            "flagged": bool(injected),
        })

    unread_n = sum(1 for m in messages if m.get("unread"))
    bits = ["Aviral, SIR — %d %s, %d unread." % (len(msgs), _plural(len(msgs), "message"), unread_n)]
    if known:
        bits.append("%d %s already in your files." % (len(known), _plural(len(known), "sender")))
    if unknown:
        bits.append("%d you've no record of." % len(unknown))
    if flagged:
        bits.append("One is trying to give me instructions — I've flagged it, not followed it.")
    spoken = " ".join(bits)

    return _tool("read_inbox", spoken, {
        "title": "read_inbox",
        "note": "Read-only. Aeris has no send capability — nothing here can be replied to by me.",
        "items": items,
        "injection": [{"file": "inbox · %s" % f["from"], "line": l}
                      for f in flagged for l in f["lines"]],
    })


# --------------------------------------------------------------------------
# 4. brief_me
# --------------------------------------------------------------------------

def _open_invoices(v):
    out = []
    for note in v.notes.values():
        if note.kind != "invoice":
            continue
        total = _num(note.meta.get("total_gbp"))
        got = _num(note.meta.get("received_gbp"))
        if total is None or got is None:
            continue
        qualifier = str(note.meta.get("qualifier") or "").strip()
        out.append({
            "id": note.id, "title": note.title,
            "number": note.meta.get("number", ""), "client": note.meta.get("client", ""),
            "total": total, "received": got, "outstanding": total - got,
            "status": str(note.meta.get("status", "")).strip(),
            # No qualifier means we do not state the derived figure at all.
            "qualifier": qualifier,
        })
    return out


def brief_me(v, **_):
    events, cal_note = data.calendar()
    messages, mail_note = data.inbox()
    today_iso = TODAY.isoformat()

    today_events = [e for e in events if e.get("date") == today_iso]
    unread = [m for m in messages if m.get("unread")]

    # what slipped: high-priority open tasks + invoices explicitly overdue
    slipped = []
    for note in v.notes.values():
        if note.kind == "task" and str(note.meta.get("priority", "")).lower() == "high" \
                and str(note.meta.get("status", "open")).lower() == "open":
            slipped.append({"title": note.title, "id": note.id, "why": "high priority, still open"})
    overdue = [i for i in _open_invoices(v) if "overdue" in i["qualifier"].lower()]
    for inv in overdue:
        slipped.append({"title": "%s %s" % (inv["number"], inv["client"]), "id": inv["id"],
                        "why": inv["qualifier"]})
    slipped = slipped[:6]

    rows = []
    if today_events:
        rows.append({"k": "Today", "v": "<b>%d</b> in the calendar — first is %s at %s"
                     % (len(today_events), today_events[0]["title"], today_events[0]["start"])})
    else:
        rows.append({"k": "Today", "v": cal_note or "Nothing in the calendar."})
    rows.append({"k": "Unread", "v": ("<b>%d</b> unread, %d from senders already in your files"
                                      % (len(unread), _known_count(v, unread)))
                 if messages else (mail_note or "No mailbox connected.")})
    if overdue:
        inv = overdue[0]
        m = money_line(inv["outstanding"], inv["qualifier"])
        rows.append({"k": "Money", "v": "%s outstanding on %s" % (m["value"], inv["number"]),
                     "qualifier": m["qualifier"]})
    rows.append({"k": "Slipped", "v": "<b>%d</b> %s carried over"
                 % (len(slipped), _plural(len(slipped), "item"))})

    spoken = ("Aviral, SIR — %s today, %d unread, and %d %s carried over. "
              "The one that matters is %s.") % (
        ("%d %s" % (len(today_events), _plural(len(today_events), "thing"))
         if today_events else "nothing"),
        len(unread), len(slipped), _plural(len(slipped), "item"),
        slipped[0]["title"] if slipped else "nothing, for once")

    return _tool("brief_me", spoken, {
        "title": "brief_me",
        "subtitle": TODAY.strftime("%A %d %B %Y"),
        "rows": rows,
        "items": [{"title": e["title"], "subtitle": "%s · %d min" % (e["start"], e["minutes"]),
                   "meta": "calendar"} for e in today_events]
                 + [{"title": s["title"], "subtitle": s["why"], "id": s.get("id"),
                     "meta": "slipped"} for s in slipped],
        "note": "Read-only. Nothing has been sent, accepted or declined.",
    })


def _known_count(v, messages):
    return sum(1 for m in messages
               if _who_is(v, m.get("from_name", ""), m.get("from", "")))


# --------------------------------------------------------------------------
# 5. remember
# --------------------------------------------------------------------------

def remember(v, fact="", source="asked", **_):
    fact = (fact or "").strip()
    if not fact:
        return _tool("remember", "Nothing to write down, SIR. Say the fact and I'll store it.",
                     {"title": "remember — nothing given", "rows": []})

    written = memory.write(fact, source=source)
    # The spoken line IS the receipt. There is no silent path to memory.
    spoken = "Aviral, SIR — %s" % written["spoken_receipt"]

    return _tool("remember", spoken, {
        "title": "remember",
        "subtitle": written["path"],
        "rows": [
            {"k": "Wrote", "v": "<b>%s</b>" % written["fact"]},
            {"k": "File", "v": written["path"]},
            {"k": "When", "v": written["created"]},
            {"k": "Scope", "v": "memory/ only — nothing under your vault was touched"},
        ],
        "note": "One fact, one dated file. Delete it by deleting the file.",
        "receipt": written["spoken_receipt"],
    }, receipt=written["spoken_receipt"])


# --------------------------------------------------------------------------
# 6. plan_day
# --------------------------------------------------------------------------

MONEY_WORDS = re.compile(
    r"\b(invoice|paid|payment|pilot|scope|pricing|price|client|proposal|contract|"
    r"quote|deposit|overdue|revenue|deal|sign|renew|waitlist|launch|demo|sell|sales)\b", re.I)


def plan_day(v, **_):
    invoices = _open_invoices(v)
    owed_by_client = {}
    for inv in invoices:
        if inv["outstanding"] > 0:
            owed_by_client.setdefault(inv["client"], []).append(inv)

    client_value = {}
    for note in v.notes.values():
        if note.kind == "client":
            val = _num(note.meta.get("proposed_value_gbp")) or 0
            client_value[note.title] = val

    scored = []
    for note in v.notes.values():
        if note.kind != "task":
            continue
        if str(note.meta.get("status", "open")).lower() not in ("open", "doing"):
            continue
        score, why = 0.0, []

        prio = str(note.meta.get("priority", "")).lower()
        score += {"high": 3.0, "med": 1.4, "low": 0.5}.get(prio, 0.8)

        blob = note.title + " " + note.text
        money_hits = len(MONEY_WORDS.findall(blob))
        if money_hits:
            score += min(3.0, money_hits * 0.9)
            why.append("touches money")

        linked_clients = [v.notes[i].title for i in note.links
                          if i in v.notes and v.notes[i].kind == "client"]
        for name in linked_clients:
            val = client_value.get(name, 0)
            if val:
                score += min(3.5, val / 2500.0)
                why.append("%s, £%s on the table" % (name, "{:,.0f}".format(val)))
            if name in owed_by_client:
                inv = owed_by_client[name][0]
                score += 2.2
                why.append("%s outstanding on %s" % ("£{:,.0f}".format(inv["outstanding"]),
                                                     inv["number"]))
        if not why:
            why.append("%s priority, no money attached" % (prio or "unset"))
        scored.append((score, note, why))

    scored.sort(key=lambda r: (-r[0], r[1].title))
    top = scored[:5]

    if not top:
        return _tool("plan_day", "Aviral, SIR — no open tasks in your files. Nothing to order.",
                     {"title": "plan_day", "items": [], "note": "No task-type notes found."})

    items = []
    for i, (score, note, why) in enumerate(top, 1):
        items.append({
            "title": "%d. %s" % (i, note.title),
            "subtitle": "; ".join(why),
            "file": note.display_path,
            "id": note.id,
            "meta": "weight %.1f" % score,
        })

    spoken = ("Aviral, SIR — five things, money first. Start with %s. "
              "The rest are on screen in order." % top[0][1].title)

    return _tool("plan_day", spoken, {
        "title": "plan_day",
        "subtitle": "ordered by what moves money · %s" % TODAY.strftime("%a %d %b"),
        "items": items,
        "note": "Ranked from your own task notes and the values written against your "
                "clients. Nothing here was invented and nothing was scheduled.",
    })


# --------------------------------------------------------------------------
# 7-9. the ones with hands
#
# Every one of these goes through actions.propose(), so the gate decides
# whether it runs or waits. Nothing here reaches the disk on its own.
# --------------------------------------------------------------------------

def look_at_screen(v, question="", **_):
    from . import actions
    out = actions.propose("look_at_screen", {"question": question},
                          reason="asked to look at the screen")
    result = out.get("result") or {}
    if not result.get("ok"):
        return _tool("look_at_screen",
                     "Aviral, SIR — I couldn't see the screen. %s" % result.get("summary", ""),
                     {"title": "look_at_screen — no image",
                      "note": result.get("summary", ""), "degraded": True})
    return _tool("look_at_screen", "Aviral, SIR — %s" % result["summary"], {
        "title": "look_at_screen",
        "subtitle": "one capture, read by %s, image discarded" % result.get("read_by", "?"),
        "rows": [{"k": "Read by", "v": "%s%s" % (result.get("read_by", ""),
                                                 "" if result.get("free") else " (metered)")},
                 {"k": "Kept", "v": "nothing — the screenshot is already deleted"}],
        "note": "Captured because you asked. Aeris takes no screenshots on her own.",
    })


def _gated(name, args, spoken_ok, title, reason=""):
    """Shared shape for a capability that may need approval first."""
    from . import actions
    out = actions.propose(name, args, reason=reason)
    if out["status"] == "needs_confirmation":
        return _tool(name,
                     "Aviral, SIR — that one needs your say-so. %s. Approve it on screen."
                     % out["summary"],
                     {"title": "%s — waiting for you" % title,
                      "subtitle": out["summary"],
                      "confirm": {"token": out["token"], "summary": out["summary"]},
                      "note": "Nothing has happened yet. It runs only if you approve it.",
                      "rows": [{"k": "Action", "v": name},
                               {"k": "Status", "v": "<b>waiting for your approval</b>"}]})
    if out["status"] == "unknown":
        return _tool(name, "I don't know that one, SIR.",
                     {"title": "unknown action", "note": out["summary"]})
    result = out.get("result") or {}
    ok = result.get("ok")
    return _tool(name,
                 "Aviral, SIR — %s" % (spoken_ok if ok else result.get("summary", "it failed.")),
                 {"title": title, "subtitle": result.get("summary", ""),
                  "rows": [{"k": k, "v": str(val)[:300]}
                           for k, val in result.items()
                           if k in ("path", "backup", "code", "stdout", "stderr") and val],
                  "note": "Logged to audit/actions.jsonl." if ok else "Nothing was changed.",
                  "degraded": not ok})


def write_file(v, path="", content="", **_):
    return _gated("write_file", {"path": path, "content": content},
                  "written, and the old version is kept.", "write_file",
                  reason="asked to write a file")


def _from_proposal(out, name, title, spoken_ok, extra_rows=None):
    """Shape a capture proposal into a tool result."""
    if out.get("status") == "needs_confirmation":
        return _tool(name, "Aviral, SIR — that needs your say-so. %s" % out["summary"],
                     {"title": "%s — waiting for you" % title,
                      "confirm": {"token": out["token"], "summary": out["summary"]},
                      "note": "Nothing written yet."})
    result = out.get("result") or {}
    if not result.get("ok"):
        return _tool(name, "Aviral, SIR — I couldn't write it. %s" % result.get("summary", ""),
                     {"title": "%s — failed" % title, "note": result.get("summary", ""),
                      "degraded": True})
    return _tool(name, "Aviral, SIR — %s" % spoken_ok, {
        "title": title,
        "subtitle": result.get("path", ""),
        "rows": (extra_rows or []) + [
            {"k": "File", "v": result.get("path", "")},
            {"k": "Scope", "v": "your vault — reindex to see it in the graph"}],
        "note": "Logged to audit/actions.jsonl. The old version is kept if there was one.",
    })


def capture_note(v, title="", body="", kind="note", tags=None, links=None, **_):
    from . import capture
    out = capture.note(title=title, body=body, kind=kind, tags=tags, links=links)
    return _from_proposal(out, "capture_note", "capture_note",
                          "written down as a %s." % (kind or "note"),
                          # row.v reaches the page as raw HTML, and this title came
                          # from a model. Escape it.
                          extra_rows=[{"k": "Title", "v": "<b>%s</b>" % html.escape(title)}])


def log_today(v, text="", **_):
    from . import capture
    out = capture.log(text)
    return _from_proposal(out, "log_today", "log_today",
                          "logged to today's note.")


# --------------------------------------------------------------------------
# 10. check_repos — his code, which is the record he did not write
# --------------------------------------------------------------------------

def check_repos(v, days=7, limit=8, **_):
    from . import github
    if not github.configured():
        return _tool("check_repos",
                     "Aviral, SIR — no GitHub token is set, so I can't see your repos.",
                     {"title": "check_repos — not connected",
                      "note": "Add GITHUB_TOKEN to Aeris/.env. A fine-grained token with "
                              "read access to your repositories is enough.",
                      "degraded": True})

    try:
        days = max(1, min(90, int(days)))
        limit = max(1, min(30, int(limit)))
    except (TypeError, ValueError):
        days, limit = 7, 8

    out, err = github.overview(limit=limit, days=days)
    if err:
        return _tool("check_repos", "Aviral, SIR — GitHub wouldn't answer. %s" % err,
                     {"title": "check_repos — failed", "note": err, "degraded": True})

    failing, waiting, stale = out["failing"], out["waiting"], out["stale"]
    active = out["active"]

    items = []
    for f in failing:
        items.append({"title": "%s — %s" % (f["repo"], f["why"]),
                      "subtitle": "red for %d %s" % (f["age_days"],
                                                     _plural(f["age_days"], "day")),
                      "url": f["url"], "meta": "failing", "flagged": True})
    for w in waiting:
        items.append({"title": "%s#%d — %s" % (w["repo"], w["number"], w["title"]),
                      "subtitle": "open %d %s%s" % (w["age_days"],
                                                    _plural(w["age_days"], "day"),
                                                    ", draft" if w["draft"] else ""),
                      "url": w["url"], "meta": "pull request"})
    for s in stale:
        items.append({"title": "%s — quiet for %d days" % (s["repo"], s["age_days"]),
                      "subtitle": "%d open %s and no pushes"
                                  % (s["open"], _plural(s["open"], "issue")),
                      "url": s["url"], "meta": "stale"})

    rows = [
        {"k": "Failing", "v": ("<b>%d</b> %s red" % (len(failing),
                                                     _plural(len(failing), "build"))
                               if failing else "nothing red")},
        {"k": "Open PRs", "v": ("<b>%d</b> waiting" % len(waiting)
                                if waiting else "none open")},
        {"k": "Stale", "v": ("<b>%d</b> %s quiet with issues open"
                             % (len(stale), _plural(len(stale), "repo"))
                             if stale else "nothing abandoned")},
        {"k": "Worked on", "v": (", ".join("%s (%d)" % (a["repo"].split("/")[-1],
                                                        a["commits"])
                                           for a in active[:4])
                                 if active else "no commits in %d days" % days)},
    ]

    # Ordered by what actually blocks him. A red build stops everything behind
    # it; a stale repo has been fine for three weeks and can wait an hour.
    if failing:
        spoken = ("Aviral, SIR — %d %s red. Start with %s."
                  % (len(failing), _plural(len(failing), "build"), failing[0]["repo"]))
    elif waiting:
        oldest = waiting[0]
        spoken = ("Aviral, SIR — nothing is red. %d pull %s open, the oldest is %s#%d "
                  "at %d days." % (len(waiting), _plural(len(waiting), "request"),
                                   oldest["repo"], oldest["number"], oldest["age_days"]))
    elif active:
        spoken = ("Aviral, SIR — all green. %d %s in the last %d days, mostly on %s."
                  % (sum(a["commits"] for a in active),
                     _plural(sum(a["commits"] for a in active), "commit"),
                     days, active[0]["repo"].split("/")[-1]))
    else:
        spoken = ("Aviral, SIR — all green, and nothing committed in %d days. "
                  "Your repos are quiet." % days)

    return _tool("check_repos", spoken, {
        "title": "check_repos",
        "subtitle": "%d %s as %s · last %d days"
                    % (out["repos"], _plural(out["repos"], "repo"),
                       out["me"]["login"], days),
        "rows": rows,
        "items": items,
        "note": "Read-only. Opening an issue or commenting stops and asks you first.",
    })


def github_issue(v, repo="", title="", body="", **_):
    return _gated("github_create_issue", {"repo": repo, "title": title, "body": body},
                  "issue opened.", "github_create_issue",
                  reason="asked to open a GitHub issue")


# --------------------------------------------------------------------------
# 11. check_deploys — is anything live actually broken
# --------------------------------------------------------------------------

def check_deploys(v, **_):
    from . import railway
    if not railway.configured():
        return _tool("check_deploys",
                     "Aviral, SIR — no Railway token is set, so I can't see your deploys.",
                     {"title": "check_deploys — not connected",
                      "note": "Add RAILWAY_TOKEN to Aeris/.env. Get one at "
                              "railway.com/account/tokens.",
                      "degraded": True})

    out, err = railway.overview()
    if err:
        return _tool("check_deploys",
                     "Aviral, SIR — Railway wouldn't answer, so I don't know. %s" % err,
                     {"title": "check_deploys — failed", "note": err, "degraded": True})

    broken, flight, live = out["broken"], out["in_flight"], out["live"]
    items = []
    for row in broken:
        items.append({"title": "%s · %s — %s" % (row["project"], row["service"],
                                                 row["status"]),
                      "subtitle": "last deploy %s" % (row["age"] or "unknown"),
                      "url": row["url"], "meta": "broken", "flagged": True})
    for row in flight:
        items.append({"title": "%s · %s — %s" % (row["project"], row["service"],
                                                 row["status"]),
                      "subtitle": "started %s" % (row["age"] or "just now"),
                      "url": row["url"], "meta": "in flight"})
    for row in live:
        items.append({"title": "%s · %s" % (row["project"], row["service"]),
                      "subtitle": "live, deployed %s" % (row["age"] or "unknown"),
                      "url": row["url"], "meta": "live"})

    rows = [
        {"k": "Broken", "v": ("<b>%d</b> down" % len(broken)) if broken else "nothing down"},
        {"k": "Deploying", "v": ("<b>%d</b> in flight" % len(flight))
                                if flight else "nothing mid-deploy"},
        {"k": "Live", "v": "<b>%d</b> %s up" % (len(live), _plural(len(live), "service"))},
    ]

    if broken:
        first = broken[0]
        spoken = ("Aviral, SIR — %s on %s is %s, since %s. That's the one to look at."
                  % (first["service"], first["project"], first["status"].lower(),
                     first["age"] or "some time ago"))
    elif flight:
        spoken = ("Aviral, SIR — nothing broken. %d %s still deploying."
                  % (len(flight), _plural(len(flight), "service")))
    elif live:
        spoken = ("Aviral, SIR — all %d %s up on Railway. Nothing to do."
                  % (len(live), _plural(len(live), "service")))
    else:
        spoken = ("Aviral, SIR — Railway has %d %s but nothing deployed in them."
                  % (out["projects"], _plural(out["projects"], "project")))

    # A project that could not be read is not a project that is fine.
    if out["problems"]:
        spoken += " Some projects I couldn't read at all — that's on screen."

    return _tool("check_deploys", spoken, {
        "title": "check_deploys",
        "subtitle": "%d %s on Railway" % (out["projects"],
                                          _plural(out["projects"], "project")),
        "rows": rows,
        "items": items,
        "injection": [{"file": "railway", "line": p} for p in out["problems"]],
        "note": "Read-only. Aeris cannot redeploy or restart anything on Railway.",
    })


def run_command(v, command="", cwd="", **_):
    return _gated("run_command", {"command": command, "cwd": cwd},
                  "done.", "run_command", reason="asked to run a command")


# --------------------------------------------------------------------------

def _tool(name, spoken, card, receipt=None):
    out = {"tool": name, "spoken": spoken, "card": card}
    if receipt:
        out["receipt"] = receipt
    return out


REGISTRY = {
    "search_brain": search_brain,
    "research_web": research_web,
    "read_inbox": read_inbox,
    "brief_me": brief_me,
    "remember": remember,
    "plan_day": plan_day,
    "look_at_screen": look_at_screen,
    "write_file": write_file,
    "run_command": run_command,
    "capture_note": capture_note,
    "log_today": log_today,
    "check_repos": check_repos,
    "github_issue": github_issue,
    "check_deploys": check_deploys,
}


def run(name, v, args):
    """Call a tool with only the arguments it declares.

    Arguments are filtered against the signature rather than wrapped in a
    try/except TypeError — a blanket catch there silently turns a real bug
    inside a tool into an empty-argument call, which is exactly the kind of
    quiet failure this build is meant not to have.
    """
    fn = REGISTRY.get(name)
    if not fn:
        return _tool("unknown", "I don't know, SIR.",
                     {"title": "Unknown tool", "note": "No tool called %r." % name})
    params = inspect.signature(fn).parameters
    takes_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    clean = {k: val for k, val in (args or {}).items()
             if takes_kwargs or k in params}
    return fn(v, **clean)
