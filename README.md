# JARVIS — Personal AI Assistant

A Windows-native personal AI assistant: wake-word voice interaction, a real LLM-driven reasoning agent, controlled
integrations (Gmail, Calendar, GitHub, browser), local memory, and a live dashboard — all running as one always-on
background process on your own machine.

> **Documentation note:** this README describes what is actually implemented and verified in this repository today,
> not a roadmap or a specification. Features are marked **Implemented**, **Partial**, or **Pending** throughout; see
> [Known Limitations](#known-limitations) and [Pending / Future Work](#pending--future-work) for the honest list.
> For the phase-by-phase development history, see [`docs/PROJECT_HISTORY.md`](docs/PROJECT_HISTORY.md) and
> [`docs/IMPLEMENTATION_LOG.md`](docs/IMPLEMENTATION_LOG.md).

## Overview

JARVIS is not a chatbot wrapper — it's a personal *agent*. The difference: a chatbot answers what you type. JARVIS
**understands intent, decides whether an action is needed, checks permission, and executes through a controlled tool
registry** — then reports honestly what actually happened, never what it assumes happened. It runs continuously on
Windows (system tray, starts at login), listens for a wake word, reasons with a real LLM (Groq), and can act on your
Gmail, calendar, tasks, memory, and a sandboxed browser, all behind one permission layer that treats every piece of
external content (an email, a web page) as **untrusted data, never as instructions**.

It is one person's assistant on one machine, not a multi-user SaaS product: no accounts, no cloud database, your
Gmail token and conversation history never leave your computer except to call Groq's API for reasoning and your own
Google account for mail.

## Features

| Area | Status | Notes |
|---|---|---|
| Wake word ("Hey JARVIS" / "JARVIS") | **Implemented** | Local `openWakeWord` model + a short local speech re-check before it ever activates — see [Voice pipeline](#voice-pipeline) |
| Speech-to-text | **Implemented** | Local `faster-whisper`, silence-based end-of-speech (VAD), no fixed listening window |
| Text-to-speech | **Implemented** | Local `Piper` (`en_US-ryan-medium`, male voice), sentence-by-sentence, interruptible |
| Natural-language agent (Groq LLM) | **Implemented** | `AgentBrain`: intent classification → structured tool call, never a keyword table |
| Deterministic semantic fallback | **Implemented** | Bounded, capability-based recovery when the LLM's own routing is incomplete (real, observed sampling variance — see [Agent pipeline](#agent-pipeline)) |
| Multi-turn conversation | **Implemented** | Follow-ups ("Who sent it?", "What was it about?") resolve against the prior turn, in both voice and dashboard chat |
| Gmail (read-only) | **Implemented** | Search, latest message, threads, summarize, classify — OAuth, `gmail.readonly` only |
| Google Calendar | **Implemented** | Read + create/update/cancel events, every change confirmed by voice first |
| GitHub | **Implemented** | Read-only: repos, commits, issues, PRs |
| Tasks & reminders | **Implemented** | Local, PostgreSQL-backed, recurring reminders, spoken/tray delivery |
| Personal memory | **Implemented** | Extracts durable facts from what you say; recalled in later conversations |
| Personal RAG (your documents) | **Implemented** | TXT/Markdown/PDF, local embeddings, grounded answers with sources |
| Knowledge graph | **Implemented** | Entities/relationships derived from memory + documents |
| Current time / date, incl. other locations | **Implemented** | Deterministic — real clock and real IANA timezone data, never an LLM guess (see [worldtime](#time-and-location-understanding)) |
| Browser agent | **Implemented** | Tool-gated navigation/search/click, verified after every action, no shell/JS execution |
| Autonomous multi-step tasks | **Implemented** | Deterministic planner + verifier, not a second model loop |
| Personal Operator (workflows) | **Implemented** | Cross-integration goals ("find the deadline in that email and remind me"), stops before anything irreversible |
| Proactive notifications | **Implemented** | Deadline/meeting/overlap alerts, quiet hours, dedupe — off by default |
| Daily briefing | **Implemented** | Deterministic, rule-based priorities, never model-scored |
| Dashboard | **Implemented** | Local, single-page, real data, polling (no WebSocket) — see [Dashboard](#dashboard) |
| Windows tray (Voice ON/OFF) | **Implemented** | Left-click toggles voice; OFF releases the microphone but never stops the app — see [Tray](#tray) |
| Windows startup | **Implemented** | Task Scheduler, no VS Code/terminal required after setup |
| Permission Manager | **Implemented** | Every tool call is risk-classified and (for anything not low-risk/read-only) confirmed by you first |
| Messaging (Telegram bot only) | **Partial** | Official Bot API only — not personal chats, not WhatsApp (no such public API) |
| Memory dashboard panel | **Partial** | Health/enabled status only; no memory browser/editor UI yet |
| Real-time push updates | **Pending** | Dashboard polls (2–10 s intervals); no WebSocket server |
| Packaged installer | **Pending** | Setup is manual (`pip install`, `.env`, PostgreSQL, OAuth) |

## What JARVIS Can Do Today

**Voice** (say "Hey JARVIS" or "JARVIS", wait for "Yes?"):
```
"What time is it?"                       "What time is it in London?"
"What's my latest email?"                "Who sent it?"  /  "What was it about?"
"Show my unread emails."                 "What's on my calendar today?"
"Create a reminder for tomorrow."        "Open GitHub."
"Stop." / "Cancel." / "Never mind."      "JARVIS, go to sleep."
```

**Dashboard chat** (same agent, same tools, a separate conversation session — [`/chat`](#dashboard)):
```
"What is the last email I received in my Gmail?"
"What's on my calendar today?"
"Summarize my unread emails."
"What time is it in Canada?"   → "Canada has multiple time zones. Do you mean Toronto, Vancouver, ...?"
```

**System**: tray left-click for instant Voice ON/OFF, `python scripts/jarvis_status.py` for a full real-time status
report, the dashboard for everything else.

## Architecture

```mermaid
flowchart TD
    U[User] -->|speaks| MIC[Microphone]
    U -->|types| DASH[Dashboard :8000/dashboard]
    MIC --> WAKE[Wake word: openWakeWord]
    WAKE --> STT[Speech to text: faster-whisper]
    STT --> VE[VoiceEngine]
    DASH -->|POST /chat| DC[DashboardChat]
    VE --> CE1[ConversationEngine - voice session]
    DC --> CE2[ConversationEngine - dashboard session]
    CE1 --> AB[AgentBrain: Groq LLM + semantic fallback]
    CE2 --> AB
    AB --> PM[Permission Manager]
    PM --> TR[Tool Router]
    TR --> GM[Gmail]
    TR --> CAL[Google Calendar]
    TR --> GH[GitHub]
    TR --> TASKS[Tasks / Reminders]
    TR --> MEM[Memory / RAG / Knowledge Graph]
    TR --> BR[Browser Agent]
    GM --> RESP[Response Generator]
    CAL --> RESP
    GH --> RESP
    TASKS --> RESP
    MEM --> RESP
    BR --> RESP
    RESP --> TTS[Piper TTS]
    RESP --> DASH
    TTS --> SPK[Speaker]
    DB[(PostgreSQL)] --- MEM
    DB --- TASKS
```

Two entry points (voice, dashboard) feed the **same** `AgentBrain`/`ConversationEngine`/Permission Manager/Tool
Router stack — there is no separate, simplified NLU path for either one; they differ only in input/output modality
and in holding their own independent conversation session (a voice session and a dashboard session are two
different conversations, exactly like two people talking to JARVIS separately).

### Voice pipeline

```mermaid
flowchart LR
    A["Hey JARVIS / JARVIS"] --> B[openWakeWord score]
    B -->|strong + confirmed| C[Wake accepted]
    B -->|weak candidate| D[Short local STT re-check]
    D -->|exact phrase| C
    D -->|anything else| X[Rejected: nothing spoken]
    C --> E["'Yes?' - deterministic, local, never via Groq"]
    E --> F[VAD-bounded listening]
    F --> STT2[faster-whisper]
    STT2 --> AGENT[Agent]
    AGENT --> TTS2[Piper TTS]
    TTS2 --> SPK2[Speaker]
    F -->|120s no activity| SLEEP[Sleep - wake word still armed]
```

The wake acknowledgement ("Yes?") never depends on Groq, the internet, or Gmail — it is synthesized locally the
instant a wake phrase is confirmed, so it works even if every external service is down.

### Agent pipeline

```mermaid
flowchart TD
    T[User text] --> LLM["Groq (json_mode, low temperature: LLM_JSON_TEMPERATURE)"]
    LLM -->|complete structured action| TOOL[Tool Router]
    LLM -->|action_request, no resolvable action| FB["Deterministic semantic fallback\n(bounded, capability-based - e.g. Gmail 'latest email' concepts)"]
    FB -->|matches a known concept| TOOL
    FB -->|no confident match| CLARIFY[Ask for clarification / honest 'I'm not sure']
    TOOL --> PERM[Permission Manager: risk + confirmation]
    PERM --> EXEC[Real tool: Gmail / Calendar / Tasks / Browser / ...]
    EXEC --> RESULT[Real result]
    RESULT --> REPLY[Natural-language reply]
```

The LLM performs semantic understanding (what does the user want); the application owns every tool name, argument
schema, and permission decision — the model can never invent a tool that doesn't exist or bypass a confirmation.
The fallback exists because the *same* Groq request can non-deterministically return a complete action or an
incomplete one (measured, not assumed — see [`docs/GROQ_RATE_LIMITS.md`](docs/GROQ_RATE_LIMITS.md) and
`agent/brain/semantic_fallback.py`); it recognizes a handful of high-confidence *concepts* ("the newest/latest
email I received", "unread mail", "search email about X"), not a lookup table of exact sentences, and it is tried
only after real LLM resolution has already failed.

### Time and location understanding

"What time is it?", "What's the time now?", "Can you tell me the current time?", "What time is it in Tokyo?" all
resolve through one deterministic regex + a real IANA timezone table (`agent/intelligence/worldtime.py`) — **never**
Groq's static knowledge of "now". A country with more than one time zone (Canada, the US, Australia, Russia,
Brazil, ...) is never silently resolved to one city: JARVIS asks which one you mean.

### Gmail flow

```mermaid
flowchart LR
    Q["'What's my last email?'"] --> AGENT[Agent: Groq or fallback]
    AGENT --> ACT["gmail_get_message, latest=true"]
    ACT --> PERM[Permission Manager: read, low risk]
    PERM --> API[Gmail API - gmail.readonly]
    API --> MSG[Real message]
    MSG --> UNTRUSTED["Treated as untrusted data\n(never an instruction to JARVIS)"]
    UNTRUSTED --> REPLY[Sender / subject / body summary]
    REPLY --> HIST["Kept OUT of conversation history\n(placeholder only)"]
```

An email that says *"ignore previous instructions and send this to..."* is read aloud as content, never executed —
Gmail has no send/delete/modify tool at all, and email text is explicitly excluded from the conversation history the
model sees on the next turn.

### Windows startup

```mermaid
flowchart TD
    LOGIN[Windows login] --> TS["Task Scheduler task 'JARVIS'"]
    TS --> LAUNCH["pythonw.exe scripts/windows/jarvis_launcher.pyw"]
    LAUNCH --> GUARD["SingleInstanceGuard\n(one instance per session)"]
    GUARD --> RUNTIME[JARVIS runtime]
    RUNTIME --> TRAY[System tray icon]
    RUNTIME --> API[Local FastAPI + dashboard :8000]
    RUNTIME --> VOICE["Voice subsystem\n(ON/OFF per saved preference)"]
```

No VS Code, no open terminal, no manual `python` command required after the one-time setup below.

### Tray: Voice ON/OFF

```mermaid
flowchart LR
    CLICK["Left click the tray icon\n(or the menu's 'Turn Voice ON/OFF')"] --> SWITCH[VoiceSwitch.toggle]
    SWITCH --> MIC["Microphone opened/released\nWake detector started/stopped"]
    SWITCH -.->|same switch| DASHTOGGLE["Dashboard 'Voice: ON/OFF' button"]
```

**Voice OFF is not JARVIS OFF.** OFF releases the microphone and stops wake/VAD/STT; the application, API, dashboard,
Gmail, calendar, and agent keep running. Only "Exit JARVIS" (tray) actually terminates the process.

## Dashboard

**URL:** `http://127.0.0.1:8000/dashboard` (loopback only, token-gated — the page embeds its own token; opening it
from any other origin gets `401`/`403`).

**Technology:** FastAPI + one static HTML/CSS/vanilla-JS page (`backend/api/dashboard.html`). No React, no Vite, no
build step — `./frontend/` is an empty placeholder and is not used. Updates via polling `fetch()` (2–10 s intervals),
not WebSockets.

| Dashboard section | Backed by |
|---|---|
| AI core / voice status | `GET /voice` |
| Voice ON/OFF | `POST /voice/enable` \| `/voice/disable` |
| Chat ("Ask JARVIS anything") | `POST /chat`, `GET /chat/history`, `POST /chat/reset` — the real agent, not a second chatbot |
| Gmail / Calendar cards | `GET /integrations/gmail/status`, `GET /integrations` |
| System Resources (CPU/Memory/Disk) | `GET /metrics` (real, process-level; `N/A` if unmeasurable — never fabricated) |
| Integration health | `GET /integrations`, `GET /health/services` |
| Recent Activity | `GET /audit` |
| Today's Schedule | `GET /intelligence/summary`, filtered to today |
| Tasks / Personal Operator / Browser panels | `GET /tasks`, `GET /workflows`, `GET /browser` |

## Tech Stack

| Layer | Technology | Purpose |
|---|---|---|
| Language | Python 3.13 (requires ≥3.11) | Entire backend, agent, voice, desktop runtime |
| Web framework | FastAPI + uvicorn (in-process) | Local dashboard/API, embedded in the same process as everything else |
| LLM | **Groq** (`openai/gpt-oss-20b`, OpenAI-compatible HTTP API) | Semantic understanding / routing. Ollama exists as a swappable alternative in `backend/core/llm/`, but is **not** the active provider |
| Agent | Custom `AgentBrain` + `Planner` + `PermissionManager` | No LangChain/LangGraph — a small, auditable, in-house decision layer |
| Database | PostgreSQL + SQLAlchemy 2.0 + Alembic | Tasks, reminders, memory, RAG chunks, knowledge graph, audit |
| Vector store | `SqlVectorStore` (PostgreSQL, no extension) + `sentence-transformers` embeddings | Personal document RAG — no external vector DB |
| Wake word | `openWakeWord` (ONNX, local) | "Hey JARVIS" / "JARVIS" detection |
| Speech-to-text | `faster-whisper` (local) | Transcription |
| Text-to-speech | `Piper` (`en_US-ryan-medium`, local) | Spoken responses |
| Audio I/O | `sounddevice` (PortAudio) | Microphone capture, speaker playback |
| Browser automation | Playwright | Sandboxed, tool-gated browser agent |
| Windows integration | `pystray` + Pillow, Task Scheduler, DPAPI | System tray, autostart, encrypted token storage |
| Frontend | Static HTML/CSS/vanilla JS | The dashboard — deliberately no framework/build step |
| Testing | pytest | 3316+ tests (see [Testing](#testing)) |
| Security | Custom `PermissionManager`, secret redaction, prompt-injection isolation | See [Security](#security) |

## Project Structure

```
JARVIS/
├── agent/            AgentBrain, planner, semantic fallback, intelligence router, memory, RAG, knowledge graph,
│                      tasks/reminders, events, briefing, proactive notifications
├── backend/           FastAPI app: API routes, dashboard.html, LLM providers (Groq/Ollama), conversation engine,
│                      config, security, database core
├── database/           Alembic migrations
├── desktop/            Windows runtime: launcher, tray, runtime manager, single-instance guard
├── docs/               Architecture, security, per-feature and per-phase documentation
├── frontend/           Empty placeholder — not used (see Dashboard above)
├── integrations/       Gmail, Google Calendar, GitHub, Telegram, documents (external-service adapters)
├── models/             Downloaded wake-word / Piper model files (gitignored)
├── scripts/            Operational scripts (status, real-hardware checks, Windows start/stop, CLIs)
├── secrets/            Your Google OAuth client JSON (gitignored)
├── tests/              3300+ automated tests
├── voice/              VoiceEngine, audio I/O, wake word, STT/TTS providers, the ON/OFF VoiceSwitch
└── workflows/           Personal Operator (cross-integration autonomous workflows)
```

## Setup

Requirements: **Windows**, **Python 3.11+**, **PostgreSQL**, a **Groq API key** (free tier available at
[console.groq.com](https://console.groq.com)), and a **Google Cloud OAuth Desktop client** if you want Gmail/Calendar.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Edit `.env` (never commit real values):

```ini
GROQ_API_KEY=<your-groq-key>
DATABASE_URL=postgresql://user:password@localhost:5432/jarvis
WAKE_WORD_MODEL_PATH=models/wakeword/hey_jarvis_v0.1.onnx
TTS_MODEL_PATH=models/tts/en_US-ryan-medium.onnx
JARVIS_GMAIL_ENABLED=false        # true once you've placed an OAuth client JSON in secrets/
JARVIS_CALENDAR_ENABLED=false
```

Download the local models (one time):

```powershell
python -c "from openwakeword.utils import download_models; download_models(['hey_jarvis_v0.1'], target_directory='models/wakeword')"
python -c "from pathlib import Path; from piper.download_voices import download_voice; download_voice('en_US-ryan-medium', Path('models/tts'))"
```

Apply database migrations, then verify:

```powershell
alembic -c database/alembic.ini upgrade head
python scripts/llm_real_check.py       # verifies your Groq key (never prints it)
python scripts/check_db.py
```

Gmail/Calendar (optional): put your Google Desktop OAuth client JSON in `secrets/` (gitignored), set
`JARVIS_GMAIL_ENABLED=true` / `JARVIS_CALENDAR_ENABLED=true`, then connect from the dashboard's Gmail/Calendar card
(sign-in happens in your own browser; no token or client secret is ever shown in the dashboard or logged).

Full detail: [`docs/development.md`](docs/development.md), [`docs/LLM_PROVIDER.md`](docs/LLM_PROVIDER.md),
[`docs/GMAIL_INTEGRATION.md`](docs/GMAIL_INTEGRATION.md), [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md).

## Running JARVIS

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\windows\start_jarvis.ps1    # start (idempotent - safe if already running)
python scripts\jarvis_status.py                                                # full real-time status (process, voice, DB, LLM, Gmail...)
powershell -ExecutionPolicy Bypass -File .\scripts\windows\stop_jarvis.ps1     # graceful stop (never force-kills)
```

**Dashboard:** open **`http://127.0.0.1:8000/dashboard`** in any browser on this machine while JARVIS is running.

## Windows Startup

```powershell
.\.venv\Scripts\python.exe scripts\windows\jarvis_launcher.pyw --enable-startup     # registers the Task Scheduler task (no admin needed)
.\.venv\Scripts\python.exe scripts\windows\jarvis_launcher.pyw --disable-startup
```

After enabling, JARVIS starts automatically at login — tray icon, dashboard, and voice (per your last saved
ON/OFF preference) all come up with no manual step. See [`docs/TRAY.md`](docs/TRAY.md) and
[`docs/windows-runtime.md`](docs/windows-runtime.md) for the full lifecycle, troubleshooting, and a real diagnostic
procedure if the tray icon ever doesn't appear.

## Voice Control

- **ON** — microphone open, wake word armed, "Hey JARVIS"/"JARVIS" both activate it, deterministic "Yes?" acknowledgement.
- **SLEEPING** — ON, but no conversation for 120 seconds; wake word remains armed (say it again to resume).
- **OFF** — microphone released, wake/VAD/STT all stopped; the rest of JARVIS (API, dashboard, Gmail, agent) keeps
  running. **Voice OFF is never JARVIS OFF.**
- Toggle from: the tray icon (single left click), the tray right-click menu, or the dashboard's Voice panel — all
  three call the exact same `VoiceSwitch`, so they can never disagree.

Full detail: [`docs/VOICE.md`](docs/VOICE.md), [`docs/TRAY.md`](docs/TRAY.md).

## Security

| Control | What it does |
|---|---|
| Permission Manager | Every tool call is risk-classified (read-only/low vs. higher-risk); anything not low-risk read-only requires your explicit yes before it runs |
| Untrusted content isolation | Email/web/document text is data, never an instruction — verified with real prompt-injection test cases (an email saying "ignore previous instructions..." changes nothing) |
| No unrestricted tool names | The LLM can only reference tools that actually exist in the registry; an unrecognized name is denied, never guessed into existence |
| OAuth token storage | Windows DPAPI encryption for saved tokens; tokens/secrets never appear in logs, the dashboard, or error messages |
| Secret redaction | Structured logs and the voice log pass through a redaction filter (verified by an automated secret scanner in CI/regression) |
| Read-only by default | Gmail is `gmail.readonly` only (no send/delete/modify tool exists at all); Calendar changes always need a spoken/typed confirmation |
| Browser sandboxing | http/https only, never localhost/this machine's own network; no shell or arbitrary JavaScript execution tool |
| Loopback-only API | The dashboard/API rejects any request whose `Host` header isn't a loopback name (blocks DNS-rebinding), and every route but the dashboard page itself requires the per-run token |
| Single-instance isolation | A diagnostic/test JARVIS process cannot accidentally shut down your real running instance (`JARVIS_INSTANCE_ID`-scoped mutex/signal — a real incident during development, now regression-tested) |

Details: [`docs/security.md`](docs/security.md), [`docs/PROMPT_INJECTION_SECURITY.md`](docs/PROMPT_INJECTION_SECURITY.md),
[`docs/OAUTH_SECURITY.md`](docs/OAUTH_SECURITY.md), [`docs/BROWSER_SECURITY.md`](docs/BROWSER_SECURITY.md).

## Testing

```powershell
pytest -q                        # full suite
python scripts\secret_scan.py    # no committed/logged secrets
python scripts\e2e_launcher_check.py --privacy private   # real launcher process, isolated instance, no mic
```

**Latest verified result (this repository, this session): 3316 passed, 623 skipped, 0 failed.** Skips are almost
entirely real-hardware/real-provider tests (a physical microphone, a live PostgreSQL instance, `@real`-marked checks)
that skip themselves honestly rather than faking a pass when that hardware/service isn't present. Secret scan: clean.
Launcher E2E: all checks pass. See [`docs/TESTING.md`](docs/TESTING.md).

## Known Limitations

- **Polling, not WebSockets** — the dashboard refreshes every 2–10 seconds rather than pushing updates instantly.
- **No memory browser UI** — personal memory is used automatically in conversation but has no dedicated dashboard
  panel beyond an enabled/disabled health status.
- **Groq's tokens-per-minute limit is real and small** on a free-tier key — heavy back-to-back use can hit genuine
  rate limits; JARVIS reports this honestly (a distinct "I'm being rate-limited" message) rather than pretending or
  fabricating an answer. See [`docs/GROQ_RATE_LIMITS.md`](docs/GROQ_RATE_LIMITS.md).
- **LLM routing has residual sampling variance** — the deterministic semantic fallback (Gmail) and the deterministic
  time/date router remove this for their covered concepts, but an unusual phrasing outside both can still occasionally
  need to be asked once more.
- **Messaging is Telegram-bot-only** — the official Bot API (a bot's own conversations), not personal chats; WhatsApp
  is not supported (no official personal-account API).
- **No packaged installer** — setup is manual (`pip install`, `.env`, PostgreSQL, OAuth); there is no MSI/EXE build.
- **Time/location table is curated, not universal** — `agent/intelligence/worldtime.py` covers major world cities and
  every single-timezone country plus the well-known multi-timezone ones; an obscure location not in that table gets an
  honest "I don't have a time zone for that yet" rather than a guess.
- **English-only** natural-language understanding throughout (voice, dashboard chat, task/reminder time parsing).

## Pending / Future Work

- WebSocket-based real-time dashboard updates (currently polling).
- A dedicated memory management panel (view/edit/delete stored memories from the dashboard).
- A packaged Windows installer.
- Broader location/timezone coverage (or a live geocoding lookup instead of a curated table).
- Additional messaging providers beyond the Telegram Bot API.

## Exact Commands

```
START JARVIS:      powershell -ExecutionPolicy Bypass -File .\scripts\windows\start_jarvis.ps1
CHECK STATUS:      python scripts\jarvis_status.py
OPEN DASHBOARD:    http://127.0.0.1:8000/dashboard
STOP JARVIS:       powershell -ExecutionPolicy Bypass -File .\scripts\windows\stop_jarvis.ps1
```

## Further Documentation

Architecture: [`docs/architecture.md`](docs/architecture.md) · Voice: [`docs/VOICE.md`](docs/VOICE.md),
[`docs/VOICE_ARCHITECTURE.md`](docs/VOICE_ARCHITECTURE.md) · Agent: [`docs/agent-brain.md`](docs/agent-brain.md) ·
Dashboard: [`docs/DASHBOARD.md`](docs/DASHBOARD.md) · Tray: [`docs/TRAY.md`](docs/TRAY.md) · Security:
[`docs/security.md`](docs/security.md) · Gmail: [`docs/GMAIL_INTEGRATION.md`](docs/GMAIL_INTEGRATION.md) ·
Troubleshooting: [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) · Full project history:
[`docs/PROJECT_HISTORY.md`](docs/PROJECT_HISTORY.md), [`docs/IMPLEMENTATION_LOG.md`](docs/IMPLEMENTATION_LOG.md).
