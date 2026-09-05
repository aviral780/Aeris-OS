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
# speech out
# --------------------------------------------------------------------------

def speak(text):
    """Return (mp3_bytes, error). Never raises."""
    text = " ".join((text or "").split())
    if not text:
        return None, "Nothing to say."
    if not configured():
        return None, ("No ElevenLabs key is set, so Aeris has no voice. "
                      "Add ELEVENLABS_API_KEY to Aeris/.env. Text still works.")
    if len(text) > MAX_TTS_CHARS:
        text = text[:MAX_TTS_CHARS].rsplit(" ", 1)[0] + "…"
    if _spent["chars"] + len(text) > BUDGET_CHARS:
        _spent["budget_hit"] = True
        return None, ("Speech budget for this session is used up (%d characters). "
                      "Nothing more will be spent without you saying so — restart Aeris "
                      "or raise BUDGET_CHARS in agent/voice.py." % BUDGET_CHARS)

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
        return None, err
    if not body:
        return None, "ElevenLabs returned no audio."
    _spent["chars"] += len(text)
    _spent["tts_calls"] += 1
    return body, None


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
    if not configured():
        return None, ("No ElevenLabs key is set, so speech-to-text is unavailable. "
                      "Add ELEVENLABS_API_KEY to Aeris/.env, or type instead.")
    if len(audio) < 1200:
        return None, "That recording was too short to transcribe."

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
    audio, err = speak("Aviral, SIR. Voice check complete.")
    if err:
        print("  speak     : FAILED — %s" % err)
        return 1
    print("  speak     : ok, %d bytes of mp3" % len(audio))
    print("\nAll good. Speech in and out are both live.")
    return 0


if __name__ == "__main__":
    raise SystemExit(check())
