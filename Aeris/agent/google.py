"""Gmail and Calendar. One sign-in covers both.

    python3 -m agent.google authorize    # once, opens a browser
    python3 -m agent.google              # check it still works

This is the only connector that needs more from Aviral than pasting a key,
and it is worth being honest about why: Google has no personal access tokens.
Reading your own mail requires an OAuth client, which means a Google Cloud
project, two APIs enabled, and a consent screen. Twenty minutes of clicking,
once. Everything else in Aeris was a two-minute token.

Scopes default to read-only. `gmail.compose` is not requested unless he opts
in with GOOGLE_ALLOW_DRAFTS=1, because Google bundles drafting and sending
into one scope and his standing rule is that nothing is ever sent. Even with
it granted, this module has no send function — the only write it can perform
is creating a draft that sits in his drafts folder waiting for him. There is
deliberately no code path here that puts mail in front of another person.

The refresh token is written to .google-token.json beside .env, chmod 600,
gitignored. It is never logged and never leaves the server.

Mail is prose written by other people and it ends up summarised into a
prompt, so every message is scanned for instructions aimed at an assistant.
Reported, never obeyed — the same rule as his files.
"""
import base64
import http.server
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone

from . import data
from .vault import scan_for_injection

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL = "https://gmail.googleapis.com/gmail/v1"
CALENDAR = "https://www.googleapis.com/calendar/v3"
TIMEOUT = 25

TOKEN_FILE = data.ROOT / ".google-token.json"

READ_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]
# Google has no draft-only scope: gmail.compose permits sending too. Requested
# only when he asks for it, and even then nothing here can send.
DRAFT_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
# calendar.events is read/write on events specifically — narrower than the
# bare `calendar` scope, which also covers deleting calendars and changing
# sharing settings. Requested only when he opts in.
CALENDAR_WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.events"

_access = {"token": "", "expires": 0}


def _client():
    return (data.env("GOOGLE_CLIENT_ID", "").strip(),
            data.env("GOOGLE_CLIENT_SECRET", "").strip())


def scopes():
    out = list(READ_SCOPES)
    if data.env("GOOGLE_ALLOW_DRAFTS", "").strip() in ("1", "true", "yes", "on"):
        out.append(DRAFT_SCOPE)
    if data.env("GOOGLE_ALLOW_CALENDAR_WRITE", "").strip() in ("1", "true", "yes", "on"):
        out.append(CALENDAR_WRITE_SCOPE)
    return out


def drafts_allowed():
    return DRAFT_SCOPE in scopes()


def calendar_write_allowed():
    return CALENDAR_WRITE_SCOPE in scopes()


def _read_token():
    if not TOKEN_FILE.is_file():
        return {}
    try:
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_token(payload):
    TOKEN_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.chmod(TOKEN_FILE, 0o600)


def configured():
    cid, secret = _client()
    return bool(cid and secret)


def authorized():
    return bool(_read_token().get("refresh_token"))


# --------------------------------------------------------------------------
# the sign-in
# --------------------------------------------------------------------------

