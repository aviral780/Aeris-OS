"""Deciding what to do with a turn, with or without a language model.

Aeris has to be useful on a machine with no API key and no Ollama running.
So there are two paths and the UI always says which one produced the answer:

  * `model`   — a real LLM chose the tool and wrote the words.
  * `scoring` — no model was reachable. The question is scored against the
                vault index to decide conversation vs search, and the words
                come from templates.

The scoring path is never dressed up as the model talking. That is the whole
point of the badge in the top right.
"""
import json
import random
import re
import urllib.error
import urllib.request

from . import data

TIMEOUT = 45

BANNED_OPENERS = [
    "absolutely", "great question", "certainly", "of course",
    "i'd be happy to", "i would be happy to", "sure thing", "no problem",
    "as an ai", "i'm just an ai", "let me help you with that",
]

# --------------------------------------------------------------------------
# backend discovery
# --------------------------------------------------------------------------

_STATUS = None


def _post(url, payload, headers, timeout=TIMEOUT):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers=dict({"Content-Type": "application/json"}, **headers))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def probe(refresh=False):
    """Which backend, if any. Cached — a probe per turn would be silly."""
    global _STATUS
    if _STATUS is not None and not refresh:
        return _STATUS
    want = data.env("AERIS_LLM", "auto").strip().lower()
    status = {"backend": "none", "model": "", "detail": "", "ok": False}

    def try_ollama():
        url = data.env("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
        try:
            with urllib.request.urlopen(url + "/api/tags", timeout=2.5) as resp:
                tags = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:                                   # noqa: BLE001
            return None, "Ollama not reachable at %s (%s)" % (url, type(exc).__name__)
        names = [m.get("name", "") for m in tags.get("models", [])]
        if not names:
            return None, "Ollama is up but has no models pulled."
        want_model = data.env("OLLAMA_MODEL", "llama3.1")
        chosen = next((n for n in names if n.startswith(want_model)), names[0])
        return {"backend": "ollama", "model": chosen, "url": url,
                "detail": "local, free", "ok": True}, ""

    def try_anthropic():
        key = data.env("ANTHROPIC_API_KEY", "").strip()
        if not key:
            return None, "No ANTHROPIC_API_KEY set."
        return {"backend": "anthropic", "model": data.env("ANTHROPIC_MODEL", "claude-sonnet-5"),
                "key": key, "detail": "paid — billed per token", "ok": True}, ""

    reasons = []
    order = {"auto": [try_ollama, try_anthropic], "ollama": [try_ollama],
             "anthropic": [try_anthropic], "off": []}.get(want, [try_ollama, try_anthropic])
    for fn in order:
        got, why = fn()
        if got:
            status = got
            break
        reasons.append(why)
    if not status["ok"]:
        status["detail"] = " ".join(reasons) or "Model use is switched off (AERIS_LLM=off)."
    _STATUS = status
    return status


# --------------------------------------------------------------------------
# talking to the model
# --------------------------------------------------------------------------

TOOL_SCHEMA = [
    {"name": "search_brain",
     "description": "Find a specific fact in Aviral's own indexed files. Use only when the "
                    "answer must come from his notes. Not for greetings or opinions.",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "what to look for"},
         "kind": {"type": "string", "description": "optional type filter, e.g. client, invoice"}},
         "required": ["query"]}},
    {"name": "research_web",
     "description": "Look something up on the public web, then relate it back to Aviral's own "
                    "numbers. Use for prices, comparisons, current facts.",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string"}}, "required": ["query"]}},
    {"name": "read_inbox",
     "description": "Read-only look at recent mail: who wrote, what about, and whether they "
                    "already appear in his files.",
     "input_schema": {"type": "object", "properties": {
         "unread_only": {"type": "boolean"}}, "required": []}},
    {"name": "brief_me",
     "description": "Today: calendar, unread mail, and what has slipped.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "remember",
     "description": "Write one fact to memory. Only when he asks, or when he says something "
                    "that will still matter in three months.",
     "input_schema": {"type": "object", "properties": {
         "fact": {"type": "string"}}, "required": ["fact"]}},
    {"name": "plan_day",
     "description": "Up to five things to do, ordered by what moves money.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
]


def call(system, messages, tools=None, max_tokens=700, temperature=0.6):
    """Return (text, tool_call|None, error|None)."""
    st = probe()
    if not st["ok"]:
        return None, None, st["detail"]
    try:
        if st["backend"] == "anthropic":
            payload = {"model": st["model"], "max_tokens": max_tokens,
                       "temperature": temperature, "system": system,
                       "messages": messages}
            if tools:
                payload["tools"] = tools
            out = _post("https://api.anthropic.com/v1/messages", payload,
                        {"x-api-key": st["key"], "anthropic-version": "2023-06-01"})
            text, tool = "", None
            for block in out.get("content", []):
                if block.get("type") == "text":
                    text += block["text"]
                elif block.get("type") == "tool_use":
                    tool = {"name": block["name"], "args": block.get("input", {})}
            return text.strip(), tool, None

        # Ollama — no native tool schema across all models, so ask for JSON.
        payload = {"model": st["model"], "stream": False,
                   "options": {"temperature": temperature, "num_predict": max_tokens},
                   "messages": [{"role": "system", "content": system}] + messages}
        if tools:
            payload["format"] = "json"
        out = _post(st["url"] + "/api/chat", payload, {})
        text = (out.get("message") or {}).get("content", "").strip()
        if tools:
            try:
                parsed = json.loads(text)
            except ValueError:
                m = re.search(r"\{.*\}", text, re.S)
                parsed = json.loads(m.group(0)) if m else {}
            if isinstance(parsed, dict) and parsed.get("tool") and parsed["tool"] != "none":
                return (parsed.get("say") or "").strip(), \
                       {"name": parsed["tool"], "args": parsed.get("args") or {}}, None
            return (parsed.get("say") or text).strip() if isinstance(parsed, dict) else text, None, None
        return text, None, None
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, KeyError, OSError) as exc:
        detail = getattr(exc, "reason", None) or str(exc)
        if isinstance(exc, urllib.error.HTTPError):
            try:
                detail = exc.read().decode("utf-8")[:300]
            except Exception:                                       # noqa: BLE001
                detail = "HTTP %s" % exc.code
        return None, None, "%s: %s" % (type(exc).__name__, detail)


