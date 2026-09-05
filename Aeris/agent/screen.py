"""Aeris looking at the screen, only when she is asked to.

Aviral chose on-demand capture over continuous recall, so this module has no
timer, no daemon and no store. A capture happens because a question asked for
one, the image lives in a temporary directory for the length of that one
question, and the directory is removed before the answer comes back. There is
deliberately nowhere on disk for a history of his screen to accumulate.

Describing the image is free, in the same way the rest of Aeris is free: the
`claude` CLI runs on the subscription he already pays for. It is invoked with
Read as its only tool and with the temp directory as its only readable path,
so the one file it can open is the screenshot just taken. Ollama's vision
models are tried next, and the metered API only if he has explicitly set a key.

macOS asks for Screen Recording permission the first time `screencapture`
runs. If it has not been granted the capture silently produces a blank or
desktop-only image rather than an error, which is exactly the sort of quiet
failure this project refuses, so `capture` checks the result and says so.
"""
import base64
import json
import os
import platform
import shutil
import subprocess
import tempfile

from . import data, llm

CAPTURE_TIMEOUT = 30
DESCRIBE_TIMEOUT = 180

# A screenshot of a 5K display is large and the useful detail survives a
# downscale, so shrink before anything reads it.
MAX_WIDTH = 1600


def available():
    """Can this machine capture at all, and what would do it."""
    if platform.system() == "Darwin":
        binary = shutil.which("screencapture")
        return {"ok": bool(binary), "how": "screencapture", "path": binary or "",
                "detail": "macOS screencapture."
                          if binary else "screencapture is missing from this macOS install."}
    for name, argv in (("grim", ["grim"]), ("scrot", ["scrot"]),
                       ("imagemagick", ["import"])):
        found = shutil.which(argv[0])
        if found:
            return {"ok": True, "how": name, "path": found,
                    "detail": "%s on %s." % (name, platform.system())}
    return {"ok": False, "how": "", "path": "",
            "detail": "No screen capture tool on this machine (%s). On a Mac this "
                      "works with no setup." % platform.system()}


def _shrink(path):
    """Downscale in place if the machine has a tool for it. Optional — a
    full-size screenshot still works, it just costs more to read."""
    sips = shutil.which("sips")
    if sips:
        subprocess.run([sips, "-Z", str(MAX_WIDTH), path],
                       capture_output=True, timeout=CAPTURE_TIMEOUT, check=False)
        return
    magick = shutil.which("magick") or shutil.which("convert")
    if magick:
        subprocess.run([magick, path, "-resize", "%dx>" % MAX_WIDTH, path],
                       capture_output=True, timeout=CAPTURE_TIMEOUT, check=False)


def capture(into_dir):
    """Take one screenshot into `into_dir`. Returns (path, error)."""
    how = available()
    if not how["ok"]:
        return None, how["detail"]
    shot = os.path.join(into_dir, "screen.png")

    if how["how"] == "screencapture":
        argv = [how["path"], "-x", "-t", "png", shot]
    elif how["how"] == "grim":
        argv = [how["path"], shot]
    elif how["how"] == "scrot":
        argv = [how["path"], "-o", shot]
    else:
        argv = [how["path"], "-window", "root", shot]

    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=CAPTURE_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, "Screen capture failed: %s" % exc
    if proc.returncode != 0:
        return None, "Screen capture exited %d: %s" % (
            proc.returncode, (proc.stderr or "").strip()[:200])
    if not os.path.isfile(shot) or os.path.getsize(shot) < 1000:
        return None, ("The screenshot came back empty. On macOS that means Screen "
                      "Recording permission has not been granted — System Settings, "
                      "Privacy & Security, Screen Recording, and tick your terminal.")
    _shrink(shot)
    return shot, None


# --------------------------------------------------------------------------
# reading the image
# --------------------------------------------------------------------------