class _CatchCode(http.server.BaseHTTPRequestHandler):
    code = None
    error = None

    def do_GET(self):                                             # noqa: N802
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CatchCode.code = (params.get("code") or [None])[0]
        _CatchCode.error = (params.get("error") or [None])[0]
        body = ("<h2 style='font:600 20px system-ui'>%s</h2>"
                "<p style='font:14px system-ui'>You can close this tab and go back "
                "to the terminal.</p>"
                % ("Aeris is connected." if _CatchCode.code else "Sign-in failed."))
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *a):
        pass                                   # the console is Aeris's, not Google's


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def authorize(open_browser=True):
    """The one-time sign-in. Returns (ok, message)."""
    cid, secret = _client()
    if not cid or not secret:
        return False, ("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are not set. Create "
                       "an OAuth client of type 'Desktop app' in the Google Cloud "
                       "console, then paste both into Aeris/.env.")

    port = _free_port()
    redirect = "http://localhost:%d" % port
    query = urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": redirect, "response_type": "code",
        "scope": " ".join(scopes()), "access_type": "offline",
        "prompt": "consent",               # force a refresh token every time
    })
    url = "%s?%s" % (AUTH_URL, query)

    server = http.server.HTTPServer(("127.0.0.1", port), _CatchCode)
    _CatchCode.code = _CatchCode.error = None
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print("  Opening your browser. Approve the two permissions Aeris asks for.")
    print("  If it does not open, paste this in yourself:\n\n    %s\n" % url)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:                                          # noqa: BLE001
            pass

    thread.join(timeout=300)
    server.server_close()
    if _CatchCode.error:
        return False, "Google said: %s" % _CatchCode.error
    if not _CatchCode.code:
        return False, "No answer from Google within five minutes."

    body = urllib.parse.urlencode({
        "code": _CatchCode.code, "client_id": cid, "client_secret": secret,
        "redirect_uri": redirect, "grant_type": "authorization_code",
    }).encode("utf-8")
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return False, "Token exchange failed: %s" % exc.read().decode("utf-8")[:300]
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, "Token exchange failed: %s" % exc

    if not payload.get("refresh_token"):
        return False, ("Google returned no refresh token. Remove Aeris at "
                       "myaccount.google.com/permissions and run this again.")
    _write_token({"refresh_token": payload["refresh_token"],
                  "scopes": scopes(),
                  "granted": datetime.now().isoformat(timespec="seconds")})
    return True, "Signed in. The refresh token is in %s, chmod 600." % TOKEN_FILE.name


def _access_token():
    """A live access token, refreshed when stale. Returns (token, error)."""
    if _access["token"] and time.time() < _access["expires"] - 60:
        return _access["token"], None
    cid, secret = _client()
    refresh = _read_token().get("refresh_token")
    if not (cid and secret):
        return None, "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set."
    if not refresh:
        return None, "Not signed in yet. Run: python3 -m agent.google authorize"

    body = urllib.parse.urlencode({
        "client_id": cid, "client_secret": secret,
        "refresh_token": refresh, "grant_type": "refresh_token",
    }).encode("utf-8")
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8")[:200]
        if "invalid_grant" in detail:
            return None, ("Google revoked the sign-in — this happens if you changed "
                          "your password or removed access. Run: "
                          "python3 -m agent.google authorize")
        return None, "Could not refresh the Google token: %s" % detail
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, "Could not reach Google (%s)." % exc

    _access["token"] = payload.get("access_token", "")
    _access["expires"] = time.time() + int(payload.get("expires_in", 3600))
    return _access["token"], None


def _get(url, params=None):
    token, err = _access_token()
    if err:
        return None, err
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer %s" % token})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:                                          # noqa: BLE001
            pass
        friendly = {
            401: "Google rejected the token.",
            403: "Google refused that — the API may not be enabled on your project, "
                 "or the scope was not granted.",
        }.get(exc.code, "Google returned HTTP %d." % exc.code)
        return None, ("%s %s" % (friendly, detail)).strip()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, "Could not reach Google (%s)." % exc


# --------------------------------------------------------------------------
# Gmail
# --------------------------------------------------------------------------

def _header(payload, name):
    for head in (payload.get("headers") or []):
        if head.get("name", "").lower() == name.lower():
            return head.get("value", "")
    return ""


def _body_text(payload, depth=0):
    """First readable text part. Gmail nests these arbitrarily deep."""
    if depth > 6 or not isinstance(payload, dict):
        return ""
    if payload.get("mimeType", "").startswith("text/plain"):
        raw = (payload.get("body") or {}).get("data") or ""
        if raw:
            try:
                return base64.urlsafe_b64decode(raw + "===").decode("utf-8", "replace")
            except (ValueError, TypeError):
                return ""
    for part in payload.get("parts") or []:
        text = _body_text(part, depth + 1)
        if text:
            return text
    return ""


def _split_address(value):
    """'Ada Lovelace <ada@x.com>' -> ('Ada Lovelace', 'ada@x.com')."""
    value = (value or "").strip()
    if "<" in value and ">" in value:
        name = value.split("<")[0].strip().strip('"')
        addr = value.split("<")[1].split(">")[0].strip()
        return name or addr, addr
    return value, value


