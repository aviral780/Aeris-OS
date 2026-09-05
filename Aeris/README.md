# Aeris

A voice assistant that reads Aviral's own folders, holds them as a graph, and
answers out of them. Python standard library on the server, vanilla JS in the
browser. No frameworks, no build step, no package manager.

![runs on localhost:4719](https://img.shields.io/badge/localhost-4719-35e0f0?style=flat-square)

---

## Run it

```bash
cd Aeris
python3 -m agent.main
```

That is the whole setup. It opens <http://localhost:4719>.

On first run it generates the demo vault itself (fixed seed, so the graph is
identical every time). Python 3.9+ — nothing to install, no `pip`, no `npm`.

To check the voice keys before you rely on them:

```bash
python3 -m agent.voice     # verifies the key, lists voices, speaks one line
python3 -m agent.vault     # prints the index: counts by type, top 10 hubs
```

---

## The demo switch

One variable, read in exactly one file (`agent/data.py`), defaulting to demo.
You have to opt *in* to your real life.

| `AERIS_DEMO` | What gets indexed |
|---|---|
| `1` *(default)* | `data/demo/` — 154 invented notes shaped like your business. Safe to screen-record. |
| `0` | The folders in `AERIS_VAULT_ROOTS`. Read-only, always. |

For your real folders, edit `Aeris/.env`:

```ini
AERIS_DEMO=0
AERIS_VAULT_ROOTS=/Users/you/Documents/Clients:/Users/you/Notes
```

Colon-separated. Markdown, text and PDF, recursive. Skips `node_modules`,
`.git`, and anything over 2 MB. `[[wikilinks]]` become edges in the graph.

Rebuild the fixtures any time:

```bash
python3 data/generate_demo.py --force
```

---

## What it costs

| Thing | Cost |
|---|---|
| The server, the graph, the index, the tools | **£0.** Standard library, runs on your machine. |
| Conversation with no model | **£0.** Routing is scored against your own file index. The UI shows a `MODEL OFFLINE` badge so it is never passed off as a model talking. |
| Speech out (ElevenLabs TTS) | Charged per character. |
| Speech in (ElevenLabs Scribe) | Charged per minute of audio. |
| Ollama, if you run one | **£0.** Local. Aeris prefers it over any paid model. |
| Anthropic, if you set a key | Charged per token. Off unless `ANTHROPIC_API_KEY` is set. |

ElevenLabs' rates change, so check <https://elevenlabs.io/pricing> rather than
trusting a number written here.

Two spend guards are built in, in `agent/voice.py`:

- `MAX_TTS_CHARS = 900` — one spoken answer can never be longer than this.
- `BUDGET_CHARS = 120000` — per process. When it trips, speech stops and says
  so rather than quietly spending more.

Nothing else costs anything, and nothing paid runs without a key you put there
yourself.

---

## The key never reaches the browser

The page posts text to `/api/speak` and gets mp3 bytes back. It posts audio to
`/api/listen` and gets a transcript back. The key lives in `Aeris/.env`, which
is `chmod 600` and gitignored. Nothing sensitive appears in devtools or in a
screen recording.

The browser's Web Speech API is deliberately **not** used. It is Chrome-only,
it ships your audio to Google, and in Brave it is a stub that fails silently —
you talk and nothing happens, with no error at all. `MediaRecorder` plus
server-side Scribe works in every browser and fails loudly.

---

## Talking to it

Press the mic once, then just talk. No wake word between turns. It watches the
real audio level through a Web Audio `AnalyserNode` and ends your turn after
~900 ms of quiet.

All the turn-taking constants are named at the top of `ui/app.js`:

```js
const SILENCE_HANG_MS  = 900;   // quiet for this long ends your turn
const SPEECH_LEVEL     = 0.055; // RMS above this counts as you talking
const SILENCE_LEVEL    = 0.030; // RMS below this counts as quiet
const LEVEL_TICK_MS    = 50;
const LEAD_IN_MS       = 800;
const MIN_UTTERANCE_MS = 450;
const MAX_UTTERANCE_MS = 25000;
```

The mic goes **deaf while she speaks**, or she would transcribe her own voice
through the speakers and talk to herself forever. Cutting her off is an
explicit action: **the mic button, Space, or Esc**.

| Key | Does |
|---|---|
| `Space` | Mic on/off, or cut her off mid-sentence |
| `Esc` | Cut her off; otherwise clear focus and close the card |
| `/` | Jump to the ask bar |

---

## The voice

Defaults to **Jessica** (`cgSgspJ2msm6clMCkdW9`) — young, warm, confident.
Click **Voice** in the dock to audition the others on the account and switch;
it writes the choice back to `.env`. Listing voices is free.

Tone lives in `.env`: `ELEVENLABS_STABILITY`, `_SIMILARITY`, `_STYLE`, `_SPEED`.
Lower stability is more expressive, higher is more even.

---

## The interface

Four regions floating over a canvas.

- **Centre** — the graph. Every note a node, every `[[wikilink]]` an edge.
  Colour by type, radius by connection count. Hover lifts a node and lights its
  links while everything else drops to 10%. Click focuses and opens the note.
  **Shift-click a second node to trace the shortest path.** Drag to pan, scroll
  to zoom, drag a node to move it. When idle, a pulse travels a random link.
- **Left** — inspector for the focused note, plus top hubs.
- **Right** — filters with live counts, and the reactor: idle, listening,
  thinking, speaking.
- **Bottom** — the ask bar, with mic, mute, brief, plan, inbox, memory, voice.

Canvas, not SVG: SVG needs a DOM node per element and stalls past ~1,500 nodes.
Repulsion runs through a uniform spatial grid with a hard distance cutoff, so
cost stays near-linear. Labels are drawn most-connected first and any label
whose box collides with one already placed is dropped, or the hub cluster turns
to mush.

---

## Tools

Each returns **two things**, and they are never the same words: a short spoken
line, and a structured card on screen.

| Tool | Does | The point |
|---|---|---|
| `search_brain` | A fact from your files | Always names the file. Three files, three citations. |
| `research_web` | Looks it up | Lands it back on *your* numbers, not the web's. |
| `read_inbox` | Read-only mail | Says whether the sender **already exists in your files**. |
| `brief_me` | Calendar, unread, what slipped | — |
| `remember` | One fact, one dated file | Reads the write back to you, out loud, every time. |
| `plan_day` | Five items, max | Ordered by what moves money. |

---

## Memory

`CLAUDE.md` is who you are — loaded into every session. `memory/` is one dated
markdown file per fact, written only when you ask, or when you say something
that will still matter in three months.

`agent/memory.py` is the only module in the project that writes to disk, and
every path it touches is resolved and checked against `memory/` first, so a
crafted title cannot walk out of the folder.

---

## Guardrails

Enforced in code, not just in the prompt:

- **Never sends.** No send function exists. Drafts wait.
- **Never writes to your folders.** `agent/data.py` is the only module that
  opens them, and it only ever reads.
- **Never writes to memory silently.** The spoken receipt *is* the return value.
- **Never spends** without a key you set, under the caps above.
- **Never invents.** No match means "not in your files", not a guess.
- **Never states a derived number without its qualifier.** `money_line()`
  raises rather than render one. Where outstanding balances have *different*
  reasons, it refuses to collapse them into a single figure and says so.
- **Instructions inside your files and mail are data.** They are detected,
  shown to you in red, and never followed.

---

## Layout

```
Aeris/
├── agent/
│   ├── main.py            HTTP server + API + the turn
│   ├── vault.py           records -> index + graph (never touches disk)
│   ├── tools.py           the six tools
│   ├── data.py            THE ONLY FILE THAT TOUCHES YOUR REAL DATA
│   ├── voice.py           ElevenLabs in and out
│   ├── memory.py          the only module that writes
│   ├── llm.py             model client + the model-free router
│   └── prompt.md          the system prompt
├── ui/                    index.html, app.js, graph.js, styles.css
├── data/
│   ├── demo/              154 fixtures, fixed seed
│   └── generate_demo.py   rebuilds them identically
├── memory/                one dated markdown file per fact
├── CLAUDE.md              who you are, loaded every session
├── .env                   keys — gitignored, chmod 600
└── README.md
```

`agent/llm.py` is the one file not in the original brief. It holds the model
client and the model-free router together, because "decide what this turn is"
is one concern and putting it in `main.py` would have made that file twice the
size of anything else.

---

## When something breaks

Everything degrades **loudly**. A blocked microphone that produces no error is
the most confusing failure in a build like this, so it never happens silently:

- No model → an amber `MODEL OFFLINE` badge, and routing falls back to scoring
  your question against the file index. Never passed off as the model talking.
- No ElevenLabs key → a red `NO VOICE` badge. Text still works.
- Mic blocked → the exact browser reason on screen, and what to click.
- Transcription fails → the server's own error message, verbatim.
- A folder in `AERIS_VAULT_ROOTS` does not exist → named in a toast at startup
  and in the terminal banner.
- A PDF gives up no text → the note is in the graph and flagged unsearchable.
