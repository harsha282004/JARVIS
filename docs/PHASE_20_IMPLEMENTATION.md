# Phase 20 implementation report: controlled browser agent

Built on the working tree that still contains the uncommitted Phase 19 changes. Nothing was committed or pushed. Architecture: `BROWSER_ARCHITECTURE.md`; security: `BROWSER_SECURITY.md`; tools: `BROWSER_TOOLS.md`; configuration: `CONFIGURATION.md` (browser section); decisions and bugs found: `IMPLEMENTATION_LOG.md`.

## Files

**Created:** `browser/{__init__,models,urlsafe,scripts,driver,engine,downloads,tools,youtube,voice,control}.py`; `scripts/browser_real_check.py`; `tests/browser_helpers.py`; `tests/browser/{__init__,test_engine_urlsafe,test_tools_youtube_security,test_voice_scenarios_api_tray,test_real_browser_local}.py`; docs `BROWSER_ARCHITECTURE.md`, `BROWSER_SECURITY.md`, `BROWSER_TOOLS.md`, `PHASE_20_IMPLEMENTATION.md`.

**Modified:** `backend/core/config.py` and `.env.example` (13 `BROWSER_*` settings), `requirements.txt` (`playwright==1.63.0`), `agent/intelligence/router.py` (BrowserRouter after HubRouter), `backend/core/context.py`, `backend/api/routes/system.py` (`/browser*`), `backend/api/dashboard.html` (Browser panel), `desktop/runtime/composition.py`, `desktop/launcher/cli.py` (build, wire, shut down), `desktop/runtime/health_checks.py` (`browser_check`), `desktop/tray/tray.py` (Open/Close browser, Stop browser action), `voice/normalize.py` ("pause" is no longer a wait word), `scripts/e2e_launcher_check.py`, docs `IMPLEMENTATION_LOG.md`, `CONFIGURATION.md`, `architecture.md`, `README.md`.

## What exists

* **Browser Engine** (one browser thread, on-demand start, states closed/opening/ready/navigating/waiting/action/recovering/error/closing), **Session Manager** (session id, tabs `t1…`, active tab, last action + status), **browser state** before actions, **crash recovery** (browser, page, tab, disconnect), bounded timeouts, retries for safe operations only.
* **31 tools** with schemas, categories, timeouts and normalized results (`BROWSER_TOOLS.md`), registered through `BrowserTools` and gated by the existing `PermissionManager` + `ConfirmationEngine`.
* **Navigation safety**: http/https only; local/private/metadata/numeric-IP/DNS-rebinding targets refused; enforced again on redirects and, in the real browser, by a navigation route.
* **Semantic element targeting** (role+name, text, label, placeholder), ambiguity handling, verification for every action, idempotence (reuse a tab, "already paused").
* **YouTube** open/search/play/pause/resume/skip/seek/volume/close with scored result selection and a clarification when ambiguous; ads handled honestly.
* **Web search** (Bing; results listed, never auto-opened; human checks reported), **GitHub**: API resolves, browser opens.
* **Downloads** (contained, recorded, never executed, code types refused) and **uploads** (approved folder, always confirmed).
* **Page reading** (structured, sanitized, injection-flagged), **screenshots** (off/memory/disk; never sign-in pages), **computer-state seam**.
* **Voice/conversation**: spoken commands, context from real browser state, follow-ups ("Play the official one"), confirmations, replies only from verified results.
* **Dashboard panel, tray items, health check, metrics, redacted structured log.**

## Permission model

`browser_navigation`, `browser_read`, `browser_interaction` (LOW, policy-allowed); `browser_external_action` (MEDIUM) and `browser_sensitive_action` (HIGH) need the user's spoken yes, single use, bound to the exact action; the category comes from the user's words and from the real names of the matching page elements. Full table in `BROWSER_SECURITY.md`.

## Testing (only what was executed)

Full suite: **2615 passed, 618 skipped** (Phase 19 ended at 2428 / 618: +187 passed, all new). The skips are the PostgreSQL variants. `tests/browser`: 187 tests.