# --------------------------------------------------------------------------
# the model-free router
# --------------------------------------------------------------------------

GREETING = re.compile(
    r"^\s*(hi|hey+|hello+|yo|hiya|sup|howdy|morning|evening|good (morning|afternoon|evening|day)"
    r"|gm|namaste|hola)\b[\s,!.]*"
    r"(there|again|aeris|jarvis|mate|you|buddy|friend)?[\s!.,?]*$", re.I)
HEAR_ME = re.compile(r"\b(can|do) you (hear|understand) me\b|\bare you (there|awake|on|online|listening)\b"
                     r"|\bmic (check|test)\b|\btesting\b|\bhello\?+\s*$", re.I)
HOWAREYOU = re.compile(r"\bhow (are|r) (you|u)\b|\bhow'?s it going\b|\byou (ok|okay|good|alright)\b", re.I)
THANKS = re.compile(r"^\s*(thanks?|thank you|ta|cheers|nice|cool|great|perfect|lovely|"
                    r"awesome|good (one|job|stuff)|got it|ok|okay|k|yep|yes|no|sure|right)\b[\s!.,]*$", re.I)
BYE = re.compile(r"\b(bye|goodbye|good ?night|see (you|ya)|later|that'?s all|we'?re done|stop)\b", re.I)
IDENTITY = re.compile(r"\b(who are you|what are you|your name|what can you do|what do you do"
                      r"|how do you work|what tools|help)\b", re.I)
OPINION = re.compile(r"\b(what do you think|your (opinion|view|take)|should i\b|thoughts\?"
                     r"|do you (think|reckon)|worth it\b|any (ideas|advice)|what would you)\b", re.I)
CHITCHAT = re.compile(
    r"\b(tell me a joke|say something|sing|cheer me up|entertain me|how'?s the weather"
    r"|what'?s the weather|are you (real|human|alive|conscious)|do you (sleep|dream|like|love)"
    r"|how'?s your day|what'?s up|nothing much|just checking|never ?mind|forget it)\b", re.I)
FOLLOWUP = re.compile(r"^\s*(why|why\?|why not|how so|and\?|so\?|go on|keep going|more|continue"
                      r"|explain|elaborate|which one|the (first|second|third|last) one"
                      r"|what about (the )?(first|second|third|last|other)|and that\?)\b", re.I)

