# Browser architecture (Phase 20)

A controlled browser agent: JARVIS can open sites, search, read, click, type, scroll, play and pause YouTube, and close the browser, through registered tools only. It is not a general computer controller (see "Boundary").

```
voice / text
   -> ConversationEngine.respond()                    (Phase 19 pipeline, unchanged entry point)
   -> IntelligenceRouter -> HubRouter (API first) -> BrowserRouter   deterministic patterns; replies built from verified results only
   -> BrowserTools.call(name, args)                   schema (extra keys forbidden) -> risk category -> PermissionManager
        -> EXTERNAL / SENSITIVE: ConfirmationEngine (the user's own spoken yes) -> tools.call(..., confirmed=True)
   -> BrowserEngine                                   ONE browser thread: lifecycle, session/tabs, navigation, targeting, verification, recovery
   -> PageDriver / BrowserDriver                      PlaywrightBrowserDriver (real) | FakeBrowserDriver (tests)
   -> Edge / Chrome / Chromium
   <- BrowserResult (normalized)                      success, action, target, url, verified, message | error, data, category, untrusted
```

Reused, not duplicated: Playwright (new dependency, pinned `playwright==1.63.0`; the installed Microsoft Edge/Chrome is used, so no browser download), the Phase 5 `PermissionManager`, the Phase 17 `ConfirmationEngine`, the Phase 18 GitHub integration, the Phase 19 voice pipeline, `backend.core.security.trust` (injection scan, sanitizer), `backend.core.redaction`, metrics, health monitor, tray, dashboard.

## Modules (`browser/`)

| File | Role |
|---|---|
| `models.py` | `BrowserState` lifecycle, `Category`, `Target` (semantic), `PageSnapshot`, `MediaState`, `BrowserResult`, `public_url()` |
| `urlsafe.py` | `validate_url` (http/https only; no local/private/metadata targets; DNS-rebinding check; no user info), `KNOWN_SITES`, `same_site` |
| `scripts.py` | the ONLY JavaScript ever run in a page: fixed constants, selected by name, read-only apart from media/scroll |
| `driver.py` | abstract `PageDriver`/`BrowserDriver` + Playwright implementation (navigation route, crash mapping, key allow-list) |
| `engine.py` | `BrowserEngine`: worker thread, sessions, tabs, verification, retries, recovery, downloads/uploads, metrics |
| `downloads.py` | `DownloadManager` (approved folder, no overwrite, dangerous types refused, hash, record) and `BrowserLog` (redacted JSONL) |
| `tools.py` | tool registry (31 tools), schemas, categories, `BrowserTools.call` (permission + confirmation gate) |
| `youtube.py` | YouTube workflow (search, scored selection, play/pause/resume/skip/seek/volume/close) |
| `voice.py` | `BrowserRouter`: spoken/typed commands, context, confirmation hand-off, wording |
| `control.py` | `BrowserControl` (dashboard/tray), `ComputerState`, `build_browser` |

## Browser lifecycle and session

