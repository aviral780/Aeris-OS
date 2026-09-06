"""Aeris — HTTP server and API.

Standard library only. Start it with:

    python3 -m agent.main

Everything the browser can reach is here. The ElevenLabs key never crosses
this boundary: the page sends text and audio, and gets back audio and text.
"""
import json
import os
import posixpath
import re
import subprocess
import sys
import threading
import traceback
import webbrowser
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import actions, agents, data, github, google, llm, memory, notion, railway
from . import tools, voice
from . import vault as vault_mod

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui"

HISTORY_TURNS = 10
MAX_BODY = 30 * 1024 * 1024        # a long recording is still only a few MB

STATE = {
    "history": deque(maxlen=HISTORY_TURNS * 2),
    "last_card": None,
    "last_tool": None,
    "turn": 0,
    "lock": threading.Lock(),
}

ORDINALS = {"first": 0, "1st": 0, "one": 0, "second": 1, "2nd": 1, "two": 1,
            "third": 2, "3rd": 2, "three": 2, "fourth": 3, "4th": 3,
            "fifth": 4, "5th": 4, "last": -1}
ORDINAL_RE = re.compile(
    r"\b(?:the\s+)?(first|second|third|fourth|fifth|last|1st|2nd|3rd|4th|5th)\b"
    r"(?:\s+(?:one|item|file|note|result|thing))?", re.I)


# --------------------------------------------------------------------------
# system prompt
# --------------------------------------------------------------------------

def system_prompt(v, question=""):
    parts = []
    prompt_path = Path(__file__).parent / "prompt.md"
    if prompt_path.exists():
        parts.append(prompt_path.read_text(encoding="utf-8"))
    claude_md = ROOT / "CLAUDE.md"
    if claude_md.exists():
        parts.append("\n\n# Who Aviral is (CLAUDE.md, loaded every session)\n\n"
                     + claude_md.read_text(encoding="utf-8"))
    # Retrieved against this question, not the last two dozen written. What he
    # said in March about pricing should surface when he asks about pricing,
    # not only while it happens to be recent.
    mems = memory.as_context(question=question)
    if mems:
        parts.append("\n\n# What you remember that bears on this\n\n" + mems)
    parts.append(
        "\n\n# The state of his files right now\n\n"
        "- mode: %s\n- documents indexed: %d\n- links between them: %d\n- types: %s\n"
        "- biggest hubs: %s\n"
        % (data.mode_label(), len(v.notes), len(v.edges),
           ", ".join("%s (%d)" % (k, n) for k, n in v.kind_counts().most_common(8)),
           ", ".join(h["title"] for h in v.top_hubs(5))))
    return "".join(parts)


# --------------------------------------------------------------------------
# the turn
# --------------------------------------------------------------------------

def _history_messages():
    return [{"role": t["role"], "content": t["content"]} for t in STATE["history"]]


def _resolve_followup(question, v):
    """'what about the second one?' — work out what he meant.

    Uses the items on the card that is currently on screen. Without this,
    every follow-up turns into 'could you restate that', which is exactly the
    behaviour the brief rules out.
    """
    card = STATE.get("last_card") or {}
    items = [i for i in (card.get("items") or []) if i.get("id")]
    if not items:
        return None
    m = ORDINAL_RE.search(question)
    if not m:
        return None
    idx = ORDINALS.get(m.group(1).lower())
    if idx is None:
        return None
    try:
        item = items[idx]
    except IndexError:
        return {"spoken": "Aviral, SIR — there were only %d. Pick one of those."
                          % len(items), "card": None, "focus": None}
    note = v.notes.get(item["id"])
    if not note:
        return None
    payload = v.note_payload(note.id)
    body = re.sub(r"\s+", " ", re.sub(r"\[\[([^\]|]+)(\|[^\]]*)?\]\]", r"\1", note.text)).strip()
    return {
        "spoken": "Aviral, SIR — that one is %s, from %s. %s"
                  % (note.title, note.display_path, body[:200]),
        "card": {"title": "follow-up", "subtitle": note.display_path,
                 "note": "Resolved '%s' against the card that was on screen." % m.group(1),
                 "rows": [{"k": "Type", "v": note.kind},
                          {"k": "Links", "v": "%d" % note.degree},
                          {"k": "Modified", "v": note.modified}],
                 "items": [{"title": l["title"], "id": l["id"], "meta": "links to"}
                           for l in payload["links"][:5]]},
        "focus": note.id,
    }