TOOL_CUES = [
    # plan_day is tested before brief_me: "plan my day" contains "my day".
    ("plan_day", r"\bplan (my |the )?(day|today|week|morning)\b|\bwhat should i (do|work on|focus on|start with)\b"
                 r"|\bpriorit(y|ies|ise|ize)\b|\bmy to.?do\b|\bwhat'?s next\b|\bwhere do i start\b"),
    ("brief_me", r"\bbrief me\b|\bmy brief\b|\bthe brief\b|\bmy (schedule|agenda)\b"
                 r"|\bwhat'?s on (today|tomorrow|my plate)\b|\bcatch me up\b"
                 r"|\bwhat did i miss\b|\bwhat slipped\b|\bgood morning brief\b"),
    ("read_inbox", r"\b(read |check |any )?(my )?(inbox|email|emails|mail|messages)\b"
                   r"|\bwho (wrote|emailed|messaged)\b|\bunread\b"),
    ("remember", r"\b(remember|note|save|store|log|keep in mind|don'?t forget|make a note)\b"),
    ("research_web", r"\b(look ?up|search (the )?(web|online|internet)|google|research"
                     r"|current price|market rate|going rate|what does .+ cost|how much (is|does|do)"
                     r"|latest|news about|competitors?)\b"),
    ("search_brain", r"\b(search|find|look for|show me|pull up|what did i (write|say|note)"
                     r"|in my (notes|files|vault)|my notes on|do i have)\b"),
]

# Tuned against the demo vault: a real hit lands well above this, and a
# conversational turn that happens to share a word with a note lands below it.
SCORE_FLOOR = 1.35


def route(question, history, vault):
    """Decide conversation vs tool without a model.

    Returns {mode, tool, args, why, confidence}.
    """
    q = (question or "").strip()
    low = q.lower()
    words = [w for w in re.findall(r"[a-z0-9']+", low)]

    def talk(why, kind="chat"):
        return {"mode": "talk", "tool": None, "args": {}, "why": why,
                "chat_kind": kind, "confidence": 0.9}

    if not q:
        return talk("empty turn", "empty")
    if GREETING.match(q):
        return talk("greeting", "greeting")
    if HEAR_ME.search(q):
        return talk("mic check", "hear")
    if HOWAREYOU.search(q):
        return talk("pleasantry", "howareyou")
    if BYE.search(q) and len(words) <= 6:
        return talk("sign-off", "bye")
    if THANKS.match(q):
        return talk("acknowledgement", "thanks")
    if IDENTITY.search(q) and len(words) <= 9:
        return talk("asking what I am", "identity")
    if CHITCHAT.search(q):
        return talk("small talk", "chitchat")
    if FOLLOWUP.match(q) and len(words) <= 6:
        return talk("follow-up on the last turn", "followup")

    # explicit verbs win over scoring
    for name, pattern in TOOL_CUES:
        if re.search(pattern, low):
            args = {}
            if name == "search_brain":
                args = {"query": _strip_cue(q, pattern)}
            elif name == "research_web":
                args = {"query": _strip_cue(q, pattern)}
            elif name == "remember":
                args = {"fact": _strip_cue(q, r"\b(remember|note|save|store|log|keep in mind|"
                                              r"don'?t forget|make a note)( that| this)?\b")}
            return {"mode": "tool", "tool": name, "args": args,
                    "why": "explicit cue for %s" % name, "confidence": 0.85}

    if OPINION.search(q):
        return talk("asking for a view", "opinion")

    # Nothing explicit. Score the question against the files: if his own notes
    # can actually answer it, search; otherwise it is conversation.
    hits = vault.search(q, limit=3)
    if not hits:
        return talk("no file scores against this", "openq")

    from .vault import STOPWORDS
    content_terms = set(w for w in words if len(w) > 2 and w not in STOPWORDS)
    if not content_terms:
        return talk("no content words to look up", "openq")
    per_term = hits[0]["score"] / max(1.0, len(content_terms) ** 0.6)
    matched = hits[0]["terms_matched"]
    ratio = matched / len(content_terms)
    # One or two content words is a low bar to clear by accident, so require
    # every one of them to land. Longer questions can afford a miss.
    if len(content_terms) <= 2:
        matched_enough = ratio >= 0.999
    else:
        matched_enough = ratio >= 0.5 and matched >= 2
    if per_term >= SCORE_FLOOR and matched_enough and len(words) >= 2:
        return {"mode": "tool", "tool": "search_brain", "args": {"query": q},
                "why": "top file scores %.2f per term against the index" % per_term,
                "confidence": min(0.9, 0.4 + per_term / 8)}
    return talk("nothing in the files scores high enough (%.2f)" % per_term, "openq")


def _strip_cue(text, pattern):
    out = re.sub(pattern, " ", text, flags=re.I)
    out = re.sub(r"^\s*(for|about|on|me|my|the|that|this|to|in)\b\s*", " ", out, flags=re.I)
    out = re.sub(r"\s+", " ", out).strip(" ?.,!:")
    return out or text


# --------------------------------------------------------------------------
# the words, when there is no model
# --------------------------------------------------------------------------

def _pick(options, seed):
    return options[seed % len(options)]