States: `closed -> opening -> ready <-> navigating / waiting / action -> recovering -> ready | error`, `closing -> closed`. The browser starts **on demand** (never at JARVIS start-up) and is closed by the tray, the API, "close the browser" or application shutdown. `BrowserSession` = session id, tabs (`t1`, `t2`, ...), active tab, last action and its status. Tab addresses and titles are cached on the browser thread; the dashboard reads the cache and never touches a page from another thread (Playwright's sync API is thread-affine: found by the real-browser test, the first version returned empty titles).

Opening a *different* site while another page is showing puts it in a **new tab** (a playing video is not destroyed by "open GitHub"); the same site navigates in place; opening a site root that is already open **reuses that tab** ("open YouTube" five times = one tab). At `BROWSER_MAX_TABS` it navigates in place. Popups the site opens (`target=_blank`) become tabs; browser-internal pages (the downloads panel) are left alone.

## Element targeting and verification

Targets are semantic: role + accessible name, visible text, label, placeholder. CSS is accepted only from trusted internal code (the YouTube player buttons), never from a tool argument. There are no coordinates. `find_element` ranks visible controls by name/role words; a click on several different matches returns `ambiguous` with the candidates; an explicit `index` (the user said "the second one") resolves it.

Nothing is "done" unless verified:

| Action | Verified by |
|---|---|
| open_url | final host is the requested site (redirects inside the site allowed), HTTP status < 400, title/text present |
| click_element | the page fingerprint (URL, title, control counts, text) changed, or a tab opened, or a file downloaded; otherwise `success=false, "I clicked it, but nothing on the page changed."` |
| type_text | the field's value equals the text |
| scroll | scroll position moved, or it says it is already at the edge |
| upload_file | the input holds exactly that file (and says it is *not submitted*) |
| YouTube play | the `<video>` is present, not paused, time advancing (or an ad is playing, which is said) |
| pause / resume / volume / seek | the `<video>` state matches; already-paused returns the state instead of acting |
| close_tab(s) | the tab is gone |

A success that could not be verified is spoken as "…, but I couldn't verify that it worked."

## Failure handling

Bounded timeouts everywhere (`BROWSER_DEFAULT_TIMEOUT_SECONDS`, `BROWSER_NAVIGATION_TIMEOUT_SECONDS`, an outer bound per action). **Only safe operations retry** (loading, reading, finding: `BROWSER_RETRIES`). Clicks, typing, key presses, uploads, tab closing are never replayed, not even after a crash.

| Failure | Behavior |
|---|---|
| browser closed/disconnected/crashed | detect -> clean up stale state -> relaunch -> reopen the last page (a plain GET of its public address) -> retry only if the operation was safe; otherwise "The browser crashed and I restarted it. Please ask again." |
| one tab crashed | replaced by a fresh tab at the same address; no restart |
| launch failure | `error` state and a spoken reason; the next request tries again |
| navigation timeout / network error | mapped to a spoken reason (name doesn't resolve, couldn't connect, certificate invalid, unsafe port, blocked address); certificate errors are never bypassed |
| element missing / page changed | "Element not found." (after a bounded wait for dynamic pages) |
| CAPTCHA / bot check | reported; never solved (found for real: DuckDuckGo shows one to automated browsers, so search defaults to Bing) |
| YouTube consent page | reported; the user chooses; JARVIS does not click "Accept all" |
| stop | tray/dashboard "Stop browser action" sets a cancel flag checked at least every 200 ms |

## YouTube

`open_youtube`, `search_youtube`, `play_youtube`, `pause_youtube`, `resume_youtube`, `skip_youtube`, `seek_youtube`, `volume_youtube`, `close_youtube`, all on the engine. Search uses the results URL, then reads result cards through a fixed extraction script. **Selection** is scored: query words, exact/prefix title, official signals (Official Artist Channel / verified badge / VEVO / "- Topic" / "Official" in the title), a small preference for the official *video* over the official *audio* unless audio was asked, penalties for covers, remixes, karaoke, lyrics, live, reactions, slowed/sped-up, mixes, shorts (unless in the query). A result is played only if it clearly beats the rest (score ≥ 0.7 and margin ≥ 0.1); otherwise `Which one do you mean?` with the candidates, and "the second one" / "the official one" / "play number 3" resolve against the *current* results. A result page with no videos, an unrelated top result, or a UI change that breaks the player produces an honest failure. Ads are reported ("An ad is playing first"), can be skipped when the skip button is visible, and cannot be seeked.

## Integration with the Integration Hub and conversation

`BrowserRouter` runs **after** `HubRouter`: "Show my repositories" is answered by the GitHub API without starting a browser; "Open my Virtual Campus repository" resolves `owner/repo` through the hub (API) and only then opens it in the browser; "Search for my Virtual Campus repository" while GitHub is showing does the same, falling back to GitHub search in the browser only if the API cannot resolve it. Context ("Open GitHub" ... "Search for X") is the browser's real state (which site the active tab shows; the last YouTube results), in memory, never saved as personal memory. Browser replies are kept out of the LLM history (a placeholder replaces them).

## Boundary and Phase 21

Browser actions go through the Browser Engine only. There is no `execute_shell`, `run_powershell`, arbitrary file access or JavaScript tool (tested). `ComputerState` (`browser`, and empty `active_window`, `active_application`, `screen`, `input`) is the seam for a later phase; nothing beyond the browser is implemented.

## Observability

Dashboard **Browser** panel and `GET /browser` (state, tabs as `scheme://host/path`, titles, last action, verified, error, crashes/recoveries, timings), tray items (Open browser, Close browser, Stop browser action), health check `browser` (closed = normal, reported as disabled with a reason), structured log `.jarvis/browser_log.jsonl` (`timestamp, session_id, action, target, url (no query), duration_ms, verified, result`), metrics `browser.startup_ms`, `browser.navigation_ms`, `browser.action_ms`, `browser.lookup_ms`, `browser.verify_ms`, counters `browser.actions/failures/retries/crashes/recoveries`.

## Not connected: the LLM planner

The tools have `ToolDescriptor`s (`BrowserTools.descriptors()`), but the Phase 4 `AgentBrain` is **not** given them: browser requests are recognised by deterministic patterns (like Phase 17/18) so they work offline and page text cannot steer them. Handing the LLM these descriptors is a small follow-up (the permission/confirmation boundary is already the right one), listed under limitations in `PHASE_20_IMPLEMENTATION.md`.

## Phase 21: autonomous tasks on top of these tools

`autonomy/` plans and runs multi-step goals **through** this layer: every browser step goes `ToolRouter.call` → `BrowserTools.call` → engine, so schemas, the five permission categories, the PermissionManager, verification and crash recovery are exactly the ones described above. The engine gained: `page_url()` (internal full-address comparison), `same_site` that treats IPs and ports exactly (found by an autonomous task that mistook another local port for an open site), browser-internal pages (`edge://downloads-hub`) never adopted, and a stricter `upload_file` filename schema (no path characters). See `AUTONOMOUS_AGENT_ARCHITECTURE.md`.

## Phase 22: browser steps inside personal workflows

`open_url`, `read_page`, `find_element`, `click_element`, `upload_file` and `get_page_state` can be steps of a workflow (`email_to_browser`, `application_submit`). The URL comes from an email link that was validated when extracted and again when resolved (`validate_url`: http/https only, no credentials, no private/loopback/numeric-obfuscated hosts); several different links → the user is asked. Every browser step runs through the Phase 21 `ToolRouter` → `BrowserTools` → `PermissionManager`; the workflow never drives the engine directly. The final "Submit" click is classified EXTERNAL_EFFECT/SENSITIVE by code, so the workflow stops before it and asks (host of the page, requirements read from your documents, what will happen, "this can't be undone"); the confirmed click is recorded in the effect ledger so a restart can never repeat it. A workflow holds the browser lock while it uses the browser, so two workflows never interleave clicks.