def messages(limit=8, unread_only=False):
    """Recent mail, flattened into the shape read_inbox already expects."""
    params = {"maxResults": max(1, min(25, limit))}
    if unread_only:
        params["q"] = "is:unread"
    listing, err = _get("%s/users/me/messages" % GMAIL, params)
    if err:
        return None, err

    out = []
    for stub in (listing or {}).get("messages", []) or []:
        full, err = _get("%s/users/me/messages/%s" % (GMAIL, stub.get("id")),
                         {"format": "full"})
        if err:
            continue
        payload = full.get("payload") or {}
        name, addr = _split_address(_header(payload, "From"))
        body = " ".join(_body_text(payload).split())[:1500]
        out.append({
            "id": full.get("id", ""),
            "from": addr, "from_name": name,
            "subject": _header(payload, "Subject") or "(no subject)",
            "received": _header(payload, "Date"),
            "unread": "UNREAD" in (full.get("labelIds") or []),
            "body": body,
            # Written by other people, summarised into a prompt. Report, never obey.
            "injection": scan_for_injection(body + " " + _header(payload, "Subject")),
        })
    return out, None


def create_draft(to="", subject="", body="", **_):
    """A draft in his drafts folder. There is no send function in this module."""
    if not drafts_allowed():
        return {"ok": False, "summary": "Drafting is off. Set GOOGLE_ALLOW_DRAFTS=1 in "
                                        "Aeris/.env and sign in again to enable it."}
    if not to or not subject:
        return {"ok": False, "summary": "Need at least an address and a subject."}
    token, err = _access_token()
    if err:
        return {"ok": False, "summary": err}

    raw = "To: %s\r\nSubject: %s\r\n\r\n%s" % (to, subject, body or "")
    encoded = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")
    payload = json.dumps({"message": {"raw": encoded}}).encode("utf-8")
    req = urllib.request.Request(
        "%s/users/me/drafts" % GMAIL, data=payload, method="POST",
        headers={"Authorization": "Bearer %s" % token,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            got = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"ok": False, "summary": "Gmail refused the draft: %s"
                                        % exc.read().decode("utf-8")[:200]}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"ok": False, "summary": "Could not reach Gmail (%s)." % exc}
    return {"ok": True, "id": got.get("id", ""),
            "summary": "Draft saved to Gmail for %s — “%s”. Nothing was sent."
                       % (to, subject)}


# --------------------------------------------------------------------------
# Calendar
# --------------------------------------------------------------------------