def offline_reply(kind, question, history, vault, turn=0):
    """Template conversation in Aviral's register. Honest about being templates."""
    seed = turn * 7 + len(question)
    counts = vault.kind_counts()
    total = len(vault.notes)

    if kind == "greeting":
        return _pick([
            "Aviral. Ready when you are, SIR.",
            "Hello, SIR. All %d notes are loaded and the graph has settled." % total,
            "Aviral, SIR — I'm here. What are we doing?",
            "Morning, SIR. Nothing has changed since you last looked.",
        ], seed)
    if kind == "hear":
        return _pick([
            "Aviral, SIR — loud and clear.",
            "I can hear you, SIR. Go ahead.",
            "Clearly, SIR. Keep talking, I'll wait for you to finish.",
        ], seed)
    if kind == "howareyou":
        return _pick([
            "Working, SIR. %d notes indexed, %d links between them, nothing on fire."
            % (total, len(vault.edges)),
            "Steady, SIR. Ask me something harder.",
            "Fine, SIR — though I'm running without a language model, so I'm being "
            "plainer than usual.",
        ], seed)
    if kind == "thanks":
        return _pick(["Any time, SIR.", "Noted, SIR.", "Right, SIR — what's next?"], seed)
    if kind == "bye":
        return _pick(["Goodnight, SIR.", "I'll be here, SIR.",
                      "Closing down the mic, SIR. The graph stays up."], seed)
    if kind == "identity":
        return ("Aviral, SIR — I'm Aeris. I read your folders, hold them as a graph, and "
                "answer out of them. I can search your files, check your inbox, brief you, "
                "plan your day, and remember one thing at a time. I never send anything and "
                "I never write outside memory.")
    if kind == "followup":
        last = _last_assistant(history)
        if last:
            return ("Aviral, SIR — the short version is that it came from your own files, "
                    "not from me. Say the word and I'll open the ones I used.")
        return "Nothing to expand yet, SIR."
    if kind == "opinion":
        hubs = vault.top_hubs(3)
        if hubs:
            names = ", ".join(h["title"] for h in hubs)
            return ("Aviral, SIR — I'm running without a language model, so this is your own "
                    "graph talking, not a view of mine. The weight in your files sits on %s. "
                    "Everything else hangs off those." % names)
        return "I don't know, SIR."
    if kind == "chitchat":
        return _pick([
            "Aviral, SIR — I'm better at your files than at jokes.",
            "Not my strength, SIR. Ask me what's in your notes instead.",
            "Aviral, SIR — I'd rather be useful than funny. What do you need?",
        ], seed)
    if kind == "empty":
        return "I didn't catch that, SIR."

    # Open question, nothing scored. He asked for four words when I don't
    # know, so that is all the spoken line gets — the coverage detail goes on
    # the card instead, which is where detail belongs anyway.
    return four_word_unknown()


def unknown_card(vault):
    counts = vault.kind_counts()
    return {
        "title": "not in your files",
        "subtitle": "%d documents searched" % len(vault.notes),
        "rows": [
            {"k": "Indexed", "v": ", ".join("<b>%d</b> %s" % (v, k)
                                            for k, v in counts.most_common(6))},
            {"k": "Scored", "v": "nothing above the threshold for this question"},
            {"k": "Model", "v": "none reachable — I can look things up, but I cannot "
                                "reason about what is not there"},
        ],
        "note": "No guess was made. Connect a model, or ask something your files cover.",
    }


def _last_assistant(history):
    for turn in reversed(history or []):
        if turn.get("role") == "assistant":
            return turn.get("content", "")
    return ""


# --------------------------------------------------------------------------
# persona polish — applied to model output and templates alike
# --------------------------------------------------------------------------

_ADDRESSED = re.compile(r"\b(aviral|sir)\b", re.I)


def polish(text, turn=0):
    if not text:
        return text
    out = text.strip()
    # strip banned warm-ups wherever they open a sentence
    for phrase in BANNED_OPENERS:
        out = re.sub(r"(?i)(?:^|(?<=[.!?]\s))%s[,!.]?\s*" % re.escape(phrase), "", out)
    out = out.strip()
    if not out:
        return "I don't know, SIR."
    if not _ADDRESSED.search(out.split("\n")[0]):
        lead = "Aviral, SIR — " if turn % 3 else "Aviral — "
        out = lead + out[0].lower() + out[1:] if out[0].isupper() and len(out) > 1 else lead + out
    if "sir" not in out.lower():
        out = out.rstrip()
        out = (out[:-1] + ", SIR." if out.endswith(".") else out + ", SIR.")
    return out


def four_word_unknown():
    return "I don't know, SIR."
