"""What Aeris is allowed to actually do, and how she is stopped.

The demo could only read. This module is where she gains hands, so it is
written as a gate rather than as a toolbox: every capability is registered
with a risk level, and the risk level decides whether it runs on sight or
waits for Aviral to say yes.

    LOCAL    runs immediately, is logged, and is reversible
    CONFIRM  never runs until it is approved by a second, explicit call

That split is Aviral's own rule, in code rather than in a prompt: act freely
on his own machine, stop at anything that leaves it or cannot be undone. A
prompt is a suggestion. `propose()` is not — there is no path through this
module that executes a CONFIRM action without a matching `confirm()`.

Three things are deliberately awkward here:

  * Writes are confined to roots he opts into, and every overwrite copies the
    old bytes into `.aeris-backups/` first. "Local and reversible" is only
    true if the undo actually exists.
  * `.env` and `agent/` are never LOCAL, even inside a write root. They hold
    the API key and the code enforcing these rules, so editing them is a
    decision he makes, not one she makes.
  * Commands never touch a shell. The string is split with shlex and handed
    to execve, so a filename with a semicolon in it stays a filename.

Every proposal and every outcome is appended to `audit/actions.jsonl`. If it
is not in that file, Aeris did not do it.
"""
import json
import os
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import data

ROOT = Path(__file__).resolve().parent.parent
AUDIT_DIR = ROOT / "audit"
AUDIT_LOG = AUDIT_DIR / "actions.jsonl"
BACKUP_DIRNAME = ".aeris-backups"

LOCAL = "local"          # do it now, log it
CONFIRM = "confirm"      # ask first, always

PENDING_TTL = 600        # a proposal Aviral ignored for ten minutes is stale
MAX_READ = 400_000       # bytes handed back from a single read
COMMAND_TIMEOUT = 120

_pending = {}
_lock = threading.Lock()


# --------------------------------------------------------------------------
# where she may write
# --------------------------------------------------------------------------

def write_roots():
    """Folders Aviral has opted into. Empty by default — she writes nowhere
    until he says where, and says so out loud rather than guessing."""
    raw = data.env("AERIS_WRITE_ROOTS", "")
    out = []
    for chunk in raw.split(":"):
        chunk = chunk.strip()
        if not chunk:
            continue
        p = Path(os.path.expanduser(chunk)).resolve()
        if p.is_dir():
            out.append(p)
    return out


def _protected(path):
    """Paths that are never LOCAL, even inside a write root.

    .env holds the ElevenLabs and Anthropic keys. agent/ is the code that
    enforces every rule in this file. Both are his to change, not hers.
    """
    try:
        resolved = path.resolve()
    except OSError:
        return True
    if resolved.name == ".env":
        return True
    agent_dir = (ROOT / "agent").resolve()
    return agent_dir == resolved or agent_dir in resolved.parents


def _inside_write_root(path):
    roots = write_roots()
    if not roots:
        return False, "No write roots are configured. Set AERIS_WRITE_ROOTS in Aeris/.env."
    try:
        resolved = path.resolve()
    except OSError as exc:
        return False, "Cannot resolve that path: %s" % exc
    for root in roots:
        if resolved == root or root in resolved.parents:
            return True, ""
    return False, ("%s is outside every write root. Roots are: %s"
                   % (resolved, ", ".join(str(r) for r in roots)))


def _backup(path):
    """Copy the current bytes aside before they are replaced. Returns the
    backup path, or "" when there was nothing there yet."""
    if not path.is_file():
        return ""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest_dir = path.parent / BACKUP_DIRNAME
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / ("%s.%s.bak" % (path.name, stamp))
    n = 2
    while dest.exists():
        dest = dest_dir / ("%s.%s-%d.bak" % (path.name, stamp, n))
        n += 1
    shutil.copy2(path, dest)
    return str(dest)


# --------------------------------------------------------------------------
# the audit log
# --------------------------------------------------------------------------