def _describe_claude_cli(st, shot, question):
    """Free, on the subscription. Read is the only tool it gets, and the temp
    directory holding the screenshot is the only place it may read from."""
    prompt = (
        "Read the image at %s. It is a screenshot of Aviral's screen.\n\n"
        "%s\n\nAnswer in two or three sentences, plainly, no preamble. Describe "
        "only what is actually visible. If you cannot make something out, say so "
        "rather than guessing at it." % (shot, question or "What is on this screen?"))
    argv = [st["path"], "-p", "--output-format", "text",
            "--allowed-tools", "Read", "--strict-mcp-config",
            "--add-dir", os.path.dirname(shot)]
    if st.get("model"):
        argv += ["--model", st["model"]]
    try:
        proc = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                              timeout=DESCRIBE_TIMEOUT, check=False,
                              cwd=os.path.dirname(shot))
    except subprocess.TimeoutExpired:
        return None, "Reading the screen took too long."
    except OSError as exc:
        return None, "Could not run the claude CLI: %s" % exc
    if proc.returncode != 0:
        return None, "claude CLI exited %d: %s" % (
            proc.returncode, (proc.stderr or proc.stdout or "").strip()[:200])
    text = (proc.stdout or "").strip()
    return (text, None) if text else (None, "The model returned nothing.")


def _describe_ollama(st, shot, question):
    """Local vision model. Free, and only as good as the model pulled."""
    model = data.env("OLLAMA_VISION_MODEL", "llama3.2-vision").strip()
    with open(shot, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode("ascii")
    payload = {"model": model, "stream": False,
               "messages": [{"role": "user",
                             "content": question or "What is on this screen?",
                             "images": [b64]}]}
    try:
        out = llm._post(st["url"] + "/api/chat", payload, {}, timeout=DESCRIBE_TIMEOUT)
    except Exception as exc:                                       # noqa: BLE001
        return None, ("Local vision failed (%s). Pull one with `ollama pull %s`."
                      % (type(exc).__name__, model))
    text = ((out.get("message") or {}).get("content") or "").strip()
    return (text, None) if text else (None, "The local vision model returned nothing.")


def _describe_anthropic(st, shot, question):
    with open(shot, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode("ascii")
    payload = {"model": st["model"], "max_tokens": 700, "messages": [
        {"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": b64}},
            {"type": "text", "text": question or "What is on this screen?"}]}]}
    try:
        out = llm._post("https://api.anthropic.com/v1/messages", payload,
                        {"x-api-key": st["key"], "anthropic-version": "2023-06-01"},
                        timeout=DESCRIBE_TIMEOUT)
    except Exception as exc:                                       # noqa: BLE001
        return None, "Vision call failed: %s" % type(exc).__name__
    text = "".join(b.get("text", "") for b in out.get("content", [])
                   if b.get("type") == "text").strip()
    return (text, None) if text else (None, "The model returned nothing.")


def look(question=""):
    """Capture once, read it, throw the image away. Returns a result dict.

    The temporary directory is removed on the way out whatever happens, so
    there is never a screenshot of Aviral's screen left behind.
    """
    how = available()
    if not how["ok"]:
        return {"ok": False, "summary": how["detail"]}

    found = llm.backends()["found"]
    with tempfile.TemporaryDirectory(prefix="aeris-screen-") as tmp:
        shot, err = capture(tmp)
        if err:
            return {"ok": False, "summary": err}
        size = os.path.getsize(shot)

        failures = []
        for name, fn in (("claude_cli", _describe_claude_cli),
                         ("ollama", _describe_ollama),
                         ("anthropic", _describe_anthropic)):
            if name not in found:
                continue
            text, why = fn(found[name], shot, question)
            if text:
                return {"ok": True, "summary": text, "read_by": name,
                        "free": name != "anthropic", "bytes": size,
                        "captured_by": how["how"], "retained": False}
            failures.append("%s: %s" % (name, why))

        return {"ok": False, "bytes": size,
                "summary": " ".join(failures) or
                           "Nothing here can read an image. The free route is the "
                           "`claude` CLI on your subscription; `ollama pull "
                           "llama3.2-vision` is the fully local alternative."}