| Test set | Tests | Result |
|---|---|---|
| `test_engine_urlsafe.py`: URL safety (schemes, credentials, 18 local/private forms, DNS rebinding, shorteners, garbage), lifecycle (start/stop/restart/launch failure/shutdown), navigation (open/back/forward/refresh/404/redirect to private/timeout with bounded retries/idempotent reuse), tabs, popups, page state/reading, login/CAPTCHA reporting, find/click/verify/missing/ambiguous/disabled, typing (passwords refused), keys allow-list, scroll, wait, screenshots, downloads (verified, no overwrite, executables refused, names sanitized), uploads (folder only), crash recovery (safe read retried, click never replayed, recovery failure, tab crash), concurrency, no secrets in status/log, metrics | 96 | pass |
| `test_tools_youtube_security.py`: registry/schemas/forbidden capabilities, hostile arguments, normalized result, categories and the confirmation gate (Buy/Post/Enter/upload, hidden names, links, `confirmed` not injectable), YouTube (open/search/play/pause/resume/skip/seek/volume/close, idempotence, UI change, no playback, ads, consent, ambiguity, "the official one", empty results, offline), scoring, web search, prompt-injection pages, static security scans (no cookies/storage/shell/eval/webbrowser; only fixed scripts evaluated), findings from the real run | 45 | pass |
| `test_voice_scenarios_api_tray.py`: the **10 scenarios**, spoken general commands, unknown names not guessed, dangerous addresses by voice, GitHub via API then browser, click/type/find by voice, confirmation by voice (yes/no/unclear/single use), a full VoiceEngine run ("Open YouTube and play…", "Pause", "Resume"), API auth/fields/controls/503, dashboard panel, tray, health, computer state, config, wiring | 36 | pass |
| `test_real_browser_local.py`: **real headless Edge** against a local test website (offline): launch, navigation, verification, back/forward/refresh, 404/empty-404/unreachable, reading, tables, find/type/click/scroll/wait, downloads (PDF saved, `.exe` refused), popups, `file://` link blocked by the route, login page and password refusal, dialog, screenshot, injection page held, **HTML5 video play/pause/seek/volume verified**, real page crash, real browser disconnect recovery, close | 10 | pass |

### Real-browser and real-internet validation (executed)

`python scripts/browser_real_check.py` (headless Edge, throw-away profile, real internet):

| Step | Result |
|---|---|
| open_youtube | verified, 7–9 s |
| search_youtube "Blinding Lights" | 4–5 results; official video ranked first |
| play_youtube (unambiguous only) | first run: **asked "Which one do you mean?"** (official video and official audio tied); after the tie-break fix: picked "The Weeknd - Blinding Lights (Official Video)", verified playing ("An ad is playing first…") |
| pause / pause again / resume | verified; second pause "It's already paused." |
| volume 30 % | verified |
| seek +20 s during an ad | refused honestly ("ads can't be skipped through") |
| skip_youtube | first run: "I pressed skip, but the ad is still playing" (hidden button); after the fix: "Skipped the ad." |
| close_youtube | closed |
| open github.com, get_page_state, read_page | verified; headings read; no injection flag |
| web_search "PostgreSQL pgvector" | first run (DuckDuckGo): human-check, no results; after switching to Bing: 10 results |
| file:// and localhost | refused without starting anything |

`python scripts/e2e_launcher_check.py` (real launcher process, real models, real headless Edge on request), private and active: browser closed until asked; `/browser` shows state only; opened on request (≈1.9 s startup, real tab); health shows `browser` correctly (closed = disabled "opens when you ask", open = healthy); status contains no secrets; **graceful exit code 0 with no browser process left on the JARVIS profile**; port released; no error log lines. Active mode idle: 0.65 % CPU, 489 MB. Timings: browser startup ≈1.9 s, navigation ≈3.7–4.2 s (network bound), action ≈1.7–2.0 s mean, element lookup ≈0.3 s, verification ≈0.07–0.15 s.

## Known limitations

1. **Voice → browser was tested with scripted microphone/STT and a fake web** (and the real browser through the tools/router separately), not with a spoken sentence controlling real YouTube. No human spoke to it.
2. **Headed mode, audible playback and a signed-in YouTube/GitHub session were not exercised.** Real runs were headless (silent), with a throw-away profile and no account. Consent pages (EU) and other regional variants of YouTube are handled only by detection ("please choose yourself").
3. **The LLM planner is not connected to the browser tools** (deterministic patterns only): unusual wording does not reach them. Descriptors exist for a follow-up.
4. **Live sites change.** YouTube result cards and the player are read with selectors that worked on the day of the run; a redesign yields an honest failure, not a false success. Search relies on Bing's markup (DuckDuckGo shows automation a human-check).
5. **Category detection is word-based**: an icon-only submit button with no label is an ordinary interaction (Enter and typed-submit are gated regardless).
6. **Only Chromium-family browsers** (Edge, Chrome, Chromium). No Firefox/Safari.
7. Browser crash recovery reopens the last active page only (other tabs are not restored).
8. Uploads are confirmed and verified up to "attached, not submitted"; there is no automated end-to-end upload against a real site.
9. `ComputerState` contains only the browser; window/application/screen fields are deliberately empty (Phase 21).
10. Not run: PostgreSQL variants (618 skips); real account sign-ins; a real dangerous download from the internet (an `.exe` was refused against the local test site).

## Recommended manual validation

1. `python -m desktop.launcher`, say "Hey JARVIS, open YouTube", then "Search for Blinding Lights", "Play the official one", "Pause", "Resume", "Skip", "Close YouTube" (headed: `BROWSER_HEADLESS=false`, the default).
2. "Open GitHub", "Open my Virtual Campus repository" (needs `JARVIS_GITHUB_ENABLED` and a token), "Open pull requests".
3. "Open example dot com", "Scroll down", "Click the more information link", "Go back", "Close the browser".
4. Try a consequential click on a harmless test page ("Click post comment" → JARVIS must ask; say "no").
5. Check the dashboard Browser panel and the tray items; run `python scripts/browser_real_check.py --headed`.
6. Sign in to a site yourself in the JARVIS browser window when asked, then confirm JARVIS never types the password.