def _audit(event, **fields):
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    row = dict({"at": datetime.now().isoformat(timespec="seconds"), "event": event}, **fields)
    with open(AUDIT_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")
    return row


def audit_tail(limit=50):
    if not AUDIT_LOG.is_file():
        return []
    rows = []
    for line in AUDIT_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows[-limit:][::-1]


# --------------------------------------------------------------------------
# the capabilities
# --------------------------------------------------------------------------

def _ok(summary, **detail):
    return dict({"ok": True, "summary": summary}, **detail)


def _err(summary, **detail):
    return dict({"ok": False, "summary": summary}, **detail)


def read_file(path="", **_):
    p = Path(os.path.expanduser(str(path)))
    if not p.is_file():
        return _err("No file at %s." % p)
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")[:MAX_READ]
    except OSError as exc:
        return _err("Could not read %s: %s" % (p, exc))
    return _ok("Read %s, %d characters." % (p.name, len(raw)),
               path=str(p), text=raw, lines=raw.count("\n") + 1)


def list_dir(path="", **_):
    p = Path(os.path.expanduser(str(path)))
    if not p.is_dir():
        return _err("No folder at %s." % p)
    entries = []
    for child in sorted(p.iterdir())[:400]:
        if child.name == BACKUP_DIRNAME:
            continue
        entries.append({"name": child.name, "dir": child.is_dir(),
                        "size": child.stat().st_size if child.is_file() else 0})
    return _ok("%d entries in %s." % (len(entries), p.name), path=str(p), entries=entries)


def write_file(path="", content="", **_):
    p = Path(os.path.expanduser(str(path)))
    inside, why = _inside_write_root(p)
    if not inside:
        return _err(why)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        backup = _backup(p)
        p.write_text(str(content), encoding="utf-8")
    except OSError as exc:
        return _err("Could not write %s: %s" % (p, exc))
    return _ok("Wrote %d characters to %s.%s"
               % (len(str(content)), p, " Previous version kept." if backup else ""),
               path=str(p), backup=backup, created=not backup)


def delete_file(path="", **_):
    p = Path(os.path.expanduser(str(path)))
    inside, why = _inside_write_root(p)
    if not inside:
        return _err(why)
    if not p.is_file():
        return _err("No file at %s." % p)
    backup = _backup(p)
    try:
        p.unlink()
    except OSError as exc:
        return _err("Could not delete %s: %s" % (p, exc))
    return _ok("Deleted %s. The old bytes are in %s." % (p, backup), path=str(p), backup=backup)


def run_command(command="", cwd="", **_):
    """Never through a shell. The string is split and exec'd directly, so
    shell metacharacters in a filename stay part of the filename."""
    command = str(command).strip()
    if not command:
        return _err("No command given.")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return _err("Could not parse that command: %s" % exc)
    if not argv:
        return _err("No command given.")

    workdir = Path(os.path.expanduser(str(cwd))) if cwd else Path.cwd()
    if not workdir.is_dir():
        return _err("No folder at %s." % workdir)
    try:
        proc = subprocess.run(argv, cwd=str(workdir), capture_output=True,
                              text=True, timeout=COMMAND_TIMEOUT, check=False)
    except FileNotFoundError:
        return _err("No such command: %s" % argv[0])
    except subprocess.TimeoutExpired:
        return _err("%s ran past %d seconds and was stopped." % (argv[0], COMMAND_TIMEOUT))
    except OSError as exc:
        return _err("Could not run %s: %s" % (argv[0], exc))

    out = (proc.stdout or "")[-8000:]
    err = (proc.stderr or "")[-4000:]
    return dict(
        {"ok": proc.returncode == 0,
         "summary": "%s exited %d." % (argv[0], proc.returncode)},
        argv=argv, cwd=str(workdir), code=proc.returncode, stdout=out, stderr=err)


def _look_at_screen(question="", **_):
    """Imported here rather than at module load: screen.py pulls in llm.py,
    and actions.py is imported early by main.py."""
    from . import screen
    return screen.look(question)


def _github_create_issue(**args):
    from . import github
    return github.create_issue(**args)


def _github_comment(**args):
    from . import github
    return github.comment(**args)


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

class Capability:
    def __init__(self, name, fn, risk, description, outward=False):
        self.name = name
        self.fn = fn
        self.risk = risk
        self.description = description
        self.outward = outward          # does it leave the machine?


REGISTRY = {}


def register(cap):
    REGISTRY[cap.name] = cap
    return cap


register(Capability("read_file", read_file, LOCAL,
                    "Read a file from disk and hand back its text."))
register(Capability("list_dir", list_dir, LOCAL,
                    "List what is in a folder."))
register(Capability("write_file", write_file, LOCAL,
                    "Write or replace a file inside a configured write root. "
                    "The previous version is always kept."))
register(Capability("delete_file", delete_file, CONFIRM,
                    "Delete a file. Always asks first."))
register(Capability("run_command", run_command, CONFIRM,
                    "Run a command. Always asks first, and never through a shell."))
register(Capability("look_at_screen", _look_at_screen, LOCAL,
                    "Take one screenshot, describe it, and throw the image away. "
                    "Only ever happens because you asked."))
# Outward. An issue filed by mistake is visible to other people and cannot be
# quietly taken back, so neither of these ever runs without him.
register(Capability("github_create_issue", _github_create_issue, CONFIRM,
                    "Open an issue on GitHub. Always asks first.", outward=True))
register(Capability("github_comment", _github_comment, CONFIRM,
                    "Comment on a GitHub issue or pull request. Always asks first.",
                    outward=True))


def risk_of(name, args):
    """The registered risk, escalated when the specific arguments earn it."""
    cap = REGISTRY.get(name)
    if not cap:
        return CONFIRM
    if cap.risk == CONFIRM:
        return CONFIRM
    target = (args or {}).get("path")
    if name in ("write_file", "delete_file") and target:
        if _protected(Path(os.path.expanduser(str(target)))):
            return CONFIRM
    return LOCAL


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------

def _sweep():
    now = time.time()
    for token in [t for t, p in _pending.items() if now - p["at"] > PENDING_TTL]:
        _pending.pop(token, None)


def propose(name, args=None, reason=""):
    """The only way in.

    LOCAL runs here and returns its result. CONFIRM does not run: it returns
    a token and waits. There is no third path, and no argument to this
    function that turns a CONFIRM into a LOCAL.
    """
    args = dict(args or {})
    cap = REGISTRY.get(name)
    if not cap:
        return {"status": "unknown", "action": name,
                "summary": "No capability called %r." % name}

    risk = risk_of(name, args)
    _audit("proposed", action=name, args=args, risk=risk, reason=reason)

    if risk == LOCAL:
        result = cap.fn(**args)
        _audit("done", action=name, args=args, ok=result.get("ok"),
               summary=result.get("summary"))
        return {"status": "done", "action": name, "args": args,
                "risk": LOCAL, "result": result}

    token = uuid.uuid4().hex[:12]
    with _lock:
        _sweep()
        _pending[token] = {"action": name, "args": args, "at": time.time(),
                           "reason": reason}
    return {"status": "needs_confirmation", "token": token, "action": name,
            "args": args, "risk": CONFIRM, "reason": reason,
            "summary": _describe(name, args),
            "outward": cap.outward}


def _describe(name, args):
    """What Aviral is actually being asked to approve, in one line."""
    if name == "run_command":
        return "Run: %s" % args.get("command", "")
    if name == "delete_file":
        return "Delete %s" % args.get("path", "")
    if name == "write_file":
        return "Write %s" % args.get("path", "")
    if name == "github_create_issue":
        return "Open an issue on %s: “%s”" % (args.get("repo", ""), args.get("title", ""))
    if name == "github_comment":
        return "Comment on %s#%s: “%s”" % (args.get("repo", ""), args.get("number", ""),
                                           str(args.get("body", ""))[:120])
    return "%s %s" % (name, json.dumps(args, default=str)[:160])


def confirm(token, approved=True):
    with _lock:
        _sweep()
        entry = _pending.pop(token, None)
    if not entry:
        return {"status": "expired", "token": token,
                "summary": "That approval is no longer waiting — it expired or was already answered."}
    if not approved:
        _audit("denied", action=entry["action"], args=entry["args"])
        return {"status": "denied", "action": entry["action"], "args": entry["args"],
                "summary": "Not done. You said no."}

    cap = REGISTRY[entry["action"]]
    result = cap.fn(**entry["args"])
    _audit("done", action=entry["action"], args=entry["args"],
           ok=result.get("ok"), summary=result.get("summary"), approved=True)
    return {"status": "done", "action": entry["action"], "args": entry["args"],
            "risk": CONFIRM, "result": result}


def pending():
    with _lock:
        _sweep()
        return [{"token": t, "action": p["action"], "args": p["args"],
                 "summary": _describe(p["action"], p["args"]),
                 "waiting_for": int(time.time() - p["at"])}
                for t, p in _pending.items()]


def status():
    roots = write_roots()
    return {
        "capabilities": [{"name": c.name, "risk": c.risk, "description": c.description}
                         for c in REGISTRY.values()],
        "write_roots": [str(r) for r in roots],
        "can_write": bool(roots),
        "pending": len(_pending),
        "audit": str(AUDIT_LOG),
    }
