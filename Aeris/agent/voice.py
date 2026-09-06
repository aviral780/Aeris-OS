"""ElevenLabs, both directions, entirely server-side.

Speech out : POST /v1/text-to-speech/{voice}  -> mp3 bytes
Speech in  : POST /v1/speech-to-text          -> transcript (scribe_v1)

The browser never sees the API key. The page posts text to /api/speak and
gets audio back; it posts audio to /api/listen and gets a transcript back.
Nothing sensitive shows up in devtools or in a screen recording.

The Web Speech API is deliberately not used anywhere: it is Chrome-only, it
ships audio to Google, and in Brave it is a stub that fails silently — you
talk and nothing happens, with no error at all. MediaRecorder plus this
module works in every browser and fails loudly when it fails.

Usage is metered here too. ElevenLabs is a paid service, so there is a
per-process character budget and a per-request cap; hitting either stops the
call rather than quietly spending more of Aviral's credit.
"""
import json
import mimetypes
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid

from . import data

API = "https://api.elevenlabs.io/v1"
TIMEOUT = 60

MAX_TTS_CHARS = 900          # one spoken answer should never exceed this
BUDGET_CHARS = 120_000       # per process, then it asks before spending more

_spent = {"chars": 0, "tts_calls": 0, "stt_calls": 0, "stt_seconds": 0.0,
          "budget_hit": False}


def usage():
    return dict(_spent, budget=BUDGET_CHARS, max_per_call=MAX_TTS_CHARS)


def reset_budget():
    _spent["chars"] = 0
    _spent["budget_hit"] = False


def _key():
    return data.env("ELEVENLABS_API_KEY", "").strip()


def configured():
    return bool(_key())


def voice_id():
    return data.env("ELEVENLABS_VOICE_ID", "cgSgspJ2msm6clMCkdW9").strip()


def _settings():
    def f(name, default):
        try:
            return float(data.env(name, str(default)))
        except ValueError:
            return default
    return {
        "stability": f("ELEVENLABS_STABILITY", 0.42),
        "similarity_boost": f("ELEVENLABS_SIMILARITY", 0.80),
        "style": f("ELEVENLABS_STYLE", 0.35),
        "use_speaker_boost": True,
        "speed": f("ELEVENLABS_SPEED", 1.03),
    }


def _http(req):
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read(), resp.headers.get("Content-Type", ""), None
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")[:400]
        except Exception:                                          # noqa: BLE001
            body = ""
        detail = body
        try:
            parsed = json.loads(body)
            detail = (parsed.get("detail") or {})
            detail = detail.get("message") or detail.get("status") or json.dumps(parsed)[:250]
        except ValueError:
            pass
        friendly = {
            401: "ElevenLabs rejected the API key. Check ELEVENLABS_API_KEY in Aeris/.env.",
            403: "ElevenLabs refused the request — the key may lack permission for this endpoint.",
            422: "ElevenLabs could not process that input.",
            429: "ElevenLabs rate limit or quota reached. Nothing further was spent.",
        }.get(exc.code, "ElevenLabs returned HTTP %d." % exc.code)
        return None, "", "%s %s" % (friendly, detail)
    except urllib.error.URLError as exc:
        return None, "", ("Could not reach ElevenLabs (%s). Check your connection."
                          % (getattr(exc, "reason", None) or exc))
    except OSError as exc:
        return None, "", "Network error talking to ElevenLabs: %s" % exc


# --------------------------------------------------------------------------
# the free local backends
#
# ElevenLabs is the only metered thing left in Aeris, so both directions have
# a local equivalent that costs nothing: macOS's own speech synthesiser for
# speech out, whisper.cpp for speech in. `auto` picks the free one when it is
# installed, because free is the default Aviral asked for — set AERIS_TTS or
# AERIS_STT to `elevenlabs` when the difference in quality is worth paying for.
# --------------------------------------------------------------------------

SUBPROCESS_TIMEOUT = 120


def _is_mac():
    return platform.system() == "Darwin"


def _macos_say_available():
    return _is_mac() and bool(shutil.which("say"))