def _model_turn(v, question, route_hint):
    """Let the model choose and phrase. Returns None if it is unavailable."""
    st = llm.probe()
    if not st["ok"]:
        return None
    system = system_prompt(v, question)
    if st["backend"] == "ollama":
        system += (
            "\n\n# Output format\n\nReply with JSON only: "
            '{"tool": "<one of search_brain, research_web, read_inbox, brief_me, '
            'remember, plan_day, or none>", "args": {...}, "say": "<what you say '
            'out loud if tool is none>"}. Most turns are conversation: use '
            '"none" unless a tool is genuinely needed.')
    messages = _history_messages() + [{"role": "user", "content": question}]
    text, tool, err = llm.call(system, messages, tools=llm.TOOL_SCHEMA)
    if err:
        return {"error": err}
    if tool and tool["name"] in tools.REGISTRY:
        result = tools.run(tool["name"], v, tool.get("args") or {})
        phrase = llm.call(
            system,
            messages + [
                {"role": "assistant", "content": "[used %s]" % tool["name"]},
                {"role": "user", "content":
                    "Tool result. Spoken draft: %s\nCard summary: %s\n\n"
                    "Say the headline in one or two sentences, out loud, in your voice. "
                    "Do not read the card. Do not repeat the draft word for word."
                    % (result["spoken"], json.dumps(result["card"])[:1800])}],
            tools=None, max_tokens=220)[0]
        spoken = phrase or result["spoken"]
        if result.get("receipt") and result["receipt"].split(":")[0] not in spoken:
            spoken = result["spoken"]          # a memory write is never paraphrased away
        return {"spoken": spoken, "card": result["card"], "tool": tool["name"],
                "by": "model", "backend": st["backend"], "model": st["model"]}
    if text:
        return {"spoken": text, "card": None, "tool": None, "by": "model",
                "backend": st["backend"], "model": st["model"]}
    return {"error": "The model returned nothing."}


def handle_turn(question):
    v = vault_mod.get()
    question = (question or "").strip()
    with STATE["lock"]:
        STATE["turn"] += 1
        turn = STATE["turn"]

    if not question:
        return {"spoken": "I didn't catch that, SIR.", "card": None, "tool": None,
                "by": "scoring", "route": "empty", "degraded": True}

    # 1. follow-ups against what is on screen, before anything else
    follow = _resolve_followup(question, v)
    if follow:
        with STATE["lock"]:
            STATE["history"].append({"role": "user", "content": question})
            STATE["history"].append({"role": "assistant", "content": follow["spoken"]})
        return {"spoken": follow["spoken"], "card": follow["card"], "tool": "follow_up",
                "focus": follow.get("focus"), "by": "context",
                "route": "resolved against the last card", "degraded": False}

    # 2. the model, if there is one
    model_error = None
    out = _model_turn(v, question, None)
    if out and "error" in out:
        model_error = out["error"]
        out = None

    # 3. no model: score the question against the files and say so
    if out is None:
        decision = llm.route(question, list(STATE["history"]), v)
        if decision["mode"] == "tool":
            result = tools.run(decision["tool"], v, decision["args"])
            out = {"spoken": result["spoken"], "card": result["card"],
                   "tool": decision["tool"], "by": "scoring"}
        else:
            kind = decision.get("chat_kind", "openq")
            out = {"spoken": llm.offline_reply(kind, question, list(STATE["history"]), v, turn),
                   "card": llm.unknown_card(v) if kind == "openq" else None,
                   "tool": None, "by": "scoring"}
        out["route"] = decision["why"]
    else:
        out.setdefault("route", "the model chose")

    out["spoken"] = llm.polish(out.get("spoken", ""), turn)
    out["degraded"] = out.get("by") != "model"
    if model_error:
        out["model_error"] = model_error

    with STATE["lock"]:
        STATE["history"].append({"role": "user", "content": question})
        STATE["history"].append({"role": "assistant", "content": out["spoken"]})
        if out.get("card"):
            STATE["last_card"] = out["card"]
            STATE["last_tool"] = out.get("tool")
    return out


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

def status_payload():
    v = vault_mod.get()
    st = llm.probe()
    tts, stt = voice.tts_backend(), voice.stt_backend()
    ok_voice = tts["ok"] or stt["ok"]
    # What is actually metered right now. Aeris is free unless one of these
    # three is the paid option, so the badge can say so without hedging.
    metered = [n for n, b in (("speech out", tts), ("speech in", stt)) if b["ok"]
               and not b["free"]]
    if st["ok"] and not st.get("free", True):
        metered.append("the model")
    return {
        "mode": data.mode_label(),
        "demo": data.is_demo(),
        "notes": len(v.notes),
        "edges": len(v.edges),
        "memories": memory.count(),
        "roots": v.roots,
        "warnings": v.warnings,
        "model": {"ok": st["ok"], "backend": st["backend"], "name": st.get("model", ""),
                  "detail": st.get("detail", ""), "free": st.get("free", True),
                  "available": st.get("available", [])},
        "voice": {"ok": ok_voice,
                  "voice_id": voice.voice_id() if tts["name"] == "elevenlabs" else "",
                  "tts": tts, "stt": stt, "wake": voice.wake_ready(),
                  "wake_word": voice.wake_word(),
                  "detail": "%s %s" % (tts["detail"], stt["detail"])
                            if ok_voice else
                            "No speech backend available — text still works. %s %s"
                            % (tts["detail"], stt["detail"])},
        "cost": {"free": not metered, "metered": metered,
                 "detail": "Nothing here is billed per use."
                           if not metered else
                           "Metered: %s. Everything else is free." % ", ".join(metered)},
        "actions": actions.status(),
        "github": github.status(),
        "railway": railway.status(),
        "google": google.status(),
        "notion": notion.status(),
        "usage": voice.usage(),
        "turn": STATE["turn"],
        "server_time": datetime.now().isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
        ".json": "application/json; charset=utf-8", ".ico": "image/x-icon"}