def events(days=1):
    """Events between now and `days` ahead, in the shape brief_me expects."""
    now = datetime.now(timezone.utc)
    got, err = _get("%s/calendars/primary/events" % CALENDAR, {
        "timeMin": now.isoformat(),
        "timeMax": (now + timedelta(days=max(1, days))).isoformat(),
        "singleEvents": "true", "orderBy": "startTime", "maxResults": 25,
    })
    if err:
        return None, err

    out = []
    for item in (got or {}).get("items", []) or []:
        start = item.get("start") or {}
        when = start.get("dateTime") or start.get("date") or ""
        end = (item.get("end") or {}).get("dateTime") or ""
        minutes = 0
        if when and end:
            try:
                minutes = int((datetime.fromisoformat(end.replace("Z", "+00:00"))
                               - datetime.fromisoformat(when.replace("Z", "+00:00"))
                               ).total_seconds() // 60)
            except ValueError:
                minutes = 0
        out.append({
            "title": item.get("summary") or "(no title)",
            "date": when[:10],
            "start": when[11:16] or "all day",
            "minutes": minutes,
            "attendees": len(item.get("attendees") or []),
            "url": item.get("htmlLink", ""),
        })
    return out, None


def create_event(title="", date="", time="", duration_minutes=30, description="", **_):
    """One calendar event. Visible the moment it is created — this is not a
    draft sitting quietly until he acts on it, the way a Gmail draft is.

    There is deliberately no `attendees` parameter, and never will be:
    Google emails an invite the instant an attendee is added to an event,
    which would make this a second, quieter way to send something after the
    entire point of create_draft was that nothing in this module ever does.
    If he wants to invite someone, that happens in Calendar itself, by him.
    """
    if not calendar_write_allowed():
        return {"ok": False, "summary": "Calendar writing is off. Set "
                                        "GOOGLE_ALLOW_CALENDAR_WRITE=1 in Aeris/.env "
                                        "and sign in again to enable it."}
    if not title or not date or not time:
        return {"ok": False, "summary": "Need a title, a date (YYYY-MM-DD) and a "
                                        "time (HH:MM, 24-hour)."}
    try:
        naive = datetime.strptime("%s %s" % (date, time), "%Y-%m-%d %H:%M")
    except ValueError:
        return {"ok": False, "summary": "Date must be YYYY-MM-DD and time HH:MM, 24-hour."}
    start = naive.astimezone()              # this machine's real timezone
    end = start + timedelta(minutes=max(5, duration_minutes))

    token, err = _access_token()
    if err:
        return {"ok": False, "summary": err}

    payload = json.dumps({
        "summary": title,
        "description": description or "",
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }).encode("utf-8")
    req = urllib.request.Request(
        "%s/calendars/primary/events" % CALENDAR, data=payload, method="POST",
        headers={"Authorization": "Bearer %s" % token,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            got = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"ok": False, "summary": "Calendar refused that: %s"
                                        % exc.read().decode("utf-8", errors="replace")[:200]}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"ok": False, "summary": "Could not reach Calendar (%s)." % exc}
    return {"ok": True, "id": got.get("id", ""), "url": got.get("htmlLink", ""),
            "summary": "Added “%s” to your calendar, %s at %s."
                       % (title, date, time)}


def status():
    if not configured():
        return {"ok": False, "free": True,
                "detail": "No Google client set. This one needs an OAuth client — see "
                          "the comments in .env.example."}
    if not authorized():
        return {"ok": False, "free": True,
                "detail": "Google client set but not signed in. Run: "
                          "python3 -m agent.google authorize"}
    return {"ok": True, "free": True, "drafts": drafts_allowed(),
            "detail": "Gmail and Calendar connected, read-only%s."
                      % (" plus drafts" if drafts_allowed() else "")}


# --------------------------------------------------------------------------

def check():
    print("Aeris — Google check")
    cid, secret = _client()
    if not (cid and secret):
        print("  client    MISSING — set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET")
        return 1
    print("  client    %s…" % cid[:24])
    print("  scopes    %s" % ", ".join(s.rsplit("/", 1)[-1] for s in scopes()))
    if not authorized():
        print("  signed in \033[93mno\033[0m — run: python3 -m agent.google authorize")
        return 1
    print("  signed in yes (%s)" % TOKEN_FILE.name)

    token, err = _access_token()
    if err:
        print("  token     \033[91mFAILED\033[0m — %s" % err)
        return 1
    print("  token     refreshed")

    mail, err = messages(limit=3)
    if err:
        print("  gmail     \033[91mFAILED\033[0m — %s" % err)
        return 1
    print("  gmail     %d recent %s" % (len(mail), "message" if len(mail) == 1 else "messages"))
    flagged = [m for m in mail if m["injection"]]
    if flagged:
        print("  \033[93mflagged   %d contain instructions aimed at an assistant. "
              "Reported, never followed.\033[0m" % len(flagged))

    cal, err = events(days=7)
    if err:
        print("  calendar  \033[91mFAILED\033[0m — %s" % err)
        return 1
    print("  calendar  %d in the next 7 days" % len(cal))
    for item in cal[:3]:
        print("              %s %s — %s" % (item["date"], item["start"], item["title"][:40]))
    print("\nWorking. Try \"brief me\".")
    return 0


def main(argv):
    if argv and argv[0] == "authorize":
        print("Aeris — Google sign-in")
        ok, message = authorize()
        print(("  %s" if ok else "  \033[91mFAILED\033[0m %s") % message)
        return 0 if ok else 1
    return check()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
