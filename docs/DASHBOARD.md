# Dashboard: JARVIS Command Center

The dashboard is a single static page (`backend/api/dashboard.html`), served by the local API at
`http://127.0.0.1:<API_PORT>/dashboard` and opened by the tray's "Open dashboard" / "Voice Settings" items. There is
no separate frontend build: no React, no bundler, no `package.json` — the existing project has none, and this
redesign deliberately did not introduce one. It is one HTML file with inline CSS and vanilla JavaScript, matching the
architecture that was already there, redesigned into a dark, glassmorphic "AI command center" look.

## Why no framework

Before touching anything, the project was audited: `frontend/` is an empty placeholder (`.gitkeep` only), there is no
`package.json` anywhere, and the dashboard has always been server-rendered HTML with polling `fetch()` calls — no
WebSocket infrastructure exists either. Introducing React/a bundler/WebSockets to match the reference image's
*visual* ambitions would have been a large, unjustified architecture change for a *visual* redesign request, and
would conflict with the strict page CSP (`default-src 'self'`) already set by the server. The redesign instead sits
entirely on top of the existing REST polling architecture: same endpoints, same auth, same CSP, new look.

## Structure of `dashboard.html`

- **Design tokens**: CSS custom properties (`--jarvis-bg`, `--jarvis-cyan`, `--jarvis-panel`, `--jarvis-success`,
  etc.) at the top of the `<style>` block. The whole page is dark-only (a command-center aesthetic, not a light/dark
  toggle) with a cyan/blue accent, glassmorphic `.glass` panels (translucent + `backdrop-filter: blur`), and a
  lightweight ambient background (CSS gradients + a faint grid — no images, no canvas particle system, negligible
  CPU cost since it's static).
- **Layout**: a CSS grid shell (`.app`) — a sticky left sidebar (`.sidebar`, collapsible via a toggle button, and a
  slide-in drawer on narrow screens) and a scrolling main column. The sidebar's `NAV` array in the script drives both
  the button list and an `IntersectionObserver`-based "active section" highlight — a single page with anchor-style
  sections, not a client-side router (no new dependency for that either).
- **Hero**: the JARVIS AI core (pure CSS/SVG rings + a pulsing glow), a compact status readout, the command bar
  ("Ask JARVIS anything…"), quick actions, and a short chat log — all in one glass panel at the top of Home.
- **Sections**: Overview, Chat, Voice, Gmail, Calendar, Tasks, Memory, Projects (Personal Operator), Browser, System,
  Settings — one per sidebar item. Every panel keeps the exact element `id`s and functions the previous dashboard
  used wherever a test or another part of the codebase depends on them (see "What was preserved" below); the visual
  container around them changed, not their wiring.

## Real data only — no fabrication

Every value on the page comes from an existing (or newly added, see below) authenticated API call. Nothing is
hardcoded:

| Panel | Source |
|---|---|
| AI core state / voice status | `GET /voice` (`enabled`, `voice_state`, `microphone`, `last_error`) |
| Gmail card | `GET /integrations/gmail/status` |
| Calendar card | `GET /integrations` filtered to `name === "calendar"` (same generic integration record Gmail/GitHub/Messaging use) |
| Integration health grid / full Integrations panel | `GET /integrations` |
| System Resources (CPU / Memory / Disk / Network) | `GET /metrics` (new `cpu_percent`, `disk` fields — see below) and `GET /health/services` for the LLM reachability signal shown as "Network" |
| Memory panel | `GET /health/services` (`memory` entry) — an honest enabled/disabled status; there is no separate memory browser, so nothing beyond that is claimed |
| Recent Activity | `GET /audit` |
| Today's Schedule | `GET /intelligence/summary` (`events`, `deadlines`, `pending_tasks`), filtered client-side to today and merged chronologically |
| Runtime / privacy / services / performance | `GET /status`, `GET /privacy`, `GET /health/services`, `GET /metrics` |
| Tasks / Personal Operator / Browser panels | `GET /tasks`, `GET /workflows`, `GET /browser` (unchanged from before) |
| Chat command bar | `POST /chat` (new — see below) |

If a value can't be read, the panel shows the real error text or an honest empty state ("Calendar not connected",
"No task running", "N/A" for a metric that can't be measured on this platform) — never a fabricated number or a
default "Connected".

## New backend additions this redesign needed

The reference design asked for an "Ask JARVIS anything" command bar using *the same agent architecture as voice* —
that endpoint did not exist (only voice's STT text ever reached `ConversationEngine`), so it was added:

- **`backend/core/dashboard_chat.py`** — `DashboardChat`, a thread-safe wrapper around a `ConversationEngine` built
  by `voice.bootstrap.build_conversation_engine` (the exact function `build_voice_engine` uses internally). It is a
  **separate instance and conversation session** from voice's own, not a shared one: `ConversationEngine` is
  documented as not thread-safe and is normally driven by the single voice worker thread, so sharing voice's live
  instance across HTTP request threads would be a real concurrency bug. The dashboard's session carries the *same*
  LLM provider, the *same* registered tools (Gmail, Calendar, Tasks, Messaging, ...), the *same* `AgentBrain`
  (LLM-driven intent/tool routing — never a keyword list) and the *same* `PermissionManager`; only the conversation
  history is independent, exactly as two separate conversations naturally would be.
- **`POST /chat`** (`{"text": "..."}` → `{"ok", "reply", "intent", "tool", "error"}`), **`GET /chat/history`**,
  **`POST /chat/reset`** in `backend/api/routes/system.py`. An `LLMProviderError` is reported with the same honest,
  per-`kind` text voice uses (`describe_llm_error`, moved to `backend/core/llm/base.py` so both callers share it);
  any other exception is caught and reported generically — the dashboard command bar can never crash or fabricate a
  reply.
- **`backend/core/sysmetrics.cpu_percent()` / `disk_usage_percent()`**, wired into `GET /metrics`. Real measurements,
  stdlib only (no `psutil` dependency added): `cpu_percent()` extends the existing `CpuMeter` into a continuously
  running sampler (each call measures the interval since the previous call — the dashboard's own poll interval —
  never a blocking sleep in the request thread); `disk_usage_percent()` uses `shutil.disk_usage` on the drive JARVIS
  runs from. Both return `None`/omit the field if they can't be read, rather than a fabricated number.

## What was preserved

Every previously working piece of functionality is still wired to the same endpoints, in the same JS functions
(`loadGmail`, `loadIntegrations`, `loadVoice`, `loadBrowser`, `loadTasks`, `loadOperator`, plus the `load()` orchestrator
for status/privacy/services/intelligence/notifications/metrics/audit) — only their container markup and CSS classes
changed. This includes: the full per-integration cards (sync/disconnect/permission checkboxes), Gmail connect/
disconnect/test-connection, the voice ON/OFF switch and all its settings sliders, the autonomous task and Personal
Operator panels and their pause/resume/cancel/confirm controls, the browser open/close/stop controls, and privacy
mode switching. `tests/**/test_*` files that check for specific dashboard element ids and copy (voice, browser,
Gmail, integrations, Personal Operator panels) all still pass unmodified against the new markup.

## Voice state → AI core mapping

`renderCore(enabled, voice_state, hasError)` in the script maps the real voice snapshot to one of
`off | sleeping | listening | processing | speaking | error`, driving both the core's CSS animation state
(`data-state` attribute) and its caption text. It is never set to "listening" unless `voice_state` genuinely reports
it — there is no synthetic "always listening" animation.

## Responsiveness, accessibility, performance

- Sidebar collapses to icon-only (desktop toggle) or a slide-in drawer (< 980px); cards reflow via `grid-template-columns:
  repeat(auto-fit, minmax(...))`, no fixed pixel widths that could overflow on a small screen.
- `prefers-reduced-motion: reduce` collapses every CSS animation/transition to effectively instant.
- `:focus-visible` outlines, semantic `<button>`s throughout, `aria-live="polite"` on the chat log, `aria-label`s on
  icon-only controls (menu toggle, sidebar collapse).
- Polling intervals are unchanged from before (10s general refresh, 2–4s for voice/tasks/browser/operator/chat) —
  no new polling loop was added beyond the existing pattern, and the ambient background is static CSS (no per-frame
  JS), so idle CPU cost is unaffected by the redesign.

## Extending it

Add a new panel by: adding a `<section id="...">` in the body, an entry in the `NAV` array (icon name from the
inline `ICONS` map, or add a new one — no icon library is loaded, by design, to respect the page's CSP), and a
`loadXxx()` function following the existing `try { const d = await api(...); $("id").innerHTML = ...; } catch (e) { ... }`
pattern. If it needs new data the backend doesn't expose yet, add a route in `backend/api/routes/system.py` behind
`Depends(authorized)` (never trust the frontend to enforce anything) and keep the same "real data or an honest empty
state" rule.

## Troubleshooting

- **Dashboard shows "JARVIS is not running in this process"**: the API is being served by something other than the
  real launcher (e.g. plain `uvicorn backend.main:app`) — start JARVIS normally.
- **Command bar says "the dashboard chat is not running"**: `DashboardChat` failed to build at startup (logged as
  `Dashboard chat could not be built`); the rest of the dashboard still works. Check `logs/jarvis.log` for the
  exception type.
- **A card shows a real error message instead of data**: that's the honest failure from the underlying API call —
  fix the underlying integration/service, don't look for a dashboard-side bug first.