class Handler(BaseHTTPRequestHandler):
    server_version = "Aeris"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        path = args[0] if args else ""
        if "/api/" in str(path):
            sys.stderr.write("  %s\n" % (str(path)[:90]))

    # ---- plumbing -------------------------------------------------------
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, val in (extra or {}).items():
            self.send_header(k, val)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b""
        if n <= 0 or n > MAX_BODY:
            return b""
        return self.rfile.read(n)

    def _json_body(self):
        try:
            return json.loads(self._body().decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return {}

    def _fail(self, code, message):
        self._send(code, {"error": message})

    # ---- routes ---------------------------------------------------------
    def do_GET(self):                                             # noqa: N802
        url = urlparse(self.path)
        q = parse_qs(url.query)
        path = url.path
        try:
            if path.startswith("/api/"):
                return self._api_get(path, q)
            return self._static(path)
        except Exception:                                          # noqa: BLE001
            traceback.print_exc()
            return self._fail(500, "Server error. The terminal has the traceback.")

    def do_POST(self):                                            # noqa: N802
        path = urlparse(self.path).path
        try:
            return self._api_post(path)
        except Exception:                                          # noqa: BLE001
            traceback.print_exc()
            return self._fail(500, "Server error. The terminal has the traceback.")

    def _static(self, path):
        if path in ("/", "/index.html"):
            name = "index.html"
        else:
            name = posixpath.normpath(path).lstrip("/")
            if ".." in name or name.startswith("/"):
                return self._fail(403, "No.")
        target = (UI / name).resolve()
        if UI.resolve() not in target.parents or not target.is_file():
            return self._fail(404, "Not found.")
        ctype = MIME.get(target.suffix, "application/octet-stream")
        return self._send(200, target.read_bytes(), ctype)

    def _api_get(self, path, q):
        v = vault_mod.get()
        if path == "/api/status":
            return self._send(200, status_payload())
        if path == "/api/graph":
            return self._send(200, v.graph_payload())
        if path == "/api/note":
            payload = v.note_payload((q.get("id") or [""])[0])
            return self._send(200, payload) if payload else self._fail(404, "No such note.")
        if path == "/api/path":
            a, b = (q.get("a") or [""])[0], (q.get("b") or [""])[0]
            ids = v.path(a, b)
            return self._send(200, {
                "path": ids,
                "titles": [v.notes[i].title for i in ids],
                "hops": max(0, len(ids) - 1),
                "found": bool(ids),
            })
        if path == "/api/memory":
            return self._send(200, {"items": memory.all_memories(), "count": memory.count()})
        if path == "/api/audit":
            return self._send(200, {"items": actions.audit_tail(60),
                                    "pending": actions.pending(),
                                    "status": actions.status()})
        if path == "/api/voices":
            vs, err = voice.voices()
            return self._fail(502, err) if err else self._send(200, {"voices": vs})
        return self._fail(404, "No such endpoint.")

    def _api_post(self, path):
        if path == "/api/ask":
            payload = self._json_body()
            return self._send(200, handle_turn(payload.get("text", "")))

        if path == "/api/speak":
            payload = self._json_body()
            audio, ctype, err = voice.speak(payload.get("text", ""))
            if err:
                return self._fail(503, err)
            return self._send(200, audio, ctype or "audio/mpeg")

        if path == "/api/listen":
            audio = self._body()
            ctype = self.headers.get("Content-Type", "audio/webm")
            text, err = voice.transcribe(audio, ctype)
            if err:
                return self._fail(503, err)
            return self._send(200, {"text": text or "", "bytes": len(audio)})

        if path == "/api/wake":
            audio = self._body()
            ctype = self.headers.get("Content-Type", "audio/webm")
            heard, err = voice.hear_wake(audio, ctype)
            if err:
                return self._fail(503, err)
            # A chunk that was not addressed to her leaves nothing behind —
            # no transcript in the response, no log line, no history entry.
            return self._send(200, heard)

        if path == "/api/see":
            # A frame from a screen he chose to share. Described and dropped.
            from . import screen as screen_mod
            png = self._body()
            question = self.headers.get("X-Aeris-Question", "")
            return self._send(200, screen_mod.look_at_bytes(png, question))

        if path == "/api/voice":
            payload = self._json_body()
            ok, detail = voice.set_voice(payload.get("id", ""))
            return self._send(200, {"ok": ok, "voice_id": detail}) if ok \
                else self._fail(400, detail)

        if path == "/api/act":
            payload = self._json_body()
            name = payload.get("action", "")
            if not name:
                return self._fail(400, "No action named.")
            return self._send(200, actions.propose(
                name, payload.get("args") or {}, reason=payload.get("reason", "")))

        if path == "/api/confirm":
            payload = self._json_body()
            token = payload.get("token", "")
            if not token:
                return self._fail(400, "No approval token given.")
            return self._send(200, actions.confirm(
                token, approved=bool(payload.get("approved"))))

        if path == "/api/agent":
            payload = self._json_body()
            goal = payload.get("goal", "")
            if not goal.strip():
                return self._fail(400, "No goal given.")
            return self._send(200, agents.start(goal, vault_mod.get()))

        if path == "/api/agent/resume":
            payload = self._json_body()
            run_id = payload.get("id", "")
            if not run_id:
                return self._fail(400, "No run id given.")
            return self._send(200, agents.resume(run_id, vault_mod.get()))

        if path == "/api/reindex":
            v = vault_mod.get(refresh=True)
            return self._send(200, v.graph_payload())

        if path == "/api/reset":
            with STATE["lock"]:
                STATE["history"].clear()
                STATE["last_card"] = None
            return self._send(200, {"ok": True})

        return self._fail(404, "No such endpoint.")


# --------------------------------------------------------------------------

def ensure_demo_data():
    demo = ROOT / "data" / "demo"
    if data.is_demo() and not any(demo.rglob("*.md")):
        print("  demo vault missing — generating (fixed seed)…")
        subprocess.run([sys.executable, str(ROOT / "data" / "generate_demo.py"), "--force"],
                       check=False)


def banner(v, port):
    st = llm.probe()
    print()
    print("  \033[96mAERIS\033[0m  ·  http://localhost:%d" % port)
    print("  " + "─" * 56)
    print("  mode      %s%s" % (
        data.mode_label().upper(),
        "  (fixtures — safe to record)" if data.is_demo()
        else "  (YOUR REAL FOLDERS, read-only)"))
    print("  indexed   %d documents, %d links" % (len(v.notes), len(v.edges)))
    print("  types     %s" % ", ".join("%s %d" % (k, n) for k, n in v.kind_counts().most_common(6)))
    print("  hubs      %s" % ", ".join(h["title"] for h in v.top_hubs(4)))
    print("  memory    %d remembered %s" % (memory.count(),
                                            "fact" if memory.count() == 1 else "facts"))
    tts, stt = voice.tts_backend(), voice.stt_backend()
    print("  voice     out %s%s · in %s%s" % (
        tts["name"], "" if tts["ok"] else " \033[93m(unavailable)\033[0m",
        stt["name"], "" if stt["ok"] else " \033[93m(unavailable)\033[0m"))
    paid = [n for n, b in (("speech out", tts), ("speech in", stt))
            if b["ok"] and not b["free"]] + ([] if st.get("free", True) else ["model"])
    print("  cost      %s" % ("\033[92mfree — nothing is billed per use\033[0m" if not paid
                              else "\033[93mmetered: %s\033[0m" % ", ".join(paid)))
    roots = actions.write_roots()
    print("  hands     %s" % ("can write in %s" % ", ".join(str(r) for r in roots)
                              if roots else "\033[93mread-only — set AERIS_WRITE_ROOTS\033[0m"))
    gh = github.status()
    print("  github    %s" % (gh["detail"] if gh["ok"]
                              else "\033[93m%s\033[0m" % gh["detail"]))
    for label, mod in (("railway", railway), ("google", google), ("notion", notion)):
        st = mod.status()
        print("  %-9s %s" % (label, st["detail"] if st["ok"]
                             else "\033[93m%s\033[0m" % st["detail"]))
    print("  model     %s" % ("%s (%s)" % (st["model"], st["detail"]) if st["ok"]
                              else "\033[93mnone — routing by file scoring\033[0m"))
    for warn in v.warnings:
        print("  \033[93m!\033[0m %s" % warn)
    print("  " + "─" * 56)
    print()


def main():
    os.chdir(ROOT)
    ensure_demo_data()
    v = vault_mod.get()
    port = int(data.env("PORT", "4719"))
    banner(v, port)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    if "--no-open" not in sys.argv:
        threading.Timer(0.7, lambda: webbrowser.open("http://localhost:%d" % port)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Aeris stopped.\n")
        server.shutdown()


if __name__ == "__main__":
    main()
