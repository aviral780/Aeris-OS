"""Bring Aeris/.env up to date without touching what is already in it.

    python3 -m agent.setup            # add missing keys, then show what to fill
    python3 -m agent.setup --check    # show only, change nothing

.env is gitignored, which is right — it holds his ElevenLabs key — but it
means a `git pull` brings a new .env.example and leaves his own .env exactly
as stale as it was. Fifteen keys have been added since he wrote his; copying
them across by hand is a typo hunt he should not have to do.

So this reads .env.example, works out which keys his .env has never heard of,
and appends them with their explanatory comments intact. Existing values are
never read, never rewritten and never printed — the file is only ever added
to. A backup is taken first regardless, because the one file in this project
that must not be lost is the one holding his keys.

Then it says plainly which settings still need a value from him and which are
fine left blank, because "add fifteen keys" is useless without "and these four
are the ones that actually matter".
"""
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
EXAMPLE = ROOT / ".env.example"

KEY_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")

# Settings that do nothing until he supplies a value, and what each unlocks.
# Anything not named here is optional or has a working default.
WANTED = {
    "GITHUB_TOKEN": "GitHub — what is failing, what is open, what you committed",
    "RAILWAY_TOKEN": "Railway — whether anything you deployed is down",
    "AERIS_WRITE_ROOTS": "lets her write files at all; blank means she cannot",
    "AERIS_VAULT_ROOTS": "your notes folder, once AERIS_DEMO=0",
    "WHISPER_MODEL": "free local speech-to-text; without it speech-in falls back to ElevenLabs",
}


def _blocks(text):
    """Split .env.example into (comments, key, value) blocks, in order."""
    out, pending = [], []
    for line in text.splitlines():
        match = KEY_LINE.match(line.strip())
        if match:
            out.append({"comment": pending, "key": match.group(1),
                        "value": match.group(2)})
            pending = []
        else:
            pending.append(line)
    return out, pending


def _keys(text):
    found = set()
    for line in text.splitlines():
        match = KEY_LINE.match(line.strip())
        if match:
            found.add(match.group(1))
    return found


def merge(write=True):
    if not EXAMPLE.is_file():
        return {"ok": False, "error": "Aeris/.env.example is missing."}

    example = EXAMPLE.read_text(encoding="utf-8")
    blocks, _trailing = _blocks(example)

    if not ENV.is_file():
        if write:
            shutil.copy2(EXAMPLE, ENV)
            os.chmod(ENV, 0o600)
        return {"ok": True, "created": True,
                "added": [b["key"] for b in blocks], "backup": "",
                "have": {}, "missing_values": list(WANTED)}

    current = ENV.read_text(encoding="utf-8")
    have = _keys(current)
    new_blocks = [b for b in blocks if b["key"] not in have]

    backup = ""
    if new_blocks and write:
        # The one file in this project that must not be lost.
        backup = str(ENV.parent / (".env.bak-%s"
                                   % datetime.now().strftime("%Y%m%d-%H%M%S")))
        shutil.copy2(ENV, backup)

        chunk = ["", "",
                 "# " + "-" * 72,
                 "# Added by `python3 -m agent.setup` on %s."
                 % datetime.now().strftime("%Y-%m-%d %H:%M"),
                 "# Nothing above this line was changed.",
                 "# " + "-" * 72]
        for block in new_blocks:
            chunk.extend(line for line in block["comment"] if line.strip())
            chunk.append("%s=%s" % (block["key"], block["value"]))
            chunk.append("")
        ENV.write_text(current.rstrip("\n") + "\n" + "\n".join(chunk) + "\n",
                       encoding="utf-8")
        os.chmod(ENV, 0o600)

    # Which of the settings that matter are still empty?
    after = ENV.read_text(encoding="utf-8") if ENV.is_file() else ""
    values = {}
    for line in after.splitlines():
        match = KEY_LINE.match(line.strip())
        if match:
            values[match.group(1)] = match.group(2).strip()
    missing = [k for k in WANTED if not values.get(k)]

    return {"ok": True, "created": False,
            "added": [b["key"] for b in new_blocks], "backup": backup,
            "have": {k: bool(v) for k, v in values.items()},
            "missing_values": missing}


def set_value(key, value):
    """Set one key in .env, in place, without disturbing anything else."""
    if not ENV.is_file():
        return False
    lines = ENV.read_text(encoding="utf-8").splitlines()
    done = False
    for i, line in enumerate(lines):
        match = KEY_LINE.match(line.strip())
        if match and match.group(1) == key:
            lines[i] = "%s=%s" % (key, value)
            done = True
            break
    if not done:
        lines.append("%s=%s" % (key, value))
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(ENV, 0o600)
    return True


def enter_secrets():
    """Type each key straight into .env, echoing nothing.

    The reason this exists: the obvious way to fill these in is to paste them
    into an AI chat and ask it to write the file. That works, and it also
    sends the key to a server and leaves it sitting in a conversation history
    — which, given Aeris imports that history into her memory, is a genuinely
    bad place for it to end up. getpass keeps every keystroke on this machine
    and out of the terminal scrollback.
    """
    import getpass

    print("\n  Nothing you type here is echoed, logged, or sent anywhere.")
    print("  Press Enter to skip any of them.\n")
    changed = []
    for key, why in WANTED.items():
        if key in ("AERIS_WRITE_ROOTS", "AERIS_VAULT_ROOTS", "WHISPER_MODEL"):
            continue                            # paths, not secrets — type them normally
        try:
            value = getpass.getpass("  %s (%s)\n  > " % (key, why)).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Stopped. Nothing further was written.")
            break
        if value:
            set_value(key, value)
            changed.append(key)
            print("  set, %d characters.\n" % len(value))
        else:
            print("  skipped.\n")
    if changed:
        print("  Written to %s (chmod 600): %s" % (ENV.name, ", ".join(changed)))
    return changed


def main(argv):
    check_only = "--check" in argv
    if "--secrets" in argv:
        print("Aeris — enter your keys")
        print("  file      %s" % ENV)
        if not ENV.is_file():
            print("  \033[91mFAILED\033[0m    run `python3 -m agent.setup` first")
            return 1
        enter_secrets()
        return 0

    print("Aeris — .env setup")
    print("  file      %s" % ENV)

    out = merge(write=not check_only)
    if not out["ok"]:
        print("  \033[91mFAILED\033[0m    %s" % out["error"])
        return 1

    if out.get("created"):
        print("  created   from .env.example, chmod 600")
    elif out["added"]:
        verb = "would add" if check_only else "added"
        print("  %-9s %d new %s: %s"
              % (verb, len(out["added"]),
                 "setting" if len(out["added"]) == 1 else "settings",
                 ", ".join(out["added"])))
        if out["backup"]:
            print("  backup    %s" % out["backup"])
    else:
        print("  up to date — nothing to add")

    print()
    if out["missing_values"]:
        print("  Still needs a value from you:")
        for key in out["missing_values"]:
            print("    \033[93m%-20s\033[0m %s" % (key, WANTED[key]))
        print()
        print("  Open it and fill those in:")
        print("    open -a TextEdit %s      # or: nano %s" % (ENV, ENV))
    else:
        print("  \033[92mEverything that needs a value has one.\033[0m")

    print()
    print("  Never printed here, and never committed: the values themselves.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