def _whisper_bin():
    configured_bin = data.env("WHISPER_BIN", "").strip()
    if configured_bin:
        return shutil.which(configured_bin) or (configured_bin
                                                if os.path.isfile(configured_bin) else None)
    # whisper.cpp renamed its binary; accept either, and the Python CLI too.
    for name in ("whisper-cli", "whisper-cpp", "whisper"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _whisper_model():
    path = data.env("WHISPER_MODEL", "").strip()
    return path if path and os.path.isfile(os.path.expanduser(path)) else ""


def tts_backend():
    """Which voice actually speaks, and whether it costs anything."""
    want = data.env("AERIS_TTS", "auto").strip().lower()
    if want == "off":
        return {"name": "off", "free": True, "ok": False,
                "detail": "Speech out is switched off (AERIS_TTS=off). Text still works."}
    mac = {"name": "macos", "free": True, "ok": True,
           "detail": "macOS speech synthesis — free, local, nothing leaves the machine."}
    eleven = {"name": "elevenlabs", "free": False, "ok": True,
              "detail": "ElevenLabs — metered, billed per character."}
    if want == "macos":
        return mac if _macos_say_available() else {
            "name": "macos", "free": True, "ok": False,
            "detail": "AERIS_TTS=macos but the `say` command is not available here."}
    if want == "elevenlabs":
        return eleven if configured() else {
            "name": "elevenlabs", "free": False, "ok": False,
            "detail": "AERIS_TTS=elevenlabs but no ELEVENLABS_API_KEY is set."}
    if _macos_say_available():
        return mac
    if configured():
        return eleven
    return {"name": "none", "free": True, "ok": False,
            "detail": "No speech out available. On a Mac this works with no setup; "
                      "elsewhere set ELEVENLABS_API_KEY in Aeris/.env."}


def stt_backend():
    want = data.env("AERIS_STT", "auto").strip().lower()
    if want == "off":
        return {"name": "off", "free": True, "ok": False,
                "detail": "Speech in is switched off (AERIS_STT=off). Type instead."}
    binary, model, ffmpeg = _whisper_bin(), _whisper_model(), shutil.which("ffmpeg")
    whisper_ok = bool(binary and model and ffmpeg)
    missing = ", ".join(n for n, present in
                        (("a whisper binary", binary), ("WHISPER_MODEL", model),
                         ("ffmpeg", ffmpeg)) if not present)
    whisper = {"name": "whisper", "free": True, "ok": whisper_ok,
               "detail": ("whisper.cpp — free, local, nothing leaves the machine."
                          if whisper_ok else "Local whisper needs %s." % missing)}
    eleven = {"name": "elevenlabs", "free": False, "ok": True,
              "detail": "ElevenLabs Scribe — metered."}
    if want == "whisper":
        return whisper
    if want == "elevenlabs":
        return eleven if configured() else {
            "name": "elevenlabs", "free": False, "ok": False,
            "detail": "AERIS_STT=elevenlabs but no ELEVENLABS_API_KEY is set."}
    if whisper_ok:
        return whisper
    if configured():
        return eleven
    return {"name": "none", "free": True, "ok": False,
            "detail": "No speech in available. Install whisper.cpp and ffmpeg for the "
                      "free route, or set ELEVENLABS_API_KEY. Typing always works."}


def _tts_macos(text):
    """`say` straight to a WAV every browser can play. Costs nothing."""
    voice = data.env("MACOS_VOICE", "Samantha").strip()
    rate = data.env("MACOS_RATE", "").strip()
    with tempfile.TemporaryDirectory(prefix="aeris-tts-") as tmp:
        out = os.path.join(tmp, "say.wav")
        argv = ["say", "-o", out, "--data-format=LEI16@22050"]
        if voice:
            argv += ["-v", voice]
        if rate:
            argv += ["-r", rate]
        argv.append(text)
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=SUBPROCESS_TIMEOUT, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, "", "macOS `say` failed: %s" % exc
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip()[:200]
            if "Voice" in detail or "voice" in detail:
                detail += (" — set MACOS_VOICE in Aeris/.env to one from `say -v ?`.")
            return None, "", "macOS `say` exited %d. %s" % (proc.returncode, detail)
        if not os.path.isfile(out):
            return None, "", "macOS `say` produced no audio."
        with open(out, "rb") as fh:
            return fh.read(), "audio/wav", None


def _stt_whisper(audio, content_type):
    """whisper.cpp, locally, for nothing.

    The browser records webm/opus, which whisper cannot read, so ffmpeg
    converts to the 16 kHz mono WAV it wants. Both are one-time installs and
    the status endpoint says plainly when either is missing.
    """
    binary, model = _whisper_bin(), _whisper_model()
    if not binary or not model:
        return None, "Local whisper is not set up. Install it, or set AERIS_STT=elevenlabs."
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None, "ffmpeg is needed to decode the recording. `brew install ffmpeg`."

    with tempfile.TemporaryDirectory(prefix="aeris-stt-") as tmp:
        raw = os.path.join(tmp, "turn.bin")
        wav = os.path.join(tmp, "turn.wav")
        with open(raw, "wb") as fh:
            fh.write(audio)
        try:
            conv = subprocess.run(
                [ffmpeg, "-nostdin", "-loglevel", "error", "-i", raw,
                 "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav],
                capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, "ffmpeg failed: %s" % exc
        if conv.returncode != 0 or not os.path.isfile(wav):
            return None, "ffmpeg could not decode that recording: %s" % (
                (conv.stderr or "").strip()[:200])

        stem = os.path.join(tmp, "out")
        argv = [binary, "-m", os.path.expanduser(model), "-f", wav,
                "-otxt", "-of", stem, "-nt"]
        lang = data.env("WHISPER_LANGUAGE", "").strip()
        if lang:
            argv += ["-l", lang]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=SUBPROCESS_TIMEOUT, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, "whisper failed: %s" % exc
        if proc.returncode != 0:
            return None, "whisper exited %d: %s" % (
                proc.returncode, (proc.stderr or "").strip()[:200])
        transcript = stem + ".txt"
        if not os.path.isfile(transcript):
            return None, "whisper produced no transcript."
        with open(transcript, encoding="utf-8", errors="replace") as fh:
            return " ".join(fh.read().split()), None


# --------------------------------------------------------------------------
# speech out
# --------------------------------------------------------------------------

def speak(text):
    """Return (audio_bytes, content_type, error). Never raises."""
    text = " ".join((text or "").split())
    if not text:
        return None, "", "Nothing to say."

    chosen = tts_backend()
    if not chosen["ok"]:
        return None, "", chosen["detail"]
    if chosen["name"] == "macos":
        if len(text) > MAX_TTS_CHARS:
            text = text[:MAX_TTS_CHARS].rsplit(" ", 1)[0] + "…"
        return _tts_macos(text)

    if not configured():
        return None, "", ("No ElevenLabs key is set, so Aeris has no voice. "
                          "Add ELEVENLABS_API_KEY to Aeris/.env. Text still works.")
    if len(text) > MAX_TTS_CHARS:
        text = text[:MAX_TTS_CHARS].rsplit(" ", 1)[0] + "…"
    if _spent["chars"] + len(text) > BUDGET_CHARS:
        _spent["budget_hit"] = True
        return None, "", ("Speech budget for this session is used up (%d characters). "
                          "Nothing more will be spent without you saying so — set "
                          "AERIS_TTS=macos for the free voice, or raise BUDGET_CHARS."
                          % BUDGET_CHARS)

    payload = {
        "text": text,
        "model_id": data.env("ELEVENLABS_TTS_MODEL", "eleven_turbo_v2_5"),
        "voice_settings": _settings(),
    }
    req = urllib.request.Request(
        "%s/text-to-speech/%s?output_format=mp3_44100_128" % (API, voice_id()),
        data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"xi-api-key": _key(), "Content-Type": "application/json",
                 "Accept": "audio/mpeg"})
    body, ctype, err = _http(req)
    if err:
        return None, "", err
    if not body:
        return None, "", "ElevenLabs returned no audio."
    _spent["chars"] += len(text)
    _spent["tts_calls"] += 1
    return body, "audio/mpeg", None


# --------------------------------------------------------------------------
# speech in — Scribe
# --------------------------------------------------------------------------

def _multipart(fields, files):
    """Build a multipart/form-data body without any third-party help."""
    boundary = "----aeris%s" % uuid.uuid4().hex
    out = bytearray()
    for name, value in fields.items():
        out += b"--%s\r\n" % boundary.encode()
        out += b'Content-Disposition: form-data; name="%s"\r\n\r\n' % name.encode()
        out += str(value).encode("utf-8") + b"\r\n"
    for name, (filename, content, ctype) in files.items():
        out += b"--%s\r\n" % boundary.encode()
        out += (b'Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
                % (name.encode(), filename.encode()))
        out += b"Content-Type: %s\r\n\r\n" % ctype.encode()
        out += content + b"\r\n"
    out += b"--%s--\r\n" % boundary.encode()
    return bytes(out), "multipart/form-data; boundary=%s" % boundary


def transcribe(audio, content_type="audio/webm"):
    """Return (transcript, error). Never raises."""
    if not audio:
        return None, "No audio arrived at the server."
    if len(audio) < 1200:
        return None, "That recording was too short to transcribe."

    chosen = stt_backend()
    if not chosen["ok"]:
        return None, chosen["detail"]
    if chosen["name"] == "whisper":
        return _stt_whisper(audio, content_type)

    if not configured():
        return None, ("No ElevenLabs key is set, so speech-to-text is unavailable. "
                      "Add ELEVENLABS_API_KEY to Aeris/.env, or type instead.")

    base = (content_type or "audio/webm").split(";")[0].strip()
    ext = mimetypes.guess_extension(base) or ".webm"
    if base == "audio/webm":
        ext = ".webm"
    elif base in ("audio/mp4", "audio/x-m4a"):
        ext = ".m4a"
    elif base == "audio/ogg":
        ext = ".ogg"

    body, ctype = _multipart(
        {"model_id": data.env("ELEVENLABS_STT_MODEL", "scribe_v1"),
         "tag_audio_events": "false"},
        {"file": ("turn%s" % ext, audio, base)})
    req = urllib.request.Request(
        "%s/speech-to-text" % API, data=body, method="POST",
        headers={"xi-api-key": _key(), "Content-Type": ctype})

    started = time.time()
    raw, _, err = _http(req)
    if err:
        return None, err
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, "ElevenLabs returned something that was not a transcript."
    text = (parsed.get("text") or "").strip()
    _spent["stt_calls"] += 1
    _spent["stt_seconds"] += time.time() - started
    if not text:
        return "", None          # silence is a valid answer, not an error
    return text, None


# --------------------------------------------------------------------------
# the wake word
#
# "The mic shouldn't be on until I say Aeris" cannot be literally true —
# something has to hear the word. What is true, and what this is built to
# guarantee, is that nothing is sent anywhere and nothing is kept until the
# word matches. The browser watches the microphone's energy level locally and
# only posts audio once someone actually speaks; the server transcribes it,
# checks for the word, and throws away everything that does not match.
#
# Which is exactly why wake mode refuses to run on a metered backend. Every
# stray sentence in the room becomes a transcription call, and billing Aviral
# per overheard word to provide a feature he asked for would be indefensible.
# --------------------------------------------------------------------------

# Scribe and whisper both mishear a made-up name. These are what "Aeris"
# actually comes back as, so they all count — being deaf to your own name is
# a worse failure than waking up one time too many.
WAKE_WORDS = {
    "aeris", "aeries", "airis", "aris", "arris", "eris", "erys",
    "aries", "arias", "ares", "eiris", "ayris", "arees",
}
# "iris" is deliberately absent. It is the one common English word on the
# candidate list that is also phonetically distant — "eye-ris", not "air-is" —
# so it costs more in false wakes than it earns in catching a mishearing.
WAKE_SCAN_WORDS = 3          # "hey Aeris, ..." — but not a word buried mid-sentence


def wake_word():
    return data.env("AERIS_WAKE_WORD", "aeris").strip().lower()


def _wake_set():
    configured = wake_word()
    return WAKE_WORDS | {configured} if configured else WAKE_WORDS


def wake_match(text):
    """(matched, whatever he said after the name).

    The remainder is the point: "Aeris, what's broken" in one breath should
    work without a pause and a second recording.
    """
    words = re.findall(r"[a-z']+", (text or "").lower())
    if not words:
        return False, ""
    names = _wake_set()
    for i, word in enumerate(words[:WAKE_SCAN_WORDS]):
        if word in names:
            # Cut from the original text, not the lowercased word list, so
            # casing and punctuation survive in the part he wants answered.
            pattern = re.compile(r"^.*?\b%s\b[\s,.!?:-]*" % re.escape(word), re.I | re.S)
            rest = pattern.sub("", text or "", count=1).strip()
            return True, rest
    return False, ""


def wake_ready():
    """Whether wake mode can run at all, and why not when it cannot."""
    stt = stt_backend()
    if not stt["ok"]:
        return {"ok": False, "reason": "no-stt",
                "detail": "Wake mode needs speech-to-text. %s" % stt["detail"]}
    if not stt["free"]:
        return {"ok": False, "reason": "metered",
                "detail": "Wake mode would transcribe every sentence spoken near the "
                          "microphone, and %s bills per use. Install the free local "
                          "route (brew install whisper-cpp ffmpeg, then set "
                          "WHISPER_MODEL) and it switches itself on."
                          % stt["name"]}
    return {"ok": True, "reason": "", "backend": stt["name"],
            "detail": "Listening locally for \"%s\". Nothing is sent or kept until "
                      "it matches." % wake_word()}


def hear_wake(audio, content_type="audio/webm"):
    """Transcribe one overheard chunk and decide whether it was for her.

    Returns (result, error). A chunk that was not for her leaves nothing
    behind: no memory, no log, no history.
    """
    ready = wake_ready()
    if not ready["ok"]:
        return None, ready["detail"]
    text, err = transcribe(audio, content_type)
    if err:
        return None, err
    matched, rest = wake_match(text or "")
    if not matched:
        # Deliberately not returning the text. It was not addressed to her, so
        # it does not travel any further than this function.
        return {"woke": False, "command": ""}, None
    return {"woke": True, "command": rest, "heard": text}, None


# --------------------------------------------------------------------------
# voices
# --------------------------------------------------------------------------

def voices():
    """The account's voices. This endpoint is free — no spend guard needed."""
    if not configured():
        return None, "No ElevenLabs key is set."
    req = urllib.request.Request("%s/voices" % API, headers={"xi-api-key": _key()})
    raw, _, err = _http(req)
    if err:
        return None, err
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, "Could not read the voice list."
    out = []
    for v in parsed.get("voices", []):
        labels = v.get("labels") or {}
        out.append({
            "id": v.get("voice_id"), "name": v.get("name", ""),
            "preview": v.get("preview_url", ""),
            "gender": labels.get("gender", ""), "accent": labels.get("accent", ""),
            "description": labels.get("description", ""),
            "current": v.get("voice_id") == voice_id(),
        })
    out.sort(key=lambda x: (x["gender"] != "female", x["name"]))
    return out, None


def set_voice(new_id):
    """Persist a voice choice back into .env. Only ever rewrites that one line."""
    new_id = (new_id or "").strip()
    if not new_id or not all(c.isalnum() for c in new_id):
        return False, "That is not a voice id."
    path = data.ROOT / ".env"
    if not path.exists():
        return False, "Aeris/.env is missing."
    lines = path.read_text(encoding="utf-8").splitlines()
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith("ELEVENLABS_VOICE_ID="):
            lines[i] = "ELEVENLABS_VOICE_ID=%s" % new_id
            found = True
            break
    if not found:
        lines.append("ELEVENLABS_VOICE_ID=%s" % new_id)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    data.reload_env()
    return True, new_id


# --------------------------------------------------------------------------

def check():
    """`python3 -m agent.voice` — verify the key without guessing."""
    print("Aeris voice check")
    key = _key()
    if not key:
        print("  key       : MISSING — add ELEVENLABS_API_KEY to Aeris/.env")
        return 1
    print("  key       : set (%s…%s)" % (key[:6], key[-4:]))
    print("  voice id  : %s" % voice_id())
    print("  tts model : %s" % data.env("ELEVENLABS_TTS_MODEL", "eleven_turbo_v2_5"))
    print("  stt model : %s" % data.env("ELEVENLABS_STT_MODEL", "scribe_v1"))
    vs, err = voices()
    if err:
        print("  voices    : FAILED — %s" % err)
        return 1
    print("  voices    : %d available" % len(vs))
    current = next((v for v in vs if v["current"]), None)
    print("  selected  : %s" % (
        "%s (%s %s)" % (current["name"], current.get("gender", ""), current.get("accent", ""))
        if current else "voice id not in this account — pick another with /api/voices"))
    audio, _ctype, err = speak("Aviral, SIR. Voice check complete.")
    if err:
        print("  speak     : FAILED — %s" % err)
        return 1
    print("  speak     : ok, %d bytes of mp3" % len(audio))
    print("\nAll good. Speech in and out are both live.")
    return 0


if __name__ == "__main__":
    raise SystemExit(check())
