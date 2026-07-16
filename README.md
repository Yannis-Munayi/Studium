# AI Course Tutor

An AI tutoring system built on the Claude API for the CSCI 3055U sample
curriculum (`sample-curriculum/`). It turns the course PDFs into structured,
self-paced lessons, teaches them YouTube-style (pause, rewind, slow down,
practice questions), and gates progression behind a mastery assessment: the
student explains each concept in their own words, Claude grades against a
rubric, and **a weighted score of at least 75% unlocks the next unit**.

## Setup

```powershell
pip install -r requirements.txt

# authenticate (either one works)
$env:ANTHROPIC_API_KEY = "sk-ant-..."   # or: ant auth login
```

## Usage

### Web UI (recommended)

```powershell
python -m tutor serve                      # then open http://127.0.0.1:8787
```

**λearn** is a course-player web app: a playlist sidebar with locked/unlocked
units, a lesson player with a chapter timeline (click to rewind), Back /
Replay / 🐢 Explain-slower controls, inline practice checks, a floating
"Prof. Owl" chat that streams answers about the current unit, and a boss-level
exam screen with an animated score ring and per-concept score cards (confetti
on a pass). XP, levels, and practice streaks are tracked per student in the
browser; real progress and grades are tracked server-side. Units that aren't
prepared yet get a one-click "Prepare this unit" button (runs ingestion).

There's also a **🔊 Listen voiceover**: toggle it in the lesson controls and
each segment is read aloud (browser speech engine — free, offline, no API
key). Pause/stop, playback speed (0.8×–1.5×), and voice selection are in the
audio bar; 🐢 slower explanations are spoken too, and each Prof. Owl chat
answer gets its own 🔊 button. Best voices: Microsoft Edge ("Natural" voices).
Math symbols are made listenable (λ is read as "lambda").

Each student picks their name on first load — that maps to the same per-student
progress files as the CLI's `--student` flag.

### Terminal CLI (same engine)

```powershell
python -m tutor units                      # list modules/units + lock state
python -m tutor ingest --module 1_turing_machine   # build unit packs (1 call/unit)
python -m tutor learn                      # study the next unlocked unit
python -m tutor status                     # progress summary
python -m tutor --student alice learn      # per-student progress tracking
```

`ingest` with no filter processes all 11 units. Each unit is one Claude call
(the unit's PDF in, a full lesson pack out) — expect very roughly $0.20–$1 per
unit depending on chapter size; the CLI prints actual token usage and cost
after each call.

### Inside a lesson

| Key      | Action                                                        |
|----------|---------------------------------------------------------------|
| `Enter`  | next segment (play)                                           |
| `b`      | previous segment (rewind)                                     |
| `r`      | show segment again (replay)                                   |
| `s`      | re-explain this segment slower/simpler (live Claude call)     |
| `a`      | ask a question about the unit (live, remembers conversation)  |
| `d`/`o`  | key definitions / learning objectives                         |
| `q`      | quit (progress is kept; lessons are replayable any time)      |

After the last segment the mastery assessment starts: you explain each rubric
concept in your own words, one grading call scores every criterion 0–2 against
the key points extracted from the textbook, and the weighted score decides
pass/fail. Failing keeps the unit unlocked for review + retake; passing
unlocks the next unit.

## How it works (the interesting parts)

- **Ingestion** (`tutor/ingest.py`) — each unit's PDF goes to Claude as a
  native document block; **structured outputs** (`messages.parse` + Pydantic)
  guarantee a valid `UnitPack`: overview, objectives, key definitions, lesson
  segments with practice questions, and a weighted grading rubric. Packs are
  stored in `coursepack/` so ingestion runs once.
- **Lessons** (`tutor/lesson.py`) — segments are served from disk (instant,
  consistent rewind/replay). Live calls (`s`, `a`, practice checking) share a
  **prompt-cached** system prefix (tutor persona + full unit content,
  `cache_control: ephemeral, ttl: 1h`), so every call after the first reads
  the unit context at ~10% of normal input price. Responses **stream** to the
  terminal.
- **Assessment** (`tutor/assess.py`) — grading runs in a *fresh* context with
  a strict examiner prompt (the friendly tutor persona never grades), uses
  **adaptive thinking**, and returns per-criterion scores as structured
  output. The 75% pass decision is computed deterministically in
  `tutor/progress.py` — the model judges criteria; the code makes the call.
- **Gating** (`tutor/progress.py`) — units unlock strictly in course order;
  progress is per-student JSON in `progress/`.

Model: `claude-opus-4-8` for everything (teaching quality is the product;
grading fairness is the experiment).

## Layout

```
sample-curriculum/material/   course source (PDF + YAML manifests) — input
tutor/                        the application
coursepack/                   generated unit packs (JSON) — created by ingest
progress/                     per-student progress — created by learn
```

## For the effectiveness study

- Grading rubrics and every attempt (per-criterion scores, timestamps) are
  plain JSON in `coursepack/` and `progress/` — easy to export for analysis
  when the professor tests the students afterwards.
- To compare cohorts, give each student their own `--student` name and diff
  their attempt histories against the prof's exam results.
